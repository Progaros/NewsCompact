"""Shrink stories for the member-anchor gate (2026-10-11).

The pipeline now joins articles to a story only against its anchor
(non-broken) members, not its centroid (app/pipeline.py, decide_story).
This migration applies the same gate to *existing* membership: per story
it iteratively drops the member whose best-anchor sim (max cosine to any
other non-broken member) is lowest while it is below CLUSTER_SIM_THRESHOLD.
Dropped articles get story_id = NULL and are re-clustered on the next
pipeline cycle (usually into their own fresh story). Stories left empty
are deleted.

The shrink is simulated in memory per story (anchor set recomputed after
each drop), so dry-run and commit produce identical decisions. Safe to
re-run: drops are strictly below the current join threshold, so dropped
articles don't oscillate back in on the next cycle.

Usage:
    PYTHONPATH=. uv run python scripts/shrink_member_gate.py          # dry run
    PYTHONPATH=. uv run python scripts/shrink_member_gate.py --commit
"""
import argparse

from app import config, db
from app.pipeline import _normalize, _unpack_vec

TH = config.CLUSTER_SIM_THRESHOLD


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true", help="apply changes")
    args = p.parse_args()

    with db.get_conn() as conn:
        total = 0
        for s in conn.execute("SELECT id FROM Story").fetchall():
            sid = s["id"]
            rows = conn.execute(
                "SELECT a.id, a.content_class, a.title, e.embedding "
                "FROM Article_Embeddings e "
                "JOIN Article a ON a.id = e.article_id WHERE a.story_id = ?",
                (sid,),
            ).fetchall()
            members = {
                r["id"]: {
                    "class": r["content_class"],
                    "vec": _normalize(_unpack_vec(bytes(r["embedding"])))
                    if r["embedding"] is not None else None,
                    "title": r["title"] or "",
                }
                for r in rows
            }

            def best_anchor(aid, pool):
                best = -1.0
                m = members[aid]["vec"]
                for j, a in pool.items():
                    if j == aid or a["vec"] is None:
                        continue
                    s = sum(x * y for x, y in zip(m, a["vec"]))
                    if s > best:
                        best = s
                return best

            drops: list[str] = []
            for _ in range(len(members)):
                if len(members) <= 1:
                    break
                # Anchor pool = non-broken members (mirrors pipeline._story_anchors);
                # broken-only stories fall back to all members.
                pool = {k: m for k, m in members.items()
                        if m["vec"] and (m["class"] is None or m["class"] != "broken")}
                if not pool:
                    pool = {k: m for k, m in members.items() if m["vec"]}
                if not pool:
                    break
                worst_id, worst = None, float("inf")
                for aid, m in members.items():
                    if m["vec"] is None:
                        continue
                    bs = best_anchor(aid, pool)
                    if bs < worst:
                        worst_id, worst = aid, bs
                if worst_id is None or worst >= TH:
                    break
                drops.append(worst_id)
                print(f"story {sid}: drop {worst_id} (anchor sim={worst:.3f}): "
                      f"{members[worst_id]['title'][:70]}")
                del members[worst_id]

            if args.commit:
                for aid in drops:
                    conn.execute("UPDATE Article SET story_id = NULL WHERE id = ?", (aid,))
                if not members:
                    conn.execute("DELETE FROM Story WHERE id = ?", (sid,))
                    print(f"story {sid}: deleted (empty)")
            total += len(drops)

        print(f"total members dropped: {total} (threshold {TH}, "
              f"{'committed' if args.commit else 'dry run'})")
        if args.commit:
            conn.commit()


if __name__ == "__main__":
    main()
