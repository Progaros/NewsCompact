"""Scheduled ntfy reports: the news brief and a daily pipeline report.

Schedules are edited in ⚙ Preferences by admins and stored in their
``UserProfile.preferences["ntfy_reports"]`` (the brief is that admin's personal one)::

    [{"kind": "brief" | "pipeline", "time": "HH:MM", "days": [0..6], "enabled": bool}]

``days`` uses Monday = 0; times are server-local. Until that key is saved, the
NEWS_HEADS_UP_NTFY_TIMES env list applies as a daily brief (of the first admin only). Each
schedule fires once per day within a short catch-up window (a time missed while the
app was down does not fire later); ReportSent keeps a restart or a second process from
sending it again.
"""
from __future__ import annotations

import datetime as dt
import logging
import statistics
import threading
from typing import Any

import httpx

from . import config, db, heads_up, pipeline, podcast, scoring

log = logging.getLogger("news.reports")

KINDS = {"brief": "News Heads Up", "pipeline": "News Pipeline Report", "podcast": "Daily Podcast"}
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


def render(
    kind: str,
    fresh: bool = True,
    user_id: int = 1,
    *,
    view: str | None = None,
    max_stories: int | None = None,
) -> str | None:
    if kind == "brief":
        key = heads_up.DEFAULT if view == "default" else heads_up.user_key(user_id)
        return heads_up.brief(force=fresh, key=key, max_stories=max_stories or 5)[0] or heads_up.last_brief(key)
    with db.get_conn() as conn:
        return pipeline_report(conn)


def _send_ntfy(text: str, title: str, *, click_url: str | None = None) -> bool:
    headers: dict[str, Any] = {"Title": title.encode("utf-8")}
    if click_url:
        headers["Click"] = click_url
    try:
        r = httpx.post(config.HEADS_UP_NTFY_URL, content=text.encode("utf-8"),
                       headers=headers, timeout=15.0)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("ntfy push to %s failed: %s", config.HEADS_UP_NTFY_URL, e)
        return False


def push_ntfy(text: str, title: str, *, click_url: str | None = None) -> bool:
    """Push an arbitrary notification to the configured ntfy topic."""
    if not config.HEADS_UP_NTFY_URL:
        return False
    return _send_ntfy(text, title, click_url=click_url)


def send(
    kind: str,
    user_id: int = 1,
    *,
    duration_minutes: int | None = None,
    language: str | None = None,
    style: str | None = None,
    fmt: str | None = None,
    view: str | None = None,
    max_stories: int | None = None,
    fact_check: bool | None = None,
    notify: bool | None = None,
    script_only: bool | None = None,
) -> tuple[bool, str | None]:
    """Render and push one report now; returns ``(sent, text)``."""
    if kind == "podcast":
        try:
            ep = podcast.start_generation(
                user_id,
                duration_minutes=duration_minutes or 15,
                view="default" if view == "default" else "personal",
                max_stories=max_stories,
                language=language or "auto",
                style=style or "informative",
                fmt=fmt or "solo",
                fact_check=True if fact_check is None else bool(fact_check),
                notify=True if notify is None else bool(notify),
                script_only=bool(script_only),
                background=True,
            )
            return True, f"Podcast #{ep['id']} queued ({ep['duration_target']}m)"
        except Exception as e:  # noqa: BLE001
            log.warning("scheduled podcast generation failed to start: %s", e)
            return False, str(e)
    text = render(kind, user_id=user_id, view=view, max_stories=max_stories)
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


def _claim(key: str, today: str) -> bool:
    """True for the one caller (across restarts and processes) that may send ``key`` today."""
    with db.get_conn() as conn:
        return conn.execute(
            "INSERT INTO ReportSent (key, sent_on) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET sent_on = excluded.sent_on WHERE sent_on <> excluded.sent_on",
            (key, today),
        ).rowcount > 0


def tick(fired: dict[str, str], now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now()
    today = now.date().isoformat()
    with db.get_conn() as conn:
        admins = [r[0] for r in conn.execute("SELECT id FROM AppUser WHERE is_admin = 1 ORDER BY id")]
    for uid in admins:
        profile = scoring.get_user_profile(user_id=uid)
        # The env default is one brief for the server's one topic, not one per admin.
        if uid != admins[0] and not isinstance(profile["preferences"].get("ntfy_reports"), list):
            continue
        for s in schedules(profile):
            key = f"{uid}:{s.get('kind')}@{_minutes(s.get('time', ''))}"
            if not due(s, now) or fired.get(key) == today:
                continue
            fired[key] = today
            if not _claim(key, today):
                continue
            if s["kind"] == "podcast":
                sent, text = send(
                    "podcast",
                    uid,
                    duration_minutes=s.get("duration_minutes") or 15,
                    language=s.get("language") or "auto",
                    style=s.get("style") or "informative",
                    fmt=s.get("format") or "solo",
                    view=s.get("view") or "personal",
                    max_stories=s.get("max_stories"),
                    fact_check=s.get("fact_check"),
                    notify=s.get("notify"),
                    script_only=s.get("script_only"),
                )
            elif s["kind"] == "brief" and (s.get("view") or s.get("max_stories")):
                sent, text = send(
                    "brief",
                    uid,
                    view=s.get("view"),
                    max_stories=s.get("max_stories"),
                )
            else:
                sent, text = send(s["kind"], uid)
            log.info("scheduled %s report at %s (user %s): %s", s["kind"], s["time"], uid,
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
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, name="news-reports", daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop.set()
