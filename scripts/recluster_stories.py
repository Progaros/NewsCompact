"""Dissolve over-merged stories so the pipeline re-clusters their articles.

Every article of the given stories goes back to the pending queue (newest
first, as usual); embeddings are kept, so only the verifier, harness and
Prompt 2 run again. The stories, their chat and merge marks are deleted;
votes keep their headline/tags snapshot. Dry run unless --apply is given.
Safe while the server runs (the next pipeline cycle picks the articles up):
  uv run python scripts/recluster_stories.py 471 7 356 --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("story_ids", type=int, nargs="+")
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args()

    with db.get_conn() as conn:
        for sid in args.story_ids:
            s = conn.execute("SELECT headline FROM Story WHERE id = ?", (sid,)).fetchone()
            if s is None:
                print(f"story {sid}: not found")
                continue
            n = conn.execute("SELECT count(*) FROM Article WHERE story_id = ?", (sid,)).fetchone()[0]
            print(f"story {sid}: {n} articles  {(s['headline'] or '')[:90]}")
            if not args.apply:
                continue
            # Copies stay attached to their original via duplicate_of and follow it.
            conn.execute(
                "UPDATE Article SET story_id = NULL, processed_at = NULL, t_merge_s = NULL, "
                "pipeline_failures = 0 WHERE story_id = ?", (sid,))
            conn.execute("UPDATE FeedbackLog SET story_id = NULL WHERE story_id = ?", (sid,))
            conn.execute("DELETE FROM Story WHERE id = ?", (sid,))
    print("applied" if args.apply else "dry run (add --apply)")


if __name__ == "__main__":
    main()
