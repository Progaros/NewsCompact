"""Tag synonym merging (app/tag_merge.py) from the command line.

The worker runs `propose` once a day (NEWS_TAG_MERGE_*). By hand:
  uv run python scripts/merge_tags.py neighbours --min-sim 0.7     # embeddings only: calibrate thresholds
  uv run python scripts/merge_tags.py propose --dry-run            # ask the LLM, print, store nothing
  uv run python scripts/merge_tags.py propose                      # store verdicts (auto-approves sure ones)
  uv run python scripts/merge_tags.py list [--status pending]      # pending | approved | rejected | all
  uv run python scripts/merge_tags.py approve ALIAS [CANONICAL]
  uv run python scripts/merge_tags.py reject ALIAS [CANONICAL]
  uv run python scripts/merge_tags.py add ALIAS CANONICAL           # manual alias, approved at once
Restart nothing: the server reloads approved aliases before each ranking.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, db, tag_merge, tags  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    nb = sub.add_parser("neighbours")
    nb.add_argument("--min-sim", type=float, default=config.TAG_MERGE_REVIEW_SIM)
    nb.add_argument("--limit", type=int, default=100)
    pr = sub.add_parser("propose")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--max", type=int, default=config.TAG_MERGE_MAX_VERIFY, help="LLM calls")
    ls = sub.add_parser("list")
    ls.add_argument("--status", default="pending", choices=("pending", "approved", "rejected", "all"))
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("alias")
        p.add_argument("canonical", nargs="?")
    add = sub.add_parser("add")
    add.add_argument("alias")
    add.add_argument("canonical")
    args = ap.parse_args()

    if args.cmd == "neighbours":
        with db.get_conn() as conn:
            tags.load_aliases(conn)
            cands = tag_merge.candidate_tags(conn)
            pairs = tag_merge.neighbour_pairs(tag_merge.ensure_vectors(conn, sorted(cands)), args.min_sim)
        print(f"{len(cands)} candidate tags, {len(pairs)} pairs at >= {args.min_sim}")
        for a, b, sim in pairs[:args.limit]:
            print(f"  {sim:.3f}  {a} ({cands[a]})  ~  {b} ({cands[b]})")
    elif args.cmd == "propose":
        result = tag_merge.run(apply=not args.dry_run, max_verify=args.max)
        print(f"{result['candidates']} candidate tags, {result['pairs']} new pairs; "
              f"approved {result['approved']}, pending {result['pending']}, rejected {result['rejected']}"
              + (" (dry run, nothing stored)" if args.dry_run else ""))
        for r in result["rows"]:
            print(f"  {r['status']:8s} {r['similarity']:.3f} {r['relation']:12s} {r['confidence']:.2f}  "
                  f"{r['alias']} ({r['stories'][0]}) -> {r['canonical']} ({r['stories'][1]})")
    elif args.cmd == "list":
        with db.get_conn() as conn:
            sql = "SELECT * FROM TagAlias" + ("" if args.status == "all" else " WHERE status = ?")
            rows = conn.execute(sql + " ORDER BY similarity DESC", () if args.status == "all" else (args.status,))
            for r in rows:
                print(f"  {r['status']:8s} {r['similarity'] or 0:.3f} {r['relation'] or '-':12s} "
                      f"{r['confidence'] or 0:.2f} {r['method']:13s} {r['alias']} -> {r['canonical']}")
    elif args.cmd in ("approve", "reject"):
        with db.get_conn() as conn:
            n = tag_merge.review(conn, tags.base_tag(args.alias), "approved" if args.cmd == "approve" else "rejected",
                                 tags.base_tag(args.canonical) if args.canonical else None)
        print(f"{n} row(s) {args.cmd}d")
    elif args.cmd == "add":
        with db.get_conn() as conn:
            print("approved: %s -> %s" % tag_merge.add_manual(conn, args.alias, args.canonical))


if __name__ == "__main__":
    main()
