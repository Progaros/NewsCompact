"""Mine a taxonomy-aware clustering golden set from the SQLite database.

Existing records in the output file are preserved byte-for-byte in meaning;
only new, deduplicated pairs are appended. This is candidate generation, not
automatic truth labeling, so every generated record includes provenance and
should receive a human review before model scoring.

Usage:
    uv run python scripts/benchmark/build_golden_dataset.py \
        --input benchmark_pairs.json --output benchmark_pairs.json --target 200
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import struct
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import db

_WORD_RE = re.compile(r"[^a-z0-9]+")


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def normalize(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / length for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def title_key(title: str) -> str:
    return _WORD_RE.sub(" ", (title or "").lower()).strip()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def record(
    left: dict[str, Any], right: dict[str, Any],
    classification: str, category: str, notes: str, score: float | None = None,
) -> dict[str, Any]:
    return {
        "left_id": left["id"],
        "right_id": right["id"],
        "new_article_id": left["id"],
        "candidate_story_id": right["story_id"],
        "label": 1 if classification in {"EXACT_MATCH", "CONTINUING_DEVELOPMENT"} else 0,
        "expected_merge": classification in {"EXACT_MATCH", "CONTINUING_DEVELOPMENT"},
        "expected_classification": classification,
        "category": category,
        "notes": notes,
        "cosine": round(score, 6) if score is not None else None,
        "same_current_story": left["story_id"] == right["story_id"],
        "provenance": "database_mined",
    }


def pair_key(item: dict[str, Any]) -> tuple[int, int]:
    return tuple(sorted((int(item["left_id"]), int(item["right_id"]))))


def add_unique(target: list[dict[str, Any]], candidates: list[dict[str, Any]], seen: set[tuple[int, int]], limit: int) -> None:
    for item in candidates:
        key = pair_key(item)
        if key in seen:
            continue
        seen.add(key)
        target.append(item)
        if len(target) >= limit:
            return


def add_category(
    target: list[dict[str, Any]], candidates: list[dict[str, Any]],
    seen: set[tuple[int, int]], category_limit: int, total_limit: int,
) -> None:
    """Append one category without allowing it to consume the whole set."""
    start = len(target)
    for item in candidates:
        if len(target) >= total_limit or len(target) - start >= category_limit:
            return
        key = pair_key(item)
        if key in seen:
            continue
        seen.add(key)
        target.append(item)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    existing = json.loads(args.input.read_text(encoding="utf-8")) if args.input.exists() else []
    if not isinstance(existing, list):
        raise ValueError("input golden set must be a JSON array")
    if len(existing) >= args.target:
        args.output.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        print(f"unchanged={len(existing)} target={args.target}")
        return

    rng = random.Random(args.seed)
    with db.get_conn() as conn:
        articles: dict[int, dict[str, Any]] = {}
        for row in conn.execute(
            "SELECT id, title, feed_id, story_id, published_date, created_at, content_class "
            "FROM Article WHERE story_id IS NOT NULL ORDER BY id"
        ):
            item = dict(row)
            item["time"] = parse_time(item["published_date"]) or parse_time(item["created_at"])
            articles[item["id"]] = item
        vectors = {
            row["article_id"]: normalize(unpack(bytes(row["embedding"])))
            for row in conn.execute("SELECT article_id, embedding FROM Article_Vectors")
            if row["article_id"] in articles
        }

    seen = {pair_key(item) for item in existing}
    additions: list[dict[str, Any]] = []
    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_story: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in articles.values():
        if title_key(item["title"] or ""):
            by_title[title_key(item["title"] or "")].append(item)
        by_story[item["story_id"]].append(item)

    # Exact syndication: identical normalized title, different feeds, close time.
    syndication = []
    for group in by_title.values():
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                if left["feed_id"] == right["feed_id"]:
                    continue
                if left["time"] and right["time"] and abs((left["time"] - right["time"]).total_seconds()) > 7200:
                    continue
                syndication.append(record(left, right, "EXACT_MATCH", "direct_syndication", "identical normalized title across feeds"))
    rng.shuffle(syndication)
    remaining = args.target - len(existing)
    add_category(additions, syndication, seen, 40, remaining)

    # Same story with a 12-48 hour gap: candidate continuations for review.
    continuations = []
    for members in by_story.values():
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                if left["feed_id"] == right["feed_id"] or not left["time"] or not right["time"]:
                    continue
                hours = abs((left["time"] - right["time"]).total_seconds()) / 3600
                if 12 <= hours <= 48:
                    continuations.append(record(
                        left, right, "CONTINUING_DEVELOPMENT", "temporal_update",
                        f"same current story, {hours:.1f} hour publication gap",
                        cosine(vectors[left["id"]], vectors[right["id"]])
                        if left["id"] in vectors and right["id"] in vectors else None,
                    ))
    rng.shuffle(continuations)
    add_category(additions, continuations, seen, 40, remaining)

    # Hard negatives: high embedding similarity but different current stories.
    ids = list(vectors)
    hard_negatives = []
    # Keep generation bounded: each sampled left article still compares with
    # the complete right-hand pool, so the candidates remain hard negatives.
    for left_id in rng.sample(ids, min(len(ids), 100)):
        best: list[tuple[float, int]] = []
        for right_id in ids:
            if left_id == right_id or articles[left_id]["story_id"] == articles[right_id]["story_id"]:
                continue
            score = cosine(vectors[left_id], vectors[right_id])
            if score >= 0.60:
                best.append((score, right_id))
        for score, right_id in sorted(best, reverse=True)[:5]:
            hard_negatives.append(record(
                articles[left_id], articles[right_id], "SAME_TOPIC_DISTINCT_EVENT",
                "hard_negative", "different current stories with cosine >= 0.60", score,
            ))
    rng.shuffle(hard_negatives)
    add_category(additions, hard_negatives, seen, 60, remaining)

    # Junk resistance: broken/thin body paired with a real article from another story.
    broken = [item for item in articles.values() if item["content_class"] in {"broken", "thin"}]
    real = [item for item in articles.values() if item["content_class"] == "ok"]
    rng.shuffle(broken)
    rng.shuffle(real)
    junk_pairs = []
    for left in broken:
        right = next((candidate for candidate in real if candidate["story_id"] != left["story_id"]), None)
        if right is not None:
            junk_pairs.append(record(
                left, right, "UNRELATED", "junk_resistance",
                "broken/thin body versus real article",
                cosine(vectors[left["id"]], vectors[right["id"]])
                if left["id"] in vectors and right["id"] in vectors else None,
            ))
    add_category(additions, junk_pairs, seen, 40, remaining)

    # If a database has fewer specialized examples, fill with distinct-story
    # pairs and mark them for review rather than pretending they are hard negatives.
    if len(existing) + len(additions) < args.target:
        fallback = []
        shuffled = list(articles.values())
        rng.shuffle(shuffled)
        for left in shuffled:
            right = next((candidate for candidate in shuffled if candidate["story_id"] != left["story_id"]), None)
            if right is not None:
                fallback.append(record(
                    left, right, "UNRELATED", "review_fill",
                    "database fallback pair; human review required",
                    cosine(vectors[left["id"]], vectors[right["id"]])
                    if left["id"] in vectors and right["id"] in vectors else None,
                ))
        add_unique(additions, fallback, seen, args.target - len(existing))

    result = existing + additions[: max(0, args.target - len(existing))]
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    counts: dict[str, int] = defaultdict(int)
    for item in result:
        counts[item.get("category", "unknown")] += 1
    print(json.dumps({"records": len(result), "added": len(result) - len(existing), "categories": dict(counts)}, indent=2))


if __name__ == "__main__":
    main()
