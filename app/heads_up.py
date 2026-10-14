""""Quick Heads Up" brief (TTL-cached); scheduled pushes live in reports.py."""
from __future__ import annotations

import logging
import threading
import time

from . import config, db, ollama, scoring

log = logging.getLogger("news.heads-up")

_cache: dict[str, float | str] = {}
_cache_lock = threading.Lock()
# /api/heads-up is public: at most one generation at a time, and a pause after failures.
_gen_lock = threading.Lock()
_FAILURE_BACKOFF_SECONDS = 60
_last_failure = 0.0


def brief(force: bool = False) -> tuple[str | None, bool]:
    """Return ``(text, cached)``. ``text`` is None when no brief can be produced.

    A stored brief younger than HEADS_UP_TTL_MINUTES is returned as-is unless
    ``force``. Failures are never cached. Non-forced callers never wait for or
    start a second generation; they get the last brief instead.
    """
    now = time.time()
    with _cache_lock:
        if not force and _cache and now - float(_cache["ts"]) < config.HEADS_UP_TTL_MINUTES * 60:
            return str(_cache["text"]), True
    if not force and now - _last_failure < _FAILURE_BACKOFF_SECONDS:
        return last_brief(), True
    if not _gen_lock.acquire(blocking=force):
        return last_brief(), True
    try:
        return _generate(now)
    finally:
        _gen_lock.release()


def _generate(now: float) -> tuple[str | None, bool]:
    global _last_failure
    with db.get_conn() as conn:
        top = scoring.ranked_stories(conn, personal=False)[:5]
    if not top:
        return None, False
    lines = []
    for item in top:
        s = item["story"]
        m = db.json_dict(s["metrics"])
        lines.append(f"- {s['headline']} | urgency={m.get('urgency_level')} "
                     f"status={m.get('status')} | {s['summary']}")
    try:
        text = ollama.chat(
            [
                {"role": "system", "content": "Write a concise 2-3 sentence news brief "
                                              "in plain text. Be factual, no preamble."},
                {"role": "user", "content": "Summarize these top stories for a reader:\n" + "\n".join(lines)},
            ],
            temperature=0.2,
        ).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("heads-up generation failed: %s", e)
        _last_failure = time.time()
        return None, False
    with _cache_lock:
        _cache.update(text=text, ts=now)
    return text, False


def last_brief() -> str | None:
    with _cache_lock:
        return str(_cache["text"]) if _cache else None
