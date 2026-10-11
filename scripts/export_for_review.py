"""Export unreviewed golden-set candidates as compact Markdown.

Usage:
    PYTHONPATH=. uv run python scripts/export_for_review.py
    PYTHONPATH=. uv run python scripts/export_for_review.py \
        --input benchmark_pairs_200.json --output review_candidates.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402


def snippet(text: str | None, words: int = 50) -> str:
    """Return one-line text capped at exactly the requested word budget."""
    clean = " ".join((text or "").split())
    return " ".join(clean.split()[:words]) or "(no text available)"


def one_line(text: str | None) -> str:
    return " ".join((text or "").split()) or "(untitled)"


def load_rows(conn, pairs: list[dict[str, Any]]) -> tuple[dict[int, dict], dict[int, dict]]:
    article_ids = sorted({int(pair["new_article_id"]) for pair in pairs})
    story_ids = sorted({int(pair["candidate_story_id"]) for pair in pairs})
    articles: dict[int, dict] = {}
    stories: dict[int, dict] = {}
    if article_ids:
        placeholders = ",".join("?" for _ in article_ids)
        rows = conn.execute(
            f"SELECT id, title, content_raw FROM Article WHERE id IN ({placeholders})",
            article_ids,
        ).fetchall()
        articles = {row["id"]: dict(row) for row in rows}
    if story_ids:
        placeholders = ",".join("?" for _ in story_ids)
        rows = conn.execute(
            f"SELECT id, headline, summary FROM Story WHERE id IN ({placeholders})",
            story_ids,
        ).fetchall()
        stories = {row["id"]: dict(row) for row in rows}
    return articles, stories


def render(pairs: list[dict[str, Any]], articles: dict[int, dict], stories: dict[int, dict]) -> str:
    sections: list[str] = []
    for index, pair in enumerate(pairs, 1):
        article_id = int(pair["new_article_id"])
        story_id = int(pair["candidate_story_id"])
        article = articles.get(article_id, {})
        story = stories.get(story_id, {})
        classification = pair.get("expected_classification") or "UNREVIEWED"
        merge = pair.get("expected_merge")
        sections.append(
            f"### Pair {index} | {pair.get('category', 'uncategorized')}\n"
            f"**Auto-Label:** {classification} | **Merge:** {merge}\n"
            f"* **Article {article_id}:** {one_line(article.get('title'))}\n"
            f"  *Snippet:* {snippet(article.get('content_raw'))}\n"
            f"* **Story {story_id}:** {one_line(story.get('headline'))}\n"
            f"  *Snippet:* {snippet(story.get('summary'))}\n\n"
            "---"
        )
    return "\n\n".join(sections) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("benchmark_pairs_200.json"))
    parser.add_argument("--output", type=Path, default=Path("review_candidates.md"))
    args = parser.parse_args()

    records = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("input JSON must be an array")
    pairs = [pair for pair in records if pair.get("provenance") == "database_mined"]
    if not pairs:
        raise ValueError("no database-mined review candidates found")

    with db.get_conn() as conn:
        articles, stories = load_rows(conn, pairs)
    missing_articles = sorted({int(pair["new_article_id"]) for pair in pairs} - set(articles))
    missing_stories = sorted({int(pair["candidate_story_id"]) for pair in pairs} - set(stories))
    if missing_articles or missing_stories:
        raise ValueError(
            f"missing database rows: articles={missing_articles[:5]} stories={missing_stories[:5]}"
        )

    args.output.write_text(render(pairs, articles, stories), encoding="utf-8")
    print(f"review_candidates.md ready: {len(pairs)} pairs written to {args.output}")


if __name__ == "__main__":
    main()
