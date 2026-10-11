"""Re-classify stored article bodies and reprocess the ones that flipped.

The content_quality.classify() rule changed (2026-10-10): a page whose chrome
signals REPEAT across the body (social feed, "Comment permalink" xN) is
broken, but an article that merely opens with one chrome line followed by
real text ("Reading time | Post permalink In the spring, we hit ...") now
classifies ok
or thin by length. Rows whose class flips need a fresh embed (broken rows
were embedded title-only; ok/thin rows embed title + body) and a fresh harness
score (broken rows were scored from the headline).

So for every classified row whose class changed: delete its vec0 row, null
quality + content_class, and unassign it — the pipeline re-processes
(embed, harness, cluster, Prompt2) on the next cycle. Unclassified rows
(content_class NULL) are left alone: they are unassigned backlog and get
classified when processed.

Safe to re-run: after one pass the stored classes match classify(), so a
second pass finds nothing.

Usage:
    PYTHONPATH=. uv run python scripts/reclassify_content.py          # dry run
    PYTHONPATH=. uv run python scripts/reclassify_content.py --commit
"""
import argparse

from app import content_quality, db


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true", help="apply changes")
    args = p.parse_args()

    flips: list[tuple[int, str | None, str]] = []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content_class, content_raw, story_id FROM Article"
        ).fetchall()
    for r in rows:
        new = content_quality.classify(r["content_raw"])
        if r["content_class"] is not None and r["content_class"] != new:
            flips.append((r["id"], r["content_class"], new))

    import collections
    print(collections.Counter((o, n) for _, o, n in flips))

    with db.get_conn() as conn:
        # Sanity: unclassified rows must all be unassigned (backlog), so the
        # pipeline will classify them when it processes them.
        stuck = conn.execute(
            "SELECT count(*) FROM Article WHERE content_class IS NULL "
            "AND story_id IS NOT NULL"
        ).fetchone()[0]
        print(f"unclassified but attached (should be 0): {stuck}")
        for aid, old, new in flips:
            print(f"flip {aid}: {old} -> {new}")
            if args.commit:
                conn.execute(
                    "DELETE FROM Article_Embeddings WHERE article_id = ?", (aid,)
                )
                conn.execute(
                    "UPDATE Article SET story_id = NULL, quality = NULL, "
                    "content_class = NULL WHERE id = ?", (aid,)
                )
        if args.commit:
            empty = conn.execute(
                "SELECT id FROM Story WHERE NOT EXISTS "
                "(SELECT 1 FROM Article a WHERE a.story_id = Story.id)"
            ).fetchall()
            for s in empty:
                conn.execute("DELETE FROM Story WHERE id = ?", (s["id"],))
            print(f"deleted {len(empty)} empty stories")
            conn.commit()
    print(f"total flips: {len(flips)} ({'committed' if args.commit else 'dry run'})")


if __name__ == "__main__":
    main()
