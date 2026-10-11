"""Per-story chat: system-prompt story context + optional web-search turns.

Design (see findings.md, "Per-story chat"):
- The story's context goes into the SYSTEM prompt so the model stays anchored
  to that story rather than answering as a free-form generalist.
- Before the final answer, the agent may run up to ``search_turns`` search+
  scrape turns via searchngx to pull in fresh context. Each turn asks the model
  for one refined query (a constrained call), then folds the scraped text in.
- History is persisted per-story in the DB (ChatSession / ChatMessage).

The model calls use the same local CHAT_MODEL the pipeline already trusts.
"""
from __future__ import annotations

import logging

from . import config, ollama, searchngx

log = logging.getLogger("news.chat")

_SYSTEM_BASE = (
    "You are a helpful assistant answering questions about ONE specific news "
    "story. Ground your answer in that story. Be concise and factual; if "
    "something is beyond the story's scope or you are unsure, say so plainly. "
    "Answer in plain markdown."
)

_QUERY_SYSTEM = (
    "You are a web-search agent. Given the user's question about a news story "
    "and the research gathered so far, propose ONE specific web search query "
    "that would help answer it. If you already have enough context to answer, "
    "set done=true and leave query empty."
)


def _clamp_turns(requested: int) -> int:
    """Clamp a requested search-turn budget to the configured allowed set."""
    allowed = config.CHAT_SEARCH_TURNS
    if requested in allowed:
        return requested
    # Nearest valid option (0 when no web search requested, else the closest).
    if requested <= 0:
        return allowed[0] if allowed and allowed[0] == 0 else 0
    return min(allowed, key=lambda t: abs(t - requested)) if allowed else 0


def story_system_prompt(story: dict) -> str:
    """Build the per-story system prompt (story context, not free chat)."""
    m = story.get("metrics") or {}
    tags = story.get("tags") or []
    arts = story.get("articles") or []
    lines = []
    for a in arts:
        src = a.get("author") or ""
        url = a.get("url") or ""
        line = f"- {a.get('title') or '(untitled)'}"
        if src:
            line += f" ({src})"
        if url:
            line += f" [{url}]"
        lines.append(line)
    parts = [
        "STORY CONTEXT",
        f"Headline: {story.get('headline') or '(unknown)'}",
        f"Summary: {story.get('summary') or '(none)'}",
        f"Tags: {', '.join(tags) if tags else '(none)'}",
        f"Metrics: urgency {m.get('urgency_level', '?')}, "
        f"impact {m.get('impact_scale', '?')}, status {m.get('status', '?')}, "
        f"location {m.get('location', '?')}, language {m.get('language', '?')}",
        f"Quality score: {story.get('quality_score', '?')}",
        f"Articles in this story:\n" + "\n".join(lines),
        "",
        _SYSTEM_BASE,
    ]
    return "\n".join(parts)


def _story_brief(story: dict) -> str:
    """A compact one-paragraph recap of the story, for the search-query call."""
    m = story.get("metrics") or {}
    return (
        f"{story.get('headline') or '(unknown story)'} — "
        f"{(story.get('summary') or '')[:400]} "
        f"(status {m.get('status', '?')}, location {m.get('location', '?')})"
    )


def _next_query(story: dict, user_question: str, context_so_far: str) -> str | None:
    """Ask the model for the next search query. Returns None when done."""
    messages = [
        {"role": "system", "content": _QUERY_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Story: {_story_brief(story)}\n\n"
                f"User question: {user_question}\n\n"
                f"Research gathered so far:\n{context_so_far[-2500:] or '(none yet)'}"
            ),
        },
    ]
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "done": {"type": "boolean"},
        },
        "required": ["query", "done"],
    }
    try:
        res = ollama.chat_json_constrained(
            schema, messages, temperature=0.0, num_predict=200
        )
    except Exception as e:  # noqa: BLE001 — a failed query ends the search loop
        log.debug("chat search query call failed: %s", e)
        return None
    if res.get("done"):
        return None
    q = (res.get("query") or "").strip()
    return q or None


def _gather_context(story: dict, user_question: str, turns: int) -> str:
    """Run up to ``turns`` search+scrape turns; return accumulated context."""
    context = ""
    for _ in range(turns):
        if len(context) >= config.CHAT_CONTEXT_MAX:
            break
        query = _next_query(story, user_question, context)
        if not query:
            break
        results = searchngx.search(query)
        added = False
        for res in results:
            if len(context) >= config.CHAT_CONTEXT_MAX:
                break
            text = searchngx.scrape(res["url"]) or res.get("description", "")
            text = text[: config.CHAT_CONTEXT_PER_SOURCE]
            block = f"\n[{res.get('title') or res['url']}] ({res['url']})\n{text}"
            context += block
            added = True
        if not added:
            break
    return context


def generate(story: dict, history: list[dict], user_question: str, search_turns: int) -> str:
    """Produce the assistant reply for one chat turn.

    ``history`` is the prior messages (role/content) WITHOUT the new user
    message. ``search_turns`` is the requested web-search budget (clamped to
    the configured set). Returns the assistant text.
    """
    turns = _clamp_turns(search_turns)
    context = _gather_context(story, user_question, turns) if turns > 0 else ""

    system = story_system_prompt(story)
    if context:
        system += f"\n\nWEB RESEARCH (fresh, from searchngx — may help answer):\n{context}"

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for h in history:
        role = h.get("role")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": h.get("content", "")})
    messages.append({"role": "user", "content": user_question})

    return ollama.chat(
        messages, temperature=config.CHAT_TEMPERATURE, max_tokens=config.CHAT_MAX_TOKENS
    ).strip()
