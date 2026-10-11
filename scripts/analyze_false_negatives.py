"""Analyze verifier false negatives in the calibrated golden dataset.

Only records expected to merge and eligible at the 0.55 retrieval threshold are
sent to Ollama. Comparisons are batched in groups of eight and the rejected
cases are written to ``false_negatives.md``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, harness  # noqa: E402
from scripts.benchmark_clustering import load_articles, story_context  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, default=Path("benchmark_pairs_200.json"))
    parser.add_argument("--output", type=Path, default=Path("false_negatives.md"))
    parser.add_argument("--threshold", type=float, default=0.55)
    args = parser.parse_args()

    records = json.loads(args.labels.read_text(encoding="utf-8"))
    records = [
        record for record in records
        if record.get("expected_merge") in (True, 1)
        and (record.get("cosine") or 0) >= args.threshold
    ]
    with db.get_conn() as conn:
        articles, _vectors = load_articles(conn)
        contexts = story_context(conn)

    pending = []
    for record in records:
        key = f"{record['left_id']}:{record['right_id']}"
        left = articles[record["left_id"]]
        right = articles[record["right_id"]]
        story = contexts.get(right["story_id"], {})
        pending.append({
            "key": key,
            "new_title": left.get("title") or "",
            "new_text": left.get("content_raw") or "",
            "headline": story.get("headline", right.get("title") or ""),
            "summary": story.get("summary", ""),
            "record": record,
        })

    decisions: dict[str, dict[str, str]] = {}
    for start in range(0, len(pending), 1):
        batch = pending[start:start + 1]
        decisions.update(harness.harness.analyze_event_pairs(batch))

    false_negatives = []
    for item in pending:
        decision = decisions.get(item["key"], {"classification": "UNRELATED", "reasoning": "No verifier decision returned."})
        if decision["classification"] not in {"EXACT_MATCH", "CONTINUING_DEVELOPMENT"}:
            record = item["record"]
            false_negatives.append(
                f"### Pair {item['key']}\n"
                f"- Article: `{record['new_article_id']}`\n"
                f"- Story: `{record['candidate_story_id']}`\n"
                f"- Expected: `{record.get('expected_classification')}`\n"
                f"- Verifier: `{decision['classification']}`\n"
                f"- Reasoning: {decision['reasoning']}\n"
            )

    header = "# Verifier false negatives\n\n"
    header += f"Analyzed {len(pending)} expected merges at retrieval threshold {args.threshold}.\n\n"
    args.output.write_text(header + "\n".join(false_negatives), encoding="utf-8")
    print(f"false_negatives={len(false_negatives)} analyzed={len(pending)} output={args.output}")


if __name__ == "__main__":
    main()
