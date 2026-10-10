"""Shrink diluted stories: unassign members below the join threshold.

Background: the story centroid is the mean of member embeddings, so junk
titles (embedded title-only) that landed in a story pulled its centroid
toward themselves — a self-reinforcing dilution that kept absorbing more
junk (e.g. story 7 grew to 16 mixed members). Since the centroid is now
computed from *non-junk* members only (app/pipeline.py, 2026-10-10), a
member that sits below CLUSTER_SIM_THRESHOLD relative to the junk-free
centroid is not really part of the story.

This migration walks each story and unassigns (story_id = NULL) the
lowest-similarity member while it is below the threshold — the pipeline
re-clusters it on a later cycle (usually into its own fresh story). It
also unassigns articles that are attached but never scored
(quality IS NULL) so they get the per-article harness pass, and deletes
stories left with no members.

The shrink is simulated in memory per story (centroid recomputed after
each drop), so dry-run and commit produce identical decisions. Safe to
re-run: it only removes members failing the *current* threshold, which is
exactly the threshold the pipeline joins at, so unassigned articles don't
oscillate in and out.

Usage:
    PYTHONPATH=. uv run python scripts/shrink_diluted_stories.py          # dry run
    PYTHONPATH=. uv run python scripts/shrink_diluted_stories.py --commit
"""
import argparse

from app import config, db
from app.pipeline import _normalize, _unpack_vec


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true", help="apply changes")
    args = p.parse_args()

    th = config.CLUSTER_SIM_THRESHOLD
    with db.get_conn() as conn:
        # Pass 1: unassign never-scored attached articles (re-processed next
        # pipeline cycle: per-article harness + re-clustering).
        unscored = conn.execute(
            "SELECT id, story_id FROM Article "
            "WHERE story_id IS NOT NULL AND quality IS NULL"
        ).fetchall()
        for row in unscored:
            print(f"unscored article {row['id']} (story {row['story_id']})")
            if args.commit:
                conn.execute("UPDATE Article SET story_id = NULL WHERE id = ?", (row["id"],))

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
                    "title": r["title"],
                }
                for r in rows
            }

            def centroid():
                # Mirror pipeline._story_centroid: non-junk members first,
                # fall back to everything.
                sel = [m["vec"] for m in members.values()
                       if m["vec"] and m["class"] != "junk"]
                if not sel:
                    sel = [m["vec"] for m in members.values() if m["vec"]]
                if not sel:
                    return None
                acc = [0.0] * len(sel[0])
                for v in sel:
                    for k in range(len(v)):
                        acc[k] += v[k]
                n = len(sel)
                return _normalize([x / n for x in acc])

            drops: list[str] = []
            for _ in range(len(members)):
                cent = centroid()
                if cent is None:
                    break
                worst_id, worst = -1, float("inf")
                for aid, m in members.items():
                    if m["vec"] is None:
                        continue
                    sim = sum(a * b for a, b in zip(m["vec"], cent))
                    if sim < worst:
                        worst_id, worst = aid, sim
                if worst >= th or worst_id == -1 or len(members) <= 1:
                    break
                drops.append(worst_id)
                print(f"story {sid}: drop {worst_id} (sim={worst:.3f}): "
                      f"{members[worst_id]['title'][:70]}")
                del members[worst_id]

            if args.commit:
                for aid in drops:
                    conn.execute("UPDATE Article SET story_id = NULL WHERE id = ?", (aid,))
                if not members:
                    conn.execute("DELETE FROM Story WHERE id = ?", (sid,))
                    print(f"story {sid}: deleted (empty)")
            total += len(drops)

        print(f"total members dropped: {total} ({'committed' if args.commit else 'dry run'})")
        if args.commit:
            conn.commit()


if __name__ == "__main__":
    main()
