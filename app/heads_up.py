""""Quick Heads Up" briefs, TTL-cached per view; scheduled pushes live in reports.py.

One brief per view key: "default" (the public ranking) and "personal" (the
signed-in user's ranking). /api/heads-up never waits for the LLM: a stale
brief starts one background generation and the caller gets the previous text
with ``generating=True``.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Any

from . import config, db, ollama, scoring

log = logging.getLogger("news.heads-up")

DEFAULT = "default"
PERSONAL = "personal"

_cache: dict[str, dict[str, Any]] = {}  # key -> {"text", "ts"}
_cache_lock = threading.Lock()
# /api/heads-up is public: one generation at a time, one queued per key, a pause after failures.
_gen_lock = threading.Lock()
_pending: set[str] = set()
_FAILURE_BACKOFF_SECONDS = 60
_last_failure: dict[str, float] = {}


def _fresh(key: str, now: float) -> bool:
    entry = _cache.get(key)
    return entry is not None and now - entry["ts"] < config.HEADS_UP_TTL_MINUTES * 60


def brief(force: bool = False, key: str = DEFAULT) -> tuple[str | None, bool]:
    """Return ``(text, cached)``, generating synchronously when stale; ``text`` is None if none can be made.

    A stored brief younger than HEADS_UP_TTL_MINUTES is returned as-is unless
    ``force``. Failures are never cached. Non-forced callers never wait for or
    start a second generation; they get the last brief instead.
    """
    now = time.time()
    with _cache_lock:
        if not force and _fresh(key, now):
            return str(_cache[key]["text"]), True
        if not force and now - _last_failure.get(key, 0.0) < _FAILURE_BACKOFF_SECONDS:
            return _text(key), True
    if not _gen_lock.acquire(blocking=force):
        return last_brief(key), True
    try:
        return _generate(key, now)
    finally:
        _gen_lock.release()


def status(key: str = DEFAULT) -> dict[str, Any]:
    """The view's current brief; a stale one starts a background refresh (never waits)."""
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        start = (not _fresh(key, now) and key not in _pending
                 and now - _last_failure.get(key, 0.0) >= _FAILURE_BACKOFF_SECONDS)
        if start:
            _pending.add(key)
        generating = key in _pending
    if start:
        threading.Thread(target=_background, args=(key,), name=f"heads-up-{key}", daemon=True).start()
    return {
        "text": entry["text"] if entry else None,
        "generating": generating,
        "generated_at": (dt.datetime.fromtimestamp(entry["ts"], dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
                         if entry else None),
    }


def _background(key: str) -> None:
    try:
        with _gen_lock:
            if not _fresh(key, time.time()):
                _generate(key, time.time())
    finally:
        with _cache_lock:
            _pending.discard(key)


def _generate(key: str, now: float) -> tuple[str | None, bool]:
    with db.get_conn() as conn:
        top = scoring.ranked_stories(conn, personal=key != DEFAULT)[:5]
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
        log.warning("heads-up generation (%s) failed: %s", key, e)
        with _cache_lock:
            _last_failure[key] = time.time()
        return None, False
    with _cache_lock:
        _cache[key] = {"text": text, "ts": now}
    return text, False


def _text(key: str) -> str | None:
    entry = _cache.get(key)
    return str(entry["text"]) if entry else None


def last_brief(key: str = DEFAULT) -> str | None:
    with _cache_lock:
        return _text(key)
