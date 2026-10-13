import pytest
from fastapi.testclient import TestClient

from app import api, db, pipeline, story, worker

from conftest import add_article


@pytest.fixture
def client(monkeypatch, llm):
    monkeypatch.setattr(worker, "start", lambda: None)
    with TestClient(api.create_app(), client=("192.168.1.20", 50000)) as c:
        yield c


def _ids(client, **params):
    data = client.get("/api/stories", params=params).json()
    return [s["id"] for s in data["stories"]], data["hidden_read"]


def _age_read(sid):
    """Reads and material updates in the same second can't be ordered; push both into the past."""
    with db.get_conn() as conn:
        conn.execute("UPDATE Story SET material_at = datetime('now', '-2 minutes') WHERE id = ?", (sid,))
        conn.execute("UPDATE StoryRead SET read_at = datetime('now', '-1 minute') WHERE story_id = ?", (sid,))


def test_mark_read_hides_keep_exempts_and_unread_restores(client, feed_id, llm):
    add_article(feed_id, "Flood hits town")
    add_article(feed_id, "Market rallies")
    pipeline.process_pending()
    first, second = _ids(client)[0]
    assert client.post(f"/api/stories/{first}/read").status_code == 200
    assert client.post("/api/stories/999/read").status_code == 404

    assert _ids(client, hide_read="true") == ([second], 1)
    assert _ids(client, hide_read="true", keep=str(first)) == ([first, second], 0)
    assert _ids(client) == ([first, second], 0)  # "show read"
    payload = client.get(f"/api/stories/{first}").json()
    assert payload["read"] == "read" and payload["read_summary"] == payload["summary"]

    assert client.delete(f"/api/stories/{first}/read").status_code == 200
    assert _ids(client, hide_read="true") == ([first, second], 0)


def test_read_story_resurfaces_only_on_significant_updates(client, feed_id, llm):
    add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    sid = _ids(client)[0][0]
    client.post(f"/api/stories/{sid}/read")
    _age_read(sid)

    llm.significance = 1  # minor follow-up
    add_article(feed_id, "Flood update minor")
    pipeline.process_pending()
    assert client.get(f"/api/stories/{sid}").json()["read"] == "read"

    llm.significance = 2  # notable development
    add_article(feed_id, "Flood update notable")
    pipeline.process_pending()
    data = client.get(f"/api/stories/{sid}").json()
    assert data["read"] == "updated"
    assert sid in _ids(client, hide_read="true")[0]


def test_urgency_escalation_resurfaces(client, feed_id, llm):
    add_article(feed_id, "Storm forms")
    pipeline.process_pending()
    sid = _ids(client)[0][0]
    client.post(f"/api/stories/{sid}/read")
    _age_read(sid)
    llm.significance, llm.urgency = 0, 7
    add_article(feed_id, "Storm hits coast")
    pipeline.process_pending()
    assert client.get(f"/api/stories/{sid}").json()["read"] == "updated"


def test_update_significance_rules():
    m = {"urgency_level": 4, "status": "Ongoing"}
    assert story.update_significance({"update_significance": 2}, m, dict(m), incremental=True) == 2
    assert story.update_significance({"update_significance": "x"}, m, dict(m), incremental=True) == 1
    assert story.update_significance({}, m, dict(m), incremental=False) == story.SIGNIFICANCE_MAX
    breaking = {"urgency_level": 4, "status": "Breaking"}
    assert story.update_significance({"update_significance": 0}, m, breaking, True) == story.SIGNIFICANCE_MAX
