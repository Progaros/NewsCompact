"""Benchmark clustering decisions against a manually labeled pair set.

The benchmark is deliberately read-only. It can export a reviewable JSON
file from the current database, then evaluate labeled pairs against:

* member-anchor cosine thresholds (the production baseline), and
* candidate retrieval followed by the optional LLM event verifier.

Examples:
    PYTHONPATH=. uv run python scripts/benchmark_clustering.py --export /tmp/pairs.json
    PYTHONPATH=. uv run python scripts/benchmark_clustering.py --labels /tmp/pairs.labeled.json
    PYTHONPATH=. uv run python scripts/benchmark_clustering.py --labels /tmp/pairs.labeled.json --llm

Exported records have ``label: null``. Reviewers should set it to 1 only when
the two articles describe the same specific event, otherwise 0. Existing
story membership is included as provenance, not treated as ground truth.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, harness  # noqa: E402

DEFAULT_THRESHOLDS = (0.55, 0.60, 0.62, 0.64, 0.66, 0.70)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def normalize(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / length for value in vector]


def similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def load_articles(conn) -> tuple[dict[int, dict[str, Any]], dict[int, list[float]]]:
    articles: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT id, title, content_raw, story_id, content_class FROM Article "
        "WHERE story_id IS NOT NULL ORDER BY id"
    ):
        articles[row["id"]] = dict(row)

    vectors: dict[int, list[float]] = {}
    for row in conn.execute(
        "SELECT article_id, embedding FROM Article_Embeddings"
    ):
        article_id = row["article_id"]
        if article_id in articles:
            vectors[article_id] = normalize(unpack(bytes(row["embedding"])))
    return articles, vectors


def story_context(conn) -> dict[int, dict[str, str]]:
    return {
        row["id"]: {
            "headline": row["headline"] or "",
            "summary": row["summary"] or "",
        }
        for row in conn.execute("SELECT id, headline, summary FROM Story")
    }


def export_pairs(
    articles: dict[int, dict[str, Any]],
    vectors: dict[int, list[float]],
    limit: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Create positives from existing stories and hard negatives by similarity."""
    rng = random.Random(seed)
    by_story: dict[int, list[int]] = defaultdict(list)
    for article_id, article in articles.items():
        if article_id in vectors:
            by_story[article["story_id"]].append(article_id)

    positives: list[tuple[float, int, int]] = []
    for member_ids in by_story.values():
        if len(member_ids) < 2:
            continue
        # Prefer difficult positives: low similarity pairs are most useful for
        # deciding whether the current gate is fragmenting real events.
        for index, left_id in enumerate(member_ids):
            for right_id in member_ids[index + 1:]:
                score = similarity(vectors[left_id], vectors[right_id])
                positives.append((score, left_id, right_id))
    positives.sort()

    negatives: list[tuple[float, int, int]] = []
    ids = list(vectors)
    # A full Python all-pairs scan is needlessly expensive at this scale. Use
    # a deterministic sample of left-hand articles, but compare each sampled
    # article with the complete right-hand pool so hard negatives remain real.
    sample_size = min(len(ids), max(80, limit * 2))
    sampled_ids = rng.sample(ids, sample_size)
    for left_id in sampled_ids:
        nearest: list[tuple[float, int]] = []
        for right_id in ids:
            if left_id == right_id or articles[left_id]["story_id"] == articles[right_id]["story_id"]:
                continue
            nearest.append((similarity(vectors[left_id], vectors[right_id]), right_id))
        nearest.sort(reverse=True)
        negatives.extend((score, left_id, right_id) for score, right_id in nearest[:5])

    # Deduplicate and keep both hard negatives and hard positives, then add a
    # small random sample so the review set is not only edge cases.
    negative_seen: set[tuple[int, int]] = set()
    unique_negatives = []
    for score, left_id, right_id in sorted(negatives, reverse=True):
        key = tuple(sorted((left_id, right_id)))
        if key not in negative_seen:
            negative_seen.add(key)
            unique_negatives.append((score, left_id, right_id))
    rng.shuffle(positives)
    rng.shuffle(unique_negatives)
    positives = sorted(positives[: max(1, limit // 2)])
    unique_negatives = sorted(unique_negatives[: max(1, limit - len(positives))], reverse=True)

    records = []
    for expected, rows in ((None, positives), (None, unique_negatives)):
        for score, left_id, right_id in rows:
            left, right = articles[left_id], articles[right_id]
            records.append({
                "left_id": left_id,
                "right_id": right_id,
                "new_article_id": left_id,
                "candidate_story_id": right["story_id"],
                "label": expected,
                "expected_merge": expected,
                "expected_classification": None,
                "category": "same_current_story" if left["story_id"] == right["story_id"] else "hard_negative",
                "notes": "",
                "cosine": round(score, 6),
                "same_current_story": left["story_id"] == right["story_id"],
                "provenance": "same_current_story" if left["story_id"] == right["story_id"] else "nearest_other_story",
            })
    return records


def metric(records: list[dict[str, Any]], predictions: dict[str, bool]) -> dict[str, Any]:
    labeled = [
        record for record in records
        if record.get("label") in (0, 1)
        or record.get("expected_merge") in (True, False)
    ]
    if not labeled:
        return {"labeled": 0, "note": "Set label to 0 or 1 in the exported JSON."}
    def prediction(record: dict[str, Any]) -> bool:
        key = f"{record['left_id']}:{record['right_id']}"
        return predictions[key]

    def expected_merge(record: dict[str, Any]) -> bool:
        value = record.get("expected_merge")
        if value is None:
            value = record.get("label")
        return value is True or value == 1

    tp = sum(prediction(record) and expected_merge(record) for record in labeled)
    fp = sum(prediction(record) and not expected_merge(record) for record in labeled)
    fn = sum(not prediction(record) and expected_merge(record) for record in labeled)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "labeled": len(labeled),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
    }


def evaluate(
    records: list[dict[str, Any]],
    articles: dict[int, dict[str, Any]],
    contexts: dict[int, dict[str, str]],
    threshold: float,
    run_llm: bool,
    verification_cache: dict[str, str] | None = None,
) -> dict[str, Any]:
    labeled = [
        record for record in records
        if record.get("label") in (0, 1)
        or record.get("expected_merge") in (True, False)
    ]
    cosine_predictions = {
        f"{record['left_id']}:{record['right_id']}": record["cosine"] >= threshold
        for record in labeled
    }
    result: dict[str, Any] = {
        "cosine": metric(labeled, cosine_predictions),
        "threshold": threshold,
    }
    if run_llm:
        verification_cache = verification_cache if verification_cache is not None else {}
        verified: dict[str, bool] = {}
        pending_pairs = []
        for record in labeled:
            key = f"{record['left_id']}:{record['right_id']}"
            if record["cosine"] < threshold:
                verified[key] = False
                continue
            if key in verification_cache:
                classification = verification_cache[key]
                verified[key] = classification in {"EXACT_MATCH", "CONTINUING_DEVELOPMENT"}
                continue
            left = articles[record["left_id"]]
            right = articles[record["right_id"]]
            story = contexts.get(right["story_id"], {})
            pending_pairs.append({
                "key": key,
                "new_title": left.get("title") or "",
                "new_text": left.get("content_raw") or "",
                "headline": story.get("headline", right.get("title") or ""),
                "summary": story.get("summary", ""),
            })
        for start in range(0, len(pending_pairs), 8):
            decisions = harness.harness.classify_event_pairs(pending_pairs[start:start + 8])
            verification_cache.update(decisions)
        for record in labeled:
            key = f"{record['left_id']}:{record['right_id']}"
            if record["cosine"] < threshold:
                verified[key] = False
            else:
                classification = verification_cache.get(key, "UNRELATED")
                verified[key] = classification in {"EXACT_MATCH", "CONTINUING_DEVELOPMENT"}
        result["two_stage"] = metric(labeled, verified)
        expected_classes = {
            f"{record['left_id']}:{record['right_id']}": record["expected_classification"]
            for record in labeled if record.get("expected_classification")
        }
        if expected_classes:
            result["taxonomy"] = {
                "labeled": len(expected_classes),
                "correct": sum(
                    verification_cache.get(key) == expected
                    for key, expected in expected_classes.items()
                ),
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="write an unlabeled review set")
    parser.add_argument("--labels", type=Path, help="evaluate a labeled JSON set")
    parser.add_argument("--show", type=Path, help="print pair titles for review")
    parser.add_argument("--upgrade", type=Path, help="upgrade an existing pair file to the taxonomy schema")
    parser.add_argument("--limit", type=int, default=40, help="number of exported pairs")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--thresholds", type=float, nargs="*", default=list(DEFAULT_THRESHOLDS))
    parser.add_argument("--llm", action="store_true", help="run event verification for labeled pairs")
    args = parser.parse_args()

    if not args.export and not args.labels and not args.show and not args.upgrade:
        parser.error("choose --export, --labels, --show, or --upgrade")
    with db.get_conn() as conn:
        articles, vectors = load_articles(conn)
        contexts = story_context(conn)
        if args.upgrade:
            records = json.loads(args.upgrade.read_text(encoding="utf-8"))
            for record in records:
                left = articles[record["left_id"]]
                right = articles[record["right_id"]]
                record.setdefault("new_article_id", record["left_id"])
                record.setdefault("candidate_story_id", right["story_id"])
                record.setdefault("expected_merge", record.get("label"))
                record.setdefault("expected_classification", None)
                record.setdefault(
                    "category",
                    "same_current_story"
                    if left["story_id"] == right["story_id"]
                    else "hard_negative",
                )
                record.setdefault("notes", "")
            args.upgrade.write_text(json.dumps(records, indent=2), encoding="utf-8")
            print(f"upgraded={len(records)} path={args.upgrade}")
            return
        if args.show:
            records = json.loads(args.show.read_text(encoding="utf-8"))
            for index, record in enumerate(records, 1):
                left = articles[record["left_id"]]
                right = articles[record["right_id"]]
                print(
                    f"{index:02d} cosine={record['cosine']:.3f} "
                    f"left={record['left_id']} {left.get('title')!r} || "
                    f"right={record['right_id']} {right.get('title')!r}"
                )
            return
        if args.export:
            records = export_pairs(articles, vectors, args.limit, args.seed)
            args.export.write_text(json.dumps(records, indent=2), encoding="utf-8")
            print(f"exported={len(records)} path={args.export}")
            print("Review each record and set label: 1=same event, 0=different event.")
            return
        records = json.loads(args.labels.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("labels file must contain a JSON array")
        verification_cache: dict[str, str] = {}
        print(json.dumps({
            "articles": len(articles),
            "vectors": len(vectors),
            "records": len(records),
            "results": [evaluate(records, articles, contexts, threshold, args.llm, verification_cache) for threshold in args.thresholds],
        }, indent=2))


if __name__ == "__main__":
    main()
