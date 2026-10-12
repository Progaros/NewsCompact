""""Quick Heads Up" brief (TTL-cached) and its scheduled ntfy push."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

import httpx

from . import config, db, ollama, scoring

log = logging.getLogger("news.heads-up")

_cache: dict[str, float | str] = {}
_cache_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_POLL_SECONDS = 15
_CATCHUP_MINUTES = 5  # a scheduled time still fires within this window


def brief(force: bool = False) -> tuple[str | None, bool]:
    """Return ``(text, cached)``. ``text`` is None when no brief can be produced.

    A stored brief younger than HEADS_UP_TTL_MINUTES is returned as-is unless
    ``force``. Failures are never cached.
    """
    now = time.time()
    with _cache_lock:
        if not force and _cache and now - float(_cache["ts"]) < config.HEADS_UP_TTL_MINUTES * 60:
            return str(_cache["text"]), True

    with db.get_conn() as conn:
        top = scoring.ranked_stories(conn)[:5]
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
        return None, False
    with _cache_lock:
        _cache.update(text=text, ts=now)
    return text, False


def last_brief() -> str | None:
    with _cache_lock:
        return str(_cache["text"]) if _cache else None


def _send_ntfy(text: str) -> None:
    try:
        r = httpx.post(config.HEADS_UP_NTFY_URL, content=text.encode("utf-8"),
                       headers={"Title": config.HEADS_UP_NTFY_TITLE}, timeout=15.0)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("ntfy push to %s failed: %s", config.HEADS_UP_NTFY_URL, e)


def _minutes(hhmm: str) -> int | None:
    try:
        h, m = (int(p) for p in hhmm.split(":"))
    except ValueError:
        return None
    return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None


def _tick(fired: dict[str, str]) -> None:
    now = dt.datetime.now()
    today, now_min = now.date().isoformat(), now.hour * 60 + now.minute
    for t in config.HEADS_UP_NTFY_TIMES:
        t_min = _minutes(t)
        if t_min is None:
            continue
        if not 0 <= now_min - t_min <= _CATCHUP_MINUTES or fired.get(t) == today:
            continue
        fired[t] = today
        text = brief(force=True)[0] or last_brief()
        if text:
            _send_ntfy(text)
            log.info("pushed scheduled brief (%s)", t)
        else:
            log.info("skip scheduled push at %s (no brief)", t)


def _loop() -> None:
    fired: dict[str, str] = {}
    while not _stop.is_set():
        try:
            _tick(fired)
        except Exception:  # noqa: BLE001
            log.exception("heads-up scheduler tick failed")
        _stop.wait(_POLL_SECONDS)


def start_scheduler() -> None:
    global _thread
    if _thread is not None or not config.HEADS_UP_NTFY_URL or not config.HEADS_UP_NTFY_TIMES:
        return
    for t in config.HEADS_UP_NTFY_TIMES:
        if _minutes(t) is None:
            log.warning("ignoring malformed NEWS_HEADS_UP_NTFY_TIMES entry %r", t)
    _thread = threading.Thread(target=_loop, name="news-heads-up", daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop.set()
