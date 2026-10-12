"""Prompt 2: generate / update a story's headline, summary, tags and metrics.

New stories (and revived ones) are summarized from all member articles;
multi-member stories are updated incrementally from the current summary plus
the new article (capped at 1,500 words).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
from typing import Any

from . import config, db, ollama
from .tags import normalize_tags

log = logging.getLogger("news.story")

_METRICS_GUIDE = (
    " Score the metrics against these anchors. urgency_level: "
    "1-2 evergreen, opinion, entertainment, personal anecdote or community chatter; "
    "3-4 routine news that stays relevant for days; "
    "5-6 notable development that matters today; "
    "7-8 major event with immediate consequences for many people "
    "(e.g. government decision taking effect, large disaster, market shock, major security incident); "
    "9-10 unfolding emergency or direct threat to life and safety right now. "
    "Most stories are 2-5; reserve 8+ for live crises. "
    "impact_scale: Local = one city, community or individual; "
    "Regional = a state/province or a few neighbouring countries; "
    "National = a whole country; Global = several continents or the world economy. "
    "status: Breaking = first reports, still unfolding within hours; "
    "Ongoing = developing over days; Resolved = concluded event, recap, analysis or explainer."
)
_SYSTEM_FULL = (
    "You are a news editor. Given the source articles of one ongoing story, respond "
    "with ONLY a JSON object: "
    '{"headline": str, "summary": str, "tags": [...], "quality": int, '
    '"location": str, "language": str, '
    '"metrics": {"urgency_level": int 1-10, "impact_scale": "Local|Regional|National|Global", '
    '"status": "Breaking|Ongoing|Resolved"}}. '
    "location is the most specific place the news is about as 'City, Country' "
    "(e.g. 'Munich, Germany'), or 'Global' when it has no specific local setting. "
    "language is the two-letter code of the articles' main language (e.g. 'de', 'en'). "
    "Summary is 2-4 factual sentences. Do not invent facts not present in the articles. "
    "Reader comments (a 'Top comments:' list) are opinions: never state them as facts; "
    "mention a notable view only as what commenters say."
    + _METRICS_GUIDE
)
_SYSTEM_INCREMENTAL = (
    "You are updating an active news story. Review the Current Summary and the "
    "New Article. If the New Article introduces material updates, rewrite the "
    "summary in 2-4 factual sentences to incorporate them. If it adds no new "
    "facts, return the Current Summary exactly as written. Preserve the story "
    "headline unless the new facts require a clearer headline. Return the same "
    "JSON object schema as requested. location/language describe the story's "
    "setting — return them as-is unless the new article clearly changes it. "
    "Reader comments (a 'Top comments:' list) are opinions, not facts."
    + _METRICS_GUIDE
)
IMPACTS = ("Local", "Regional", "National", "Global")
STATUSES = ("Breaking", "Ongoing", "Resolved")
_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "quality": {"type": "integer", "minimum": 1, "maximum": 10},
        "location": {"type": "string"},
        "language": {"type": "string"},
        "metrics": {
            "type": "object",
            "properties": {
                "urgency_level": {"type": "integer", "minimum": 1, "maximum": 10},
                "impact_scale": {"type": "string", "enum": list(IMPACTS)},
                "status": {"type": "string", "enum": list(STATUSES)},
            },
            "required": ["urgency_level", "impact_scale", "status"],
        },
    },
    "required": ["headline", "summary", "tags", "quality", "metrics"],
}
_INCREMENTAL_MAX_WORDS = 1500
_SENTENCE_SPLIT = re.compile(r"[.!?]+\s*")
_LANG_RE = re.compile(r"[a-z]{2,3}(-[a-z]{2,4})?")


def _article_text(a: dict[str, Any]) -> str:
    return a.get("content_raw") or a.get("title") or ""


def _combined_text(articles: list[dict[str, Any]]) -> str:
    """Newest first; once over the budget, older articles shrink to one-line bullets."""
    parts: list[str] = []
    used = 0
    for i, a in enumerate(reversed(articles)):
        body = _article_text(a)
        if i > 0 and used + len(body) > config.CONTEXT_BUDGET_CHARS:
            part = f"- {a.get('title')} :: {' '.join(body.split())[:400]}"
        else:
            part = f"### {a.get('title') or 'untitled'}\n{body}"
        parts.append(part)
        used += len(part)
    return "\n\n".join(parts)


def sanitize_metrics(metrics: Any) -> dict[str, Any]:
    """Clamp/default Prompt2 metrics so the UI and breaking check see clean values."""
    m = dict(metrics) if isinstance(metrics, dict) else {}
    try:
        urgency = int(m.get("urgency_level"))
    except (TypeError, ValueError):
        urgency = None
    if urgency is None or not 1 <= urgency <= 10:
        if m.get("urgency_level") is not None:
            log.warning("prompt2: invalid urgency_level %r -> 5", m.get("urgency_level"))
        urgency = 5
    m["urgency_level"] = urgency
    if m.get("impact_scale") not in IMPACTS:
        if m.get("impact_scale") is not None:
            log.warning("prompt2: invalid impact_scale %r -> National", m.get("impact_scale"))
        m["impact_scale"] = "National"
    if m.get("status") not in STATUSES:
        if m.get("status") is not None:
            log.warning("prompt2: invalid status %r -> Ongoing", m.get("status"))
        m["status"] = "Ongoing"
    return m


def _log_violations(story_id: int, out: dict[str, Any]) -> None:
    headline = (out.get("headline") or "").strip()
    summary = (out.get("summary") or "").strip()
    problems = []
    if not headline:
        problems.append("empty headline")
    elif len(headline) > 200:
        problems.append(f"headline too long ({len(headline)} chars)")
    if not summary:
        problems.append("empty summary")
    else:
        n = len([s for s in _SENTENCE_SPLIT.split(summary) if s.strip()])
        if not 1 <= n <= 6:
            problems.append(f"summary outside 1-6 sentences ({n})")
    if problems:
        log.warning("prompt2 violation for story %s: %s | raw=%s", story_id,
                    "; ".join(problems), json.dumps(out, ensure_ascii=False)[:400])


def _build_prompt(story: sqlite3.Row | None, arts: list[dict[str, Any]],
                  article_id: int | None, force_full: bool) -> tuple[str, str]:
    if len(arts) >= 2 and story is not None and not force_full:
        new = next((a for a in arts if a.get("id") == article_id), arts[-1])
        body = " ".join(_article_text(new).split()[:_INCREMENTAL_MAX_WORDS])
        user = (
            f"Current Headline:\n{story['headline'] or new.get('title', '')}\n\n"
            f"Current Summary:\n{story['summary'] or '(none)'}\n\n"
            f"New Article Title:\n{new.get('title', '')}\n\n"
            f"New Article Text:\n{body}"
        )
        return _SYSTEM_INCREMENTAL, user
    titles = "\n".join(f"- {a.get('title')}" for a in arts)
    return _SYSTEM_FULL, f"Attached source articles:\n{titles}\n\nFull article text:\n{_combined_text(arts)}"


def update_story(conn: sqlite3.Connection, story_id: int, *, breaking: bool = False,
                 article_id: int | None = None, force_full: bool = False) -> int:
    """Rewrite the story from its articles. Returns the prompt size in chars (0 = nothing to do)."""
    arts = [dict(a) for a in conn.execute(
        "SELECT * FROM Article WHERE story_id = ? ORDER BY published_date ASC", (story_id,)
    )]
    if not arts:
        return 0
    story = conn.execute("SELECT headline, summary, metrics FROM Story WHERE id = ?", (story_id,)).fetchone()
    system, user = _build_prompt(story, arts, article_id, force_full)

    out = ollama.structured(
        _SCHEMA,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        prose_fallback=True,
    )
    if isinstance(out, list):  # prose fallback may wrap the object in an array
        out = next((x for x in out if isinstance(x, dict)), None)
    if not isinstance(out, dict):
        raise ValueError(f"Prompt2 returned non-object JSON: {str(out)[:200]!r}")
    _log_violations(story_id, out)

    metrics = sanitize_metrics(out.get("metrics"))
    if breaking:
        metrics["status"] = "Breaking"
    prev = db.json_dict(story["metrics"]) if story is not None else {}
    # Optional fields: an omission keeps what the story already had.
    location = str(out.get("location") or "").strip()[:120] or str(prev.get("location") or "")[:120]
    language = str(out.get("language") or "").strip().lower()
    if not _LANG_RE.fullmatch(language):
        language = str(prev.get("language") or "")
    if location:
        metrics["location"] = location
    if language:
        metrics["language"] = language

    raw_tags = out.get("tags") if isinstance(out.get("tags"), list) else []
    tags = normalize_tags([t for t in raw_tags if isinstance(t, str)])[:6]
    # Median of member qualities: one bot-blocked fetch can't drag a story down.
    quals = [a["quality"] for a in arts if a.get("quality") is not None]
    if quals:
        quality = round(float(statistics.median(quals)), 2)
    else:
        try:
            quality = max(1.0, min(10.0, float(out.get("quality", 5))))
        except (TypeError, ValueError):
            quality = 5.0
    headline = (out.get("headline") or "").strip() or arts[-1].get("title") or "Untitled"

    conn.execute(
        "UPDATE Story SET headline=?, summary=?, tags=?, metrics=?, quality_score=?, "
        "updated_at=datetime('now') WHERE id=?",
        (headline, (out.get("summary") or "").strip(), json.dumps(tags),
         json.dumps(metrics), quality, story_id),
    )
    return len(user)
