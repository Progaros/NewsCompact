"""Probe: member-anchor gate (cos to closest real member) vs centroid gate.

For every story, simulate the shrink under a *member-anchor* criterion:
a member passes if its max cosine to any other non-broken (real) member
clears the join threshold. Iterate: drop the member with the lowest
best-anchor sim until the rest pass. Reports before/after membership so
the centroid-drift fix (app/pipeline.py) can be previewed on real data
(esp. story 7) before committing.

Usage: PYTHONPATH=. uv run python scripts/probe_member_gate.py [--story 7]
"""
import sys

from app import config, db
from app.pipeline import _normalize, _unpack_vec

TH = config.CLUSTER_SIM_THRESHOLD
RE = config.CLUSTER_REVIVE_SIM_THRESHOLD


def sim(a, b):
    return sum(x * y for x, y in zip(a, b))


def load_story(conn, sid):
    rows = conn.execute(
        "SELECT a.id, a.content_class, a.title, e.embedding "
        "FROM Article_Embeddings e JOIN Article a ON a.id = e.article_id "
        "WHERE a.story_id = ?",
        (sid,),
    ).fetchall()
    return {
        r["id"]: {
            "class": r["content_class"],
            "vec": _normalize(_unpack_vec(bytes(r["embedding"])))
            if r["embedding"] is not None else None,
            "title": r["title"] or "",
        }
        for r in rows
    }


def real_anchors(members):
    # Anchor set = non-broken (mirrors pipeline._story_anchors exclude_broken=True).
    return {aid: m for aid, m in members.items()
            if m["vec"] and (m["class"] is None or m["class"] != "broken")}


def best_anchor_sim(member_vec, anchors, self_id):
    best = -1.0
    for aid, a in anchors.items():
        if aid == self_id:
            continue
        s = sim(member_vec, a["vec"])
        if s > best:
            best = s
    return best


def simulate(members):
    """Iterative worst-drop under the member-anchor gate.
    Returns (kept, drops) where drops is a list of (id, bestsim, title)."""
    members = dict(members)
    drops = []
    for _ in range(len(members)):
        if len(members) <= 1:
            break
        anchors = real_anchors(members)
        # Junk-only: no real anchor, fall back to any member (centroid-ish).
        pool = anchors if len(anchors) >= 1 else {k: m for k, m in members.items() if m["vec"]}
        if not pool:
            break
        worst_id, worst = None, float("inf")
        for aid, m in members.items():
            if m["vec"] is None:
                continue
            bs = best_anchor_sim(m["vec"], pool, aid)
            if bs < worst:
                worst_id, worst = aid, bs
        if worst_id is None or worst >= TH:
            break
        drops.append((worst_id, worst, members[worst_id]["title"]))
        del members[worst_id]
    return members, drops


def main():
    story_arg = None
    th = TH
    if "--story" in sys.argv:
        i = sys.argv.index("--story")
        story_arg = int(sys.argv[i + 1])
    if "--th" in sys.argv:
        i = sys.argv.index("--th")
        th = float(sys.argv[i + 1])
        globals()["TH"] = th

    with db.get_conn() as conn:
        ids = [story_arg] if story_arg else [r["id"] for r in conn.execute("SELECT id FROM Story")]
        total_drops = 0
        shrank = 0
        for sid in ids:
            members = load_story(conn, sid)
            kept, drops = simulate(members)
            if not drops:
                continue
            shrank += 1
            total_drops += len(drops)
            headline = conn.execute("SELECT headline FROM Story WHERE id=?", (sid,)).fetchone()
            feeds = len({r[0] for r in conn.execute(
                "SELECT DISTINCT feed_id FROM Article WHERE story_id=?", (sid,))})
            hl = (headline["headline"] if headline else "") or ""
            if story_arg:
                print(f"story {sid}: {len(members)} -> {len(kept)} members "
                      f"(dropped {len(drops)})")
                for aid, s, t in drops:
                    print(f"  drop {aid} (best-anchor sim={s:.3f}): {t[:80]}")
                if kept:
                    anchors = real_anchors(kept)
                    print("  survivors:")
                    for aid, m in kept.items():
                        bs = best_anchor_sim(m["vec"], anchors, aid) if len(anchors) > 1 else 1.0
                        print(f"    keep {aid} (class={m['class']}, best-anchor={bs:.3f}): {m['title'][:70]}")
            else:
                print(f"story {sid}: {len(members)} -> {len(kept)}  feeds={feeds}  "
                      f"{hl[:80]}")
        if not story_arg:
            print(f"\nTH={th}: stories: {len(ids)} total, {shrank} would shrink, "
                  f"{total_drops} members dropped (revive {RE})")


if __name__ == "__main__":
    main()
