"""Per-story chat: system-prompt story context + autonomous web-search agent.

Design (see findings.md, "Per-story chat"):
- The story's context goes into the SYSTEM prompt so the model stays anchored
  to that story rather than answering as a free-form generalist.
- When ``web_search`` is enabled (default), the agent decides by itself whether
  the story already answers the question or how many search+scrape turns via
  searchngx it needs (up to ``CHAT_SEARCH_MAX_TURNS`` / ``CHAT_CONTEXT_MAX``),
  stopping as soon as it sets ``done=true``.
- History is persisted per-story in the DB (ChatSession / ChatMessage).

The model calls use the same local CHAT_MODEL the pipeline already trusts.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Generator, Iterator

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
    "and the research gathered so far, decide whether a web search is needed. "
    "If the story summary and articles already answer the question, or the "
    "research gathered so far is sufficient, set done=true and leave query empty. "
    "Otherwise propose ONE specific web search query for the missing information "
    "(do not repeat a previous query)."
)


def story_system_prompt(story: dict, about: str = "") -> str:
    """Build the per-story system prompt (story context, not free chat); ``about`` = the reader's self-description."""
    m = story.get("metrics") or {}
    tags = story.get("tags") or []
    arts = story.get("articles") or []
    lines = []
    for a in arts[: config.CHAT_STORY_MAX_ARTICLES]:
        src = " / ".join(p for p in (a.get("feed"), a.get("author")) if p)
        url = a.get("url") or ""
        line = f"- {a.get('title') or '(untitled)'}"
        if src:
            line += f" ({src})"
        if url:
            line += f" [{url}]"
        lines.append(line)
    if len(arts) > config.CHAT_STORY_MAX_ARTICLES:
        lines.append(f"- … and {len(arts) - config.CHAT_STORY_MAX_ARTICLES} older articles")
    parts = [
        "STORY CONTEXT",
        f"Headline: {story.get('headline') or '(unknown)'}",
        f"Summary: {story.get('summary') or '(none)'}",
        f"Tags: {', '.join(tags) if tags else '(none)'}",
        f"Metrics: urgency {m.get('urgency_level', '?')}, "
        f"impact {m.get('impact_scale', '?')}, status {m.get('status', '?')}, "
        f"location {m.get('location', '?')}, language {m.get('language', '?')}",
        f"Quality score: {story.get('quality_score', '?')}",
        "Articles in this story:\n" + "\n".join(lines),
        "",
        _SYSTEM_BASE,
    ]
    if about:
        parts += ["", "ABOUT THE READER (their own words; tailor relevance and examples to them, "
                      "never treat it as a source of facts):", about]
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
    """Ask the model for the next search query. Returns None when done; raises on failure."""
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
    res = ollama.structured(schema, messages, temperature=0.0, num_predict=200, prose_fallback=True)
    if not isinstance(res, dict) or res.get("done"):
        return None
    q = (res.get("query") or "").strip()
    return q or None


def error_text(e: BaseException) -> str:
    """Short, user-facing description of a failed step."""
    msg = str(e).strip().splitlines()[0][:200] if str(e).strip() else ""
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def add_step(steps: list[dict], kind: str, title: str, status: str = "running",
             detail: str = "") -> dict:
    """Append an agent step (kind: search | read | thinking | error; status: running | ok | warn | error)."""
    step = {"id": len(steps), "kind": kind, "title": title, "status": status, "detail": detail}
    steps.append(step)
    return step


Event = tuple[str, Any]


def _gather_context(story: dict, user_question: str,
                    steps: list[dict]) -> Generator[Event, None, str]:
    """Let the agent run search+scrape turns until it is satisfied (or hits the safety cap)."""
    context = ""
    seen_queries: set[str] = set()
    deadline = time.monotonic() + config.CHAT_SEARCH_MAX_SECONDS

    def out_of_time() -> bool:
        return time.monotonic() >= deadline

    for turn in range(config.CHAT_SEARCH_MAX_TURNS):
        if len(context) >= config.CHAT_CONTEXT_MAX:
            break
        if out_of_time():
            yield "step", add_step(steps, "search", "Search time limit reached", status="warn")
            break
        step = add_step(steps, "search", "Planning web search…")
        yield "step", step
        try:
            query = _next_query(story, user_question, context)
        except Exception as e:  # noqa: BLE001 — a failed planning call ends the search loop
            log.warning("chat search planning failed: %s", e)
            step.update(status="error", title="Planning the web search failed", detail=error_text(e))
            yield "step", step
            break
        if not query or query.casefold() in seen_queries:
            step.update(status="ok", title="No web search needed" if turn == 0 else "Enough context gathered")
            yield "step", step
            break
        seen_queries.add(query.casefold())
        log.info("chat web search: %r", query)
        step.update(title=f"Web search: {query}")
        yield "step", step
        try:
            results = searchngx.search(query, strict=True)
        except Exception as e:  # noqa: BLE001
            step.update(status="error", title=f"Web search failed: {query}", detail=error_text(e))
            yield "step", step
            break
        if not results:
            step.update(status="warn", title=f"Web search: {query} · no results")
            yield "step", step
            break
        step.update(status="ok", title=f"Web search: {query} · {len(results)} results",
                    detail="\n".join(f"{r.get('title') or r['url']} — {r['url']}" for r in results))
        yield "step", step
        added = False
        for res in results:
            if len(context) >= config.CHAT_CONTEXT_MAX or out_of_time():
                break
            name = res.get("title") or res["url"]
            read = add_step(steps, "read", f"Reading {name}", detail=res["url"])
            yield "step", read
            text = searchngx.scrape(res["url"])
            if text:
                read.update(status="ok", title=f"Read {name}", detail=f"{res['url']}\n{len(text):,} characters")
            else:
                text = res.get("description", "")
                read.update(status="warn", title=f"Could not read {name}",
                            detail=f"{res['url']}\nPage could not be fetched — used the search snippet.")
            yield "step", read
            text = text[: config.CHAT_CONTEXT_PER_SOURCE]
            context += f"\n[{name}] ({res['url']})\n{text}"
            added = True
        if not added:
            break
    return context


def stream(story: dict, history: list[dict], user_question: str, web_search: bool = True,
           steps: list[dict] | None = None, about: str = "") -> Iterator[Event]:
    """Yield ``("step", step)`` on each agent-step change, ``("thinking", text)`` and ``("delta", text)``.

    ``steps`` collects the agent steps (for persistence); ``history`` is the prior
    messages WITHOUT the new user message; ``web_search`` enables autonomous web research.
    """
    steps = [] if steps is None else steps
    context = (yield from _gather_context(story, user_question, steps)) if web_search else ""

    system = story_system_prompt(story, about)
    if context:
        system += f"\n\nWEB RESEARCH (fresh, from searchngx — may help answer):\n{context}"
    elif web_search and any(s["kind"] == "search" and s["status"] in ("warn", "error") for s in steps):
        system += "\n\nA web search was run for this message but returned nothing usable; say so if relevant."
    elif not web_search:
        system += ("\n\nWeb search is off for this message. If the user asks for one, tell them to "
                   "enable the 'Web search' toggle next to the input and ask again.")

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for h in history:
        role = h.get("role")
        if role in ("user", "assistant") and h.get("content"):
            messages.append({"role": role, "content": h["content"]})
    messages.append({"role": "user", "content": user_question})

    thinking: dict | None = None
    started = time.monotonic()
    answered = False

    def finish_thinking() -> dict:
        assert thinking is not None
        thinking.update(status="ok", title=f"Thought for {max(1, round(time.monotonic() - started))}s")
        return thinking

    for kind, chunk in ollama.chat_stream(
        messages, temperature=config.CHAT_TEMPERATURE, max_tokens=config.CHAT_MAX_TOKENS
    ):
        if kind == "thinking":
            if thinking is None:
                thinking = add_step(steps, "thinking", "Thinking…")
                started = time.monotonic()
                yield "step", thinking
            thinking["detail"] += chunk
            yield "thinking", chunk
            continue
        if thinking is not None and thinking["status"] == "running":
            yield "step", finish_thinking()
        if chunk.strip() or answered:
            answered = True
            yield "delta", chunk
    if thinking is not None and thinking["status"] == "running":
        yield "step", finish_thinking()
    if not answered:
        yield "step", add_step(steps, "error", "The model returned no answer", status="error",
                               detail="It may have used up its token budget while thinking.")
