import httpx
import pytest
from fastapi.testclient import TestClient

from app import api, auth, config, db, ollama, pipeline, worker

from conftest import add_article


@pytest.fixture
def client(monkeypatch, llm):
    monkeypatch.setattr(worker, "start", lambda: None)
    with TestClient(api.create_app(), client=("192.168.1.20", 50000)) as c:
        yield c


@pytest.fixture
def story(feed_id, llm):
    add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    with db.get_conn() as conn:
        return conn.execute("SELECT id FROM Story").fetchone()[0]


def test_stories_and_articles(client, story):
    data = client.get("/api/stories").json()
    assert data["count"] == 1 and data["stories"][0]["articles"][0]["feed"] == "Feed"
    assert client.get("/api/stories?archived=true").json()["count"] == 0
    assert client.get("/api/stories?q=nothing-matches").json()["count"] == 0
    assert client.get(f"/api/stories/{story}").status_code == 200
    assert client.get("/api/stories/999").status_code == 404
    arts = client.get("/api/articles?status=assigned").json()
    assert arts["count"] == 1 and arts["articles"][0]["timings"]["latency"] is not None
    assert client.get("/api/articles?status=bogus").status_code == 422
    assert client.get("/api/stories?limit=0").status_code == 422


def test_security_headers_and_origin_check(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/refresh", headers=evil).status_code == 403
    assert client.delete("/api/feeds/1", headers=evil).status_code == 403
    assert client.get("/docs").status_code == 404


def test_feed_validation(client, feed_id):
    assert client.post("/api/feeds", json={"title": "x", "xml_url": "file:///etc/passwd"}).status_code == 422
    assert client.post("/api/feeds", json={"title": "x", "xml_url": "https://example.org/rss"}).status_code == 409
    assert client.patch("/api/feeds/999", json={"enabled": 1}).status_code == 404
    assert client.get("/api/feeds/preview", params={"url": "gopher://x"}).status_code == 422
    assert client.patch(f"/api/feeds/{feed_id}", json={"title": "Renamed"}).status_code == 200
    assert client.get("/api/feeds").json()["feeds"][0]["title"] == "Renamed"


def test_profile_put_validates(client):
    ok = client.put("/api/profile", json={"preferences": {"locations": [" munich ", ""], "min_quality": 3},
                                          "tag_weights": {"AI": 2}})
    assert ok.status_code == 200
    body = ok.json()
    assert body["preferences"]["locations"] == ["munich"] and body["tag_weights"] == {"ai": 2.0}
    assert client.put("/api/profile", json={"preferences": {"min_quality": 99}}).status_code == 422
    assert client.put("/api/profile", json={"preferences": {"locations": ["x" * 200]}}).status_code == 422


def test_profile_reports_and_source_trust(client, feed_id):
    p = client.get("/api/profile").json()
    assert p["source_trust"] == {"News": 1.0} and p["ntfy"]["enabled"] is False
    sched = [{"kind": "pipeline", "time": "06:45", "days": [4, 0, 0]}]
    r = client.put("/api/profile", json={"preferences": {"ntfy_reports": sched}, "source_weights": {"News": 0.4}})
    assert r.status_code == 200
    body = r.json()
    assert body["ntfy_reports"] == [{"kind": "pipeline", "time": "06:45", "days": [0, 4], "enabled": True}]
    assert body["source_trust"] == {"News": 0.4}
    bad = [{"kind": "pipeline", "time": "25:00"}], [{"kind": "spam", "time": "06:00"}], \
        [{"kind": "brief", "time": "06:00", "days": [7]}]
    for b in bad:
        assert client.put("/api/profile", json={"preferences": {"ntfy_reports": b}}).status_code == 422
    assert client.put("/api/profile", json={"source_weights": {"News": 2}}).status_code == 422
    assert "processed" in client.get("/api/reports/pipeline").json()["text"]
    assert client.get("/api/reports/nope").status_code == 404
    assert client.post("/api/reports/pipeline/send").status_code == 409  # ntfy off in tests


def test_chat_streams_and_persists(client, story, llm):
    import json

    assert client.post(f"/api/stories/{story}/chat", json={"message": "x" * 5000}).status_code == 422
    r = client.post(f"/api/stories/{story}/chat", json={"message": "What happened?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines() if line]
    assert [e["type"] for e in events] == ["step", "thinking", "step", "delta", "delta", "done"]
    assert events[2]["step"]["kind"] == "thinking" and events[2]["step"]["status"] == "ok"
    history = client.get(f"/api/stories/{story}/chat").json()["messages"]
    assert [m["content"] for m in history] == ["What happened?", "Short answer."]
    assert history[1]["steps"][0]["detail"] == "Hmm. "


def test_chat_edit_keeps_versions(client, story, llm):
    url = f"/api/stories/{story}/chat"
    client.post(url, json={"message": "First question"})
    client.post(url, json={"message": "Follow-up"})
    msgs = client.get(url).json()["messages"]
    assert [m["content"] for m in msgs] == ["First question", "Short answer.", "Follow-up", "Short answer."]
    first = msgs[0]["id"]

    assert client.post(url, json={"message": "Edited", "edit_of": msgs[1]["id"]}).status_code == 404  # not a user msg
    assert client.post(url, json={"message": "Edited", "edit_of": 9999}).status_code == 404
    client.post(url, json={"message": "Edited question", "edit_of": first})
    msgs = client.get(url).json()["messages"]
    assert [m["content"] for m in msgs] == ["Edited question", "Short answer."]
    assert msgs[0]["version"] == 2 and msgs[0]["versions"][0] == first

    old = client.post(f"{url}/switch", json={"message_id": first}).json()["messages"]
    assert [m["content"] for m in old] == ["First question", "Short answer.", "Follow-up", "Short answer."]
    assert old[0]["version"] == 1 and len(old[0]["versions"]) == 2
    assert client.get(url).json()["messages"] == old  # the shown branch is remembered
    assert client.post(f"{url}/switch", json={"message_id": 9999}).status_code == 404


def test_chat_reports_failed_tools(client, story, llm, monkeypatch):
    import json

    from app import searchngx

    def boom(*_a, **_k):
        raise httpx.ConnectError("search host down")

    monkeypatch.setattr(searchngx, "search", boom)
    r = client.post(f"/api/stories/{story}/chat", json={"message": "Latest?", "search_turns": 1})
    steps = [json.loads(line)["step"] for line in r.text.splitlines() if '"step"' in line]
    assert steps[-3]["kind"] == "search" and steps[-3]["status"] == "error"
    assert "search host down" in steps[-3]["detail"]
    saved = client.get(f"/api/stories/{story}/chat").json()["messages"][1]["steps"]
    assert [s["kind"] for s in saved] == ["search", "thinking"]

    def dead_llm(*_a, **_k):
        raise httpx.ConnectError("ollama down")
        yield  # pragma: no cover

    monkeypatch.setattr(ollama, "chat_stream", dead_llm)
    r = client.post(f"/api/stories/{story}/chat", json={"message": "Again?"})
    events = [json.loads(line) for line in r.text.splitlines() if line]
    assert events[-1]["type"] == "error" and events[-2]["step"]["title"] == "Answer failed"
    assert client.get(f"/api/stories/{story}/chat").json()["messages"][-1]["steps"][0]["kind"] == "error"


def test_think_tags_are_split_from_the_answer():
    split = ollama._ThinkSplitter()
    out = []
    for chunk in ("<thi", "nk>plan", "ning</th", "ink>\n\nAnswer <b>", "!"):
        out += split.feed(chunk)
    out += split.flush()
    kinds = {k: "".join(t for kk, t in out if kk == k) for k in ("thinking", "content")}
    assert kinds == {"thinking": "planning", "content": "\n\nAnswer <b>!"}


def test_open_mode_is_lan_only(client, monkeypatch):
    monkeypatch.setattr(worker, "start", lambda: None)
    with TestClient(api.create_app(), client=("8.8.8.8", 50000)) as remote:
        assert remote.get("/api/stories").status_code == 200
        assert remote.get("/api/feeds").status_code == 403
        assert remote.post("/api/refresh").status_code == 403
    # Behind nginx on the same host: the proxy-appended address decides, spoofed entries don't help.
    with TestClient(api.create_app(), client=("127.0.0.1", 50000)) as proxied:
        assert proxied.get("/api/feeds", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 403
        spoof = {"X-Forwarded-For": "10.0.0.2, 8.8.8.8"}
        assert proxied.get("/api/feeds", headers=spoof).status_code == 403
        assert proxied.get("/api/feeds", headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 200


def test_auth_allowlist(client, story, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    get = lambda p: client.get(p, follow_redirects=False).status_code  # noqa: E731
    assert get("/api/stories") == 200 and get(f"/api/stories/{story}") == 200
    assert get("/api/feeds") == 401 and get(f"/api/stories/{story}/chat") == 401
    assert get("/feeds") == 303
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "secret"}).status_code == 200
    assert get("/api/feeds") == 200
    auth._sessions.clear()
    auth._failures.clear()
