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

from . import config, db, ollama, scoring
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
    "The headline names the story's single core event; never join separate events "
    "with ';'. Summary is 2-4 factual sentences. Do not invent facts not present in the articles. "
    "Reader comments (a 'Top comments:' list) are opinions: never state them as facts; "
    "mention a notable view only as what commenters say."
    + _METRICS_GUIDE
)
_SIGNIFICANCE_GUIDE = (
    " new_facts lists, before anything else, each concrete fact the New Article reports "
    "that the Current Summary does not already state or imply (a new event, figure, "
    "decision or official statement); background, recaps, rewording, analysis, opinion "
    "and reactions are not new facts. Return [] when there are none — most follow-up "
    "articles add none. update_significance rates what the New Article adds for a reader "
    "who already knows the Current Summary: 0 = nothing new (repetition, rewording, "
    "background, commentary); 1 = minor detail, reaction or opinion; "
    "2 = notable development that changes what a reader knows (new decision, outcome, "
    "casualties, major new figures); 3 = major change (escalation, outcome, reversal, "
    "the story's core changes). When unsure, choose the lower value."
)
_SYSTEM_INCREMENTAL = (
    "You are updating an active news story. Review the Current Summary and the "
    "New Article. If the New Article introduces material updates, rewrite the "
    "summary in 2-4 factual sentences to incorporate them while keeping the key "
    "facts already in it. If it adds no new "
    "facts, return the Current Summary exactly as written. Preserve the story "
    "headline: it names the story's core event, not the latest angle or reaction, "
    "and never joins separate events with ';'. Return the same "
    "JSON object schema as requested. location/language describe the story's "
    "setting — return them as-is unless the new article clearly changes it. "
    "Reader comments (a 'Top comments:' list) are opinions, not facts."
    + _SIGNIFICANCE_GUIDE
    + _METRICS_GUIDE
)
# Revived story: full rewrite, but rated against the summary readers already saw.
_SYSTEM_REVIVE = (
    _SYSTEM_FULL
    + " Also return new_facts and update_significance for the New Article named below,"
    " judged against the Current Summary."
    + _SIGNIFICANCE_GUIDE
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
# new_facts comes first so the rating is grounded in an explicit list.
_RATED_SCHEMA = {
    **_SCHEMA,
    "properties": {"new_facts": {"type": "array", "items": {"type": "string"}},
                   **_SCHEMA["properties"],
                   "update_significance": {"type": "integer", "minimum": 0, "maximum": 3}},
    "required": ["new_facts", *_SCHEMA["required"], "update_significance"],
}
SIGNIFICANCE_MAX = 3
# Escalations only count when the story reaches this urgency ("major event" anchor).
_ESCALATION_MIN_URGENCY = 7
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
    if ";" in headline:
        problems.append("multi-event headline (';')")
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
    user = f"Attached source articles:\n{titles}\n\nFull article text:\n{_combined_text(arts)}"
    new = next((a for a in arts if a.get("id") == article_id), None) if article_id else None
    if new is not None and story is not None and story["summary"]:
        user += (f"\n\nCurrent Summary (what readers already know):\n{story['summary']}\n\n"
                 f"New Article Title:\n{new.get('title', '')}")
        return _SYSTEM_REVIVE, user
    return _SYSTEM_FULL, user


def update_story(conn: sqlite3.Connection, story_id: int, *, breaking: bool = False,
                 article_id: int | None = None, force_full: bool = False) -> int:
    """Rewrite the story from its articles. Returns the prompt size in chars (0 = nothing to do)."""
    arts = [dict(a) for a in conn.execute(
        "SELECT a.*, COALESCE(f.category, '') AS feed_category FROM Article a JOIN Feed f ON f.id = a.feed_id "
        "WHERE a.story_id = ? ORDER BY a.published_date ASC", (story_id,)
    )]
    if not arts:
        return 0
    story = conn.execute("SELECT headline, summary, metrics FROM Story WHERE id = ?", (story_id,)).fetchone()
    system, user = _build_prompt(story, arts, article_id, force_full)
    rated = system is not _SYSTEM_FULL
    prev_summary = (story["summary"] or "") if story is not None else ""

    # A malformed reply is retried once here instead of re-running the whole article later.
    for attempt in range(2):
        try:
            out = ollama.structured(
                _RATED_SCHEMA if rated else _SCHEMA,
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.2,
                prose_fallback=True,
            )
            if isinstance(out, list):  # prose fallback may wrap the object in an array
                out = next((x for x in out if isinstance(x, dict)), None)
            if not isinstance(out, dict):
                raise ValueError(f"Prompt2 returned non-object JSON: {str(out)[:200]!r}")
            break
        except ValueError as e:
            if attempt:
                raise
            log.warning("Prompt2 reply unusable for story %s, retrying once: %s", story_id, e)
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
    # Trust-weighted median: one bot-blocked fetch or a pile of low-trust reposts can't drag it down.
    quals = [(a["quality"], config.SOURCE_TRUST.get(a["feed_category"], config.SOURCE_TRUST_DEFAULT))
             for a in arts if a.get("quality") is not None]
    if quals:
        quality = scoring.weighted_median(quals)
        quality = round(float(quality if quality is not None else statistics.median(q for q, _ in quals)), 2)
    else:
        try:
            quality = max(1.0, min(10.0, float(out.get("quality", 5))))
        except (TypeError, ValueError):
            quality = 5.0
    headline = (out.get("headline") or "").strip() or arts[-1].get("title") or "Untitled"
    significance = update_significance(out, prev, metrics, rated, prev_summary)
    if rated:
        facts = out.get("new_facts")
        log.info("story %s update significance=%d (model=%s new_facts=%s)", story_id, significance,
                 out.get("update_significance"), len(facts) if isinstance(facts, list) else "-")

    conn.execute(
        "UPDATE Story SET headline=?, summary=?, tags=?, metrics=?, quality_score=?, "
        "updated_at=datetime('now'), "
        "news_at=CASE WHEN ? THEN datetime('now') ELSE COALESCE(news_at, datetime('now')) END, "
        "material_at=CASE WHEN ? THEN datetime('now') ELSE material_at END WHERE id=?",
        (headline, (out.get("summary") or "").strip(), json.dumps(tags),
         json.dumps(metrics), quality, significance >= 1,
         significance >= config.READ_RESURFACE_SIGNIFICANCE, story_id),
    )
    return len(user)


def _shows_breaking(m: dict[str, Any]) -> bool:
    """Same rule as the UI's Breaking badge."""
    try:
        return m.get("status") == "Breaking" and int(m.get("urgency_level")) >= 8
    except (TypeError, ValueError):
        return False


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def update_significance(out: dict[str, Any], prev: dict[str, Any], metrics: dict[str, Any],
                        rated: bool, prev_summary: str = "") -> int:
    """0-3: how much this rewrite matters to a reader who already read the story.

    Unrated rewrites count as major for a new story and as nothing for an existing
    one (merge, rescore). Rated ones with no new facts or an unchanged summary are 0;
    otherwise the model's level, raised to major on a real escalation.
    """
    if not rated:
        return 0 if prev_summary else SIGNIFICANCE_MAX
    facts = out.get("new_facts")
    if isinstance(facts, list) and not any(isinstance(f, str) and f.strip() for f in facts):
        return 0
    if prev_summary and _norm(out.get("summary") or "") == _norm(prev_summary):
        return 0
    try:
        level = max(0, min(SIGNIFICANCE_MAX, int(out.get("update_significance", 1))))
    except (TypeError, ValueError):
        level = 1
    try:
        prev_urgency = int(prev.get("urgency_level"))
    except (TypeError, ValueError):
        prev_urgency = metrics["urgency_level"]
    escalated = ((metrics["urgency_level"] - prev_urgency >= 2
                  and metrics["urgency_level"] >= _ESCALATION_MIN_URGENCY)
                 or (_shows_breaking(metrics) and not _shows_breaking(prev)))
    return SIGNIFICANCE_MAX if escalated else level
