"""Show (or apply) how the split pass would divide oversized stories.

Lists every story the split pass would divide (ignoring when it was last
checked) with the planned parts and sample titles. With --apply, the given
stories are split right away (needs Ollama for the summary rewrites):
  uv run python scripts/split_stories.py
  uv run python scripts/split_stories.py 471 267 --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, db, split  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("story_ids", type=int, nargs="*", help="default: every story with enough articles")
    ap.add_argument("--apply", action="store_true", help="split the given stories now")
    args = ap.parse_args()
    if args.apply and not args.story_ids:
        ap.error("--apply needs story ids")

    with db.get_conn() as conn:
        ids = args.story_ids or [r[0] for r in conn.execute(
            "SELECT story_id FROM Article WHERE story_id IS NOT NULL AND duplicate_of IS NULL "
            "GROUP BY story_id HAVING count(*) >= ? ORDER BY count(*) DESC", (config.SPLIT_MIN_ARTICLES,))]
        for sid in ids:
            s = conn.execute("SELECT headline FROM Story WHERE id = ?", (sid,)).fetchone()
            if s is None:
                print(f"story {sid}: not found")
                continue
            groups = split.plan(conn, sid)
            print(f"story {sid}: {(s['headline'] or '')[:90]}")
            if not groups:
                print("  stays whole")
                continue
            for g in groups:
                titles = [r[0] for r in conn.execute(
                    "SELECT title FROM Article WHERE id IN (%s) ORDER BY created_at DESC LIMIT 3"
                    % ",".join("?" * len(g)), g)]
                print(f"  {len(g):4d}  " + "\n        ".join(t[:100] for t in titles))
            if args.apply:
                new_ids = split.apply(conn, sid, groups)
                conn.execute("UPDATE Story SET split_checked_at = datetime('now') WHERE id = ?", (sid,))
                print(f"  -> split into {[sid, *new_ids]}")
    if not args.apply:
        print("dry run (add story ids and --apply to split)")


if __name__ == "__main__":
    main()
