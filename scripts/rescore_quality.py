"""One-time re-score: apply the new quality logic to existing stories.

The stored story quality was last-writer-wins and scored on junk RSS
descriptions (see findings.md, ICC story quality 2). This re-scores every
*assigned* article with the calibrated harness (junk bodies -> headline-based
score) and recomputes each story quality as the median of its member
articles. Safe to re-run; it skips articles that already have a quality unless
--force is given, so re-runs after a crash are cheap.

Usage:
    PYTHONPATH=. uv run python scripts/rescore_quality.py [--force]
"""
from __future__ import annotations

import statistics
import sys

sys.path.insert(0, ".")
from app import db  # noqa: E402
from app import harness  # noqa: E402
from app.content_quality import classify  # noqa: E402

FORCE = "--force" in sys.argv


def _dist(conn) -> str:
    rows = conn.execute(
        "SELECT quality_score q FROM Story"
    ).fetchall()
    buckets: dict[int, int] = {}
    for r in rows:
        b = int(round(r["q"]))
        buckets[b] = buckets.get(b, 0) + 1
    return " ".join(f"{k}:{buckets.get(k, 0)}" for k in sorted(buckets))


def main() -> None:
    db.init_db()
    h = harness.LocalJevHarness()
    with db.get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        before = _dist(conn)
        print("story quality BEFORE:", before)

        arts = conn.execute(
            "SELECT id, title, content_raw, quality, content_class, story_id FROM Article "
            "WHERE story_id IS NOT NULL ORDER BY id"
        ).fetchall()
        print(f"assigned articles to re-score: {len(arts)}  (force={FORCE})")

        done = skipped = 0
        for a in arts:
            text = a["content_raw"] or ""
            cls = classify(text)
            if not FORCE and a["quality"] is not None:
                skipped += 1
                continue
            try:
                q, cls = h.article_quality(a["title"] or "", text)
            except Exception as e:  # noqa: BLE001 - one bad article isn't fatal
                print(f"  score failed art={a['id']}: {e}")
                continue
            conn.execute(
                "UPDATE Article SET quality = ?, content_class = ? WHERE id = ?",
                (q, cls, a["id"]),
            )
            done += 1
            print(f"  art {a['id']} {cls} -> q={q}")
        print(f"scored {done}, skipped {skipped}")

        # Recompute story quality = median of member per-article qualities.
        stories = conn.execute(
            "SELECT id FROM Story"
        ).fetchall()
        for s in stories:
            quals = [r["quality"] for r in conn.execute(
                "SELECT quality FROM Article WHERE story_id = ? AND quality IS NOT NULL",
                (s["id"],),
            )]
            if quals:
                conn.execute(
                    "UPDATE Story SET quality_score = ? WHERE id = ?",
                    (round(float(statistics.median(quals)), 2), s["id"]),
                )
        print("story quality AFTER: ", _dist(conn))

        # Show the ICC story specifically.
        row = conn.execute(
            "SELECT id, headline, quality_score FROM Story WHERE id = 5"
        ).fetchone()
        if row:
            print("ICC story:", dict(row))

        # Junk vs quality cross-tab (sanity).
        print("\ncontent_class distribution (assigned):")
        for r in conn.execute(
            "SELECT content_class, COUNT(*) c FROM Article WHERE story_id IS NOT NULL "
            "GROUP BY content_class"
        ):
            print(f"  {r['content_class'] or 'NULL'}: {r['c']}")


if __name__ == "__main__":
    main()
