import datetime as dt

import httpx

from app import config, db, pipeline, reports, scoring, searchngx

from conftest import add_article


def test_schedule_fires_on_selected_days_once(monkeypatch):
    sent = []
    monkeypatch.setattr(reports, "send", lambda kind, *_a: (sent.append(kind), (True, "x"))[1])
    scoring.update_profile({"ntfy_reports": [
        {"kind": "pipeline", "time": "07:00", "days": [0], "enabled": True},
        {"kind": "brief", "time": "07:00", "days": [0], "enabled": False},
    ]})
    monday = dt.datetime(2026, 10, 12, 7, 2)
    monday -= dt.timedelta(days=monday.weekday())
    fired: dict[str, str] = {}
    reports.tick(fired, monday)
    reports.tick(fired, monday)
    assert sent == ["pipeline"]
    reports.tick({}, monday + dt.timedelta(days=1))  # Tuesday not selected
    reports.tick({}, monday.replace(minute=10))  # past the catch-up window
    assert sent == ["pipeline"]


def test_report_is_sent_once_per_day_across_restarts_and_admins(monkeypatch):
    sent = []
    monkeypatch.setattr(reports, "send", lambda kind, uid: (sent.append((kind, uid)), (True, "x"))[1])
    monkeypatch.setattr(config, "HEADS_UP_NTFY_TIMES", ["07:00"])
    with db.get_conn() as conn:
        conn.execute("INSERT INTO AppUser (name, is_admin) VALUES ('second', 1)")
    at = dt.datetime(2026, 10, 12, 7, 1)
    reports.tick({}, at)
    reports.tick({}, at.replace(minute=3))  # restarted / second process: no in-memory state
    assert sent == [("brief", 1)]  # the env default is one brief, not one per admin
    reports.tick({}, at + dt.timedelta(days=1))
    assert sent == [("brief", 1)] * 2


def test_env_times_apply_until_schedules_are_saved(monkeypatch):
    monkeypatch.setattr(config, "HEADS_UP_NTFY_TIMES", ["06:30"])
    assert reports.schedules() == [{"kind": "brief", "time": "06:30", "days": list(range(7)), "enabled": True}]
    scoring.update_profile({"ntfy_reports": []})
    assert reports.schedules() == []


def test_pipeline_report_text(feed_id, llm):
    add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    with db.get_conn() as conn:
        text = reports.pipeline_report(conn)
    assert "1 processed" in text and "Stories: 1 new" in text and "Top: Headline" in text


def test_search_uses_searxng_json_and_falls_back_to_engines(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.url.params))
        if "engines" in req.url.params:
            return httpx.Response(200, json={"results": [
                {"title": "T", "url": "https://a.example/x", "content": "C"},
                {"title": "dup", "url": "https://a.example/x"},
            ]})
        return httpx.Response(200, json={"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]})

    monkeypatch.setattr(searchngx, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert searchngx.search("q") == [{"title": "T", "url": "https://a.example/x", "description": "C"}]
    assert seen[0]["format"] == "json" and seen[0]["categories"] == config.SEARCHNGX_CATEGORIES
    assert seen[1]["engines"] == config.SEARCHNGX_FALLBACK_ENGINES
