"""Compare stored story metrics with what the current Prompt 2 anchors produce (dry run).

Re-runs the full-context Prompt 2 on a random sample of stories and prints the
old vs new urgency / impact / status distributions. Nothing is written unless
--write is given (then the sampled stories are rewritten via story.update_story).
Needs Ollama; run on the server:  uv run python scripts/rescore_metrics.py --sample 40
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, db, ollama, story  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--write", action="store_true", help="rewrite the sampled stories")
    args = ap.parse_args()

    old, new = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    with db.get_conn() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM Story WHERE headline IS NOT NULL ORDER BY random() LIMIT ?", (args.sample,))]
        for i, sid in enumerate(ids, 1):
            s = conn.execute("SELECT headline, metrics FROM Story WHERE id = ?", (sid,)).fetchone()
            arts = [dict(a) for a in conn.execute(
                "SELECT * FROM Article WHERE story_id = ? ORDER BY published_date", (sid,))]
            if not arts:
                continue
            before = db.json_dict(s["metrics"])
            if args.write:
                story.update_story(conn, sid, force_full=True)
                after = db.json_dict(conn.execute("SELECT metrics FROM Story WHERE id = ?", (sid,)).fetchone()[0])
            else:
                system, user = story._build_prompt(s, arts, None, force_full=True)
                out = ollama.structured(story._SCHEMA, [{"role": "system", "content": system},
                                                        {"role": "user", "content": user}],
                                        temperature=0.2, prose_fallback=True, model=config.STORY_MODEL)
                after = story.sanitize_metrics(out.get("metrics") if isinstance(out, dict) else {})
            for key in ("urgency_level", "impact_scale", "status"):
                old[key][before.get(key)] += 1
                new[key][after.get(key)] += 1
            print(f"[{i}/{len(ids)}] {before.get('urgency_level')}->{after.get('urgency_level')} "
                  f"{before.get('impact_scale')}->{after.get('impact_scale')}  {s['headline'][:70]}")

    for key in ("urgency_level", "impact_scale", "status"):
        print(f"\n{key}\n  old: {sorted(old[key].items(), key=str)}\n  new: {sorted(new[key].items(), key=str)}")


if __name__ == "__main__":
    main()
