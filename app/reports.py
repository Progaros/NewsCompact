"""Scheduled ntfy reports: the news brief and a daily pipeline report.

Schedules are edited in ⚙ Preferences and stored in
``UserProfile.preferences["ntfy_reports"]``::

    [{"kind": "brief" | "pipeline", "time": "HH:MM", "days": [0..6], "enabled": bool}]

``days`` uses Monday = 0; times are server-local. Until that key is saved, the
NEWS_HEADS_UP_NTFY_TIMES env list applies as a daily brief. Each schedule
fires once per day within a short catch-up window (a time missed while the
app was down does not fire later).
"""
from __future__ import annotations

import datetime as dt
import logging
import statistics
import threading
from typing import Any

import httpx

from . import config, db, heads_up, pipeline, scoring

log = logging.getLogger("news.reports")

KINDS = {"brief": "News Heads Up", "pipeline": "News Pipeline Report"}
ALL_DAYS = list(range(7))
_POLL_SECONDS = 15
_CATCHUP_MINUTES = 5
_stop = threading.Event()
_thread: threading.Thread | None = None


def schedules(profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    prefs = (profile or scoring.get_user_profile())["preferences"]
    if isinstance(prefs.get("ntfy_reports"), list):
        return [s for s in prefs["ntfy_reports"] if isinstance(s, dict)]
    return [{"kind": "brief", "time": t, "days": ALL_DAYS, "enabled": True} for t in config.HEADS_UP_NTFY_TIMES]


# --- report texts ------------------------------------------------------------
def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.1f}m" if seconds < 7200 else f"{seconds / 3600:.1f}h"


def pipeline_report(conn, hours: int = 24) -> str:
    """Plain-text health summary of the last ``hours`` (phone-sized)."""
    since = f"-{int(hours)} hours"
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    ingested = one("SELECT count(*) FROM Article WHERE created_at >= datetime('now', ?)", since)
    processed = conn.execute(
        "SELECT t_wall_s, (julianday(processed_at) - julianday(created_at)) * 86400, quality FROM Article "
        "WHERE processed_at >= datetime('now', ?) AND duplicate_of IS NULL", (since,)).fetchall()
    pending = pipeline.pending_count(conn)
    dead = one("SELECT count(*) FROM Article WHERE pipeline_failures >= ?", config.PIPELINE_MAX_FAILURES)
    new_stories = conn.execute(
        "SELECT (SELECT count(*) FROM Article a WHERE a.story_id = s.id), s.metrics, s.headline FROM Story s "
        "WHERE s.created_at >= datetime('now', ?) AND s.headline IS NOT NULL", (since,)).fetchall()
    singles = sum(1 for n, _m, _h in new_stories if n <= 1)
    breaking = sum(1 for _n, m, _h in new_stories if db.json_dict(m).get("status") == "Breaking")
    multi_event = one("SELECT count(*) FROM Story WHERE headline LIKE '%;%' AND updated_at >= datetime('now', ?)", since)
    failing = [r[0] for r in conn.execute("SELECT title FROM Feed WHERE enabled = 1 AND last_error IS NOT NULL")]

    n = len(processed)
    quals = [r[2] for r in processed if r[2] is not None]
    join_rate = max(0.0, 1 - len(new_stories) / n) if n else 0.0
    lines = [
        f"Last {hours} h",
        f"Articles: {ingested} new · {n} processed · {pending} queued · {dead} dead letter",
        f"Stories: {len(new_stories)} new ({singles / len(new_stories):.0%} single-article) · join rate {join_rate:.0%}"
        if new_stories else "Stories: none new",
        f"Speed: work {_dur(_median([r[0] for r in processed if r[0] is not None]))} · "
        f"latency {_dur(_median([r[1] for r in processed if r[1] is not None]))} (median)",
    ]
    if quals:
        lines.append(f"Quality: {sum(q <= 3 for q in quals) / len(quals):.0%} low (≤3) · "
                     f"{sum(q >= 7 for q in quals) / len(quals):.0%} high (≥7)")
    lines.append(f"Breaking: {breaking} · multi-event headlines: {multi_event}")
    if failing:
        more = f" +{len(failing) - 3}" if len(failing) > 3 else ""
        lines.append(f"Failing feeds: {len(failing)} ({', '.join(failing[:3])}{more})")
    top = scoring.ranked_stories(conn)[:3]
    if top:
        lines.append("Top: " + " | ".join(item["story"]["headline"] for item in top))
    return "\n".join(lines)


def render(kind: str, fresh: bool = True) -> str | None:
    if kind == "brief":
        return heads_up.brief(force=fresh)[0] or heads_up.last_brief()
    with db.get_conn() as conn:
        return pipeline_report(conn)


def _send_ntfy(text: str, title: str) -> bool:
    try:
        r = httpx.post(config.HEADS_UP_NTFY_URL, content=text.encode("utf-8"),
                       headers={"Title": title.encode("utf-8")}, timeout=15.0)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("ntfy push to %s failed: %s", config.HEADS_UP_NTFY_URL, e)
        return False


def send(kind: str) -> tuple[bool, str | None]:
    """Render and push one report now; returns ``(sent, text)``."""
    text = render(kind)
    if not text or not config.HEADS_UP_NTFY_URL:
        return False, text
    title = config.HEADS_UP_NTFY_TITLE if kind == "brief" else KINDS[kind]
    return _send_ntfy(text, title), text


# --- scheduler ---------------------------------------------------------------
def _minutes(hhmm: str) -> int | None:
    try:
        h, m = (int(p) for p in str(hhmm).split(":"))
    except ValueError:
        return None
    return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None


def due(schedule: dict[str, Any], now: dt.datetime) -> bool:
    t_min = _minutes(schedule.get("time", ""))
    return (
        t_min is not None
        and schedule.get("kind") in KINDS
        and bool(schedule.get("enabled", True))
        and now.weekday() in (schedule.get("days") or [])
        and 0 <= now.hour * 60 + now.minute - t_min <= _CATCHUP_MINUTES
    )


def tick(fired: dict[str, str], now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now()
    today = now.date().isoformat()
    for s in schedules():
        key = f"{s.get('kind')}@{s.get('time')}"
        if not due(s, now) or fired.get(key) == today:
            continue
        fired[key] = today
        sent, text = send(s["kind"])
        log.info("scheduled %s report at %s: %s", s["kind"], s["time"],
                 "sent" if sent else "skipped (no text)" if not text else "push failed")


def _loop() -> None:
    fired: dict[str, str] = {}
    while not _stop.is_set():
        try:
            tick(fired)
        except Exception:  # noqa: BLE001
            log.exception("report scheduler tick failed")
        _stop.wait(_POLL_SECONDS)


def start_scheduler() -> None:
    global _thread
    if _thread is not None or not config.HEADS_UP_NTFY_URL:
        return
    _thread = threading.Thread(target=_loop, name="news-reports", daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop.set()
