import httpx
import pytest
from fastapi.testclient import TestClient

from app import api, auth, config, db, ollama, pipeline, worker

from conftest import add_article


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


def test_related_stories_are_listed(client, story, feed_id):
    from app import clustering

    with db.get_conn() as conn:
        other_feed = conn.execute("INSERT INTO Feed (title, xml_url) VALUES ('Other', 'https://o/rss')").lastrowid
        other = conn.execute("INSERT INTO Story (headline) VALUES ('Flood hits next town')").lastrowid
        conn.execute("INSERT INTO Article (feed_id, story_id, title, url) VALUES (?, ?, 'a', 'https://o/1')",
                     (other_feed, other))
        clustering.link_stories(conn, story, other, "SAME_TOPIC_DISTINCT_EVENT", "article")
        hidden = conn.execute("INSERT INTO Story (headline) VALUES ('Unrelated')").lastrowid
        clustering.link_stories(conn, story, hidden, "UNRELATED", "merge")
    expected = [{"id": other, "headline": "Flood hits next town"}]
    assert client.get("/api/stories").json()["stories"][0]["related"] == expected
    assert client.get(f"/api/stories/{story}").json()["related"] == expected
    client.put("/api/profile", json={"preferences": {"excluded_feeds": [other_feed]}})
    assert client.get("/api/stories").json()["stories"][0]["related"] == []
    assert client.get(f"/api/stories/{story}", params={"view": "default"}).json()["related"] == expected


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


def test_profile_put_validates(client, story):
    ok = client.put("/api/profile", json={"preferences": {"locations": [" munich ", ""], "min_quality": 3},
                                          "tag_weights": {"AI": 2}})
    assert ok.status_code == 200
    body = ok.json()
    assert body["preferences"]["locations"] == ["munich"] and body["tag_weights"] == {"ai": 2.0}
    assert client.put("/api/profile", json={"preferences": {"min_quality": 99}}).status_code == 422
    assert client.put("/api/profile", json={"preferences": {"locations": ["x" * 200]}}).status_code == 422

    # Feedback vote + undo via API
    r = client.post("/api/feedback", json={"story_id": story, "kind": "up"}).json()
    assert r["vote"] == "up" and r["profile"]["tag_weights"]["ai"] == 3.0
    assert client.get(f"/api/stories/{story}").json()["vote"] == "up"
    r2 = client.post("/api/feedback", json={"story_id": story, "kind": None}).json()
    assert r2["vote"] is None and r2["profile"]["tag_weights"]["ai"] == 2.0
    assert client.get(f"/api/stories/{story}").json()["vote"] is None


def test_home_location_is_per_user(client, story):
    before = client.get("/api/stories").json()["stories"][0]["relevance"]["distance"]
    body = client.put("/api/profile", json={"preferences": {
        "home_city": " Vienna ", "home_country": "Austria", "language": "EN"}}).json()
    assert {k: body["preferences"][k] for k in ("home_city", "home_country", "language")} == {
        "home_city": "Vienna", "home_country": "Austria", "language": "en"}
    assert body["home_defaults"]["home_city"] == config.USER_CITY
    assert before == 10.0 and client.get("/api/stories").json()["stories"][0]["relevance"]["distance"] == 0.0
    assert client.put("/api/profile", json={"preferences": {"language": "german"}}).status_code == 422
    client.put("/api/profile", json={"preferences": {"home_city": "", "home_country": "", "language": ""}})
    assert client.get("/api/stories").json()["stories"][0]["relevance"]["distance"] == before  # server default


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


def test_single_source_trust_overrides_its_category(client, story, feed_id):
    p = client.get("/api/profile").json()
    assert p["feed_trust"] == [{"id": feed_id, "title": "Feed", "category": "News", "trust": 1.0, "custom": False}]
    before = client.get(f"/api/stories/{story}").json()["relevance"]["credibility"]
    body = client.put("/api/profile", json={"source_weights": {"News": 1.0},
                                            "feed_weights": {str(feed_id): 0.2, "999": 1}}).json()
    assert body["feed_weights"] == {str(feed_id): 0.2}  # unknown feeds are dropped
    assert body["feed_trust"][0]["trust"] == 0.2 and body["feed_trust"][0]["custom"] is True
    assert body["source_trust"] == {"News": 1.0}
    assert client.get(f"/api/stories/{story}").json()["relevance"]["credibility"] < before
    assert client.put("/api/profile", json={"feed_weights": {str(feed_id): 1.5}}).status_code == 422
    assert client.put("/api/profile", json={"feed_weights": {}}).json()["feed_trust"][0]["custom"] is False


def test_excluded_sources_leave_the_personal_feed_only(client, story, feed_id, monkeypatch):
    other = None
    with db.get_conn() as conn:
        other = conn.execute("INSERT INTO Feed (title, xml_url, category) VALUES ('Other', 'https://o.example/rss', 'News')"
                             ).lastrowid
    aid = add_article(other, "Flood hits town again")
    with db.get_conn() as conn:
        conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (story, aid))
    assert client.get("/api/stories").json()["stories"][0]["source_count"] == 2

    client.put("/api/profile", json={"preferences": {"excluded_feeds": [other, other]}})
    assert client.get("/api/profile").json()["preferences"]["excluded_feeds"] == [other]
    mine = client.get("/api/stories").json()["stories"][0]
    assert mine["source_count"] == 1 and [a["feed"] for a in mine["articles"]] == ["Feed"]
    assert client.get(f"/api/stories/{story}").json()["source_count"] == 1
    assert client.get("/api/stories", params={"view": "default"}).json()["stories"][0]["source_count"] == 2

    client.put("/api/profile", json={"preferences": {"excluded_feeds": [other, feed_id]}})
    assert client.get("/api/stories").json()["count"] == 0  # every source hidden
    assert client.get("/api/stories", params={"view": "default"}).json()["count"] == 1


def test_heads_up_is_per_view_and_never_blocks(client, story, llm, monkeypatch):
    import threading
    import time

    from app import heads_up

    heads_up._cache.clear()
    release, prompts = threading.Event(), []

    def slow_chat(messages, **_kw):
        prompts.append(messages[-1]["content"])
        release.wait(5)
        return f"brief {len(prompts)}"

    monkeypatch.setattr(ollama, "chat", slow_chat)
    first = client.get("/api/heads-up").json()  # returns at once, generation runs in the background
    assert first["generating"] is True and first["view"] == "default" and first["generated_at"] is None
    assert client.get("/api/heads-up").json()["generating"] is True  # no second generation queued
    release.set()
    for _ in range(100):
        if not (d := client.get("/api/heads-up").json())["generating"]:
            break
        time.sleep(0.05)
    assert d["text"] == "brief 1" and d["generated_at"]

    personal = client.get("/api/heads-up", params={"view": "personal"}).json()
    assert personal["view"] == "personal" and personal["generating"] is True and personal["text"] != "brief 1"
    for _ in range(100):
        if not (d := client.get("/api/heads-up", params={"view": "personal"}).json())["generating"]:
            break
        time.sleep(0.05)
    assert d["text"] == "brief 2" and len(prompts) == 2
    assert client.get("/api/heads-up").json()["text"] == "brief 1"  # cached per view


def test_about_you_personalizes_brief_and_chat(client, story, llm, monkeypatch):
    import time

    from app import heads_up

    seen = []
    monkeypatch.setattr(ollama, "chat", lambda messages, **_k: (seen.append(messages[0]["content"]), "brief")[1])

    def chat_stream(messages, **_kw):
        seen.append(messages[0]["content"])
        yield "content", "ok"

    monkeypatch.setattr(ollama, "chat_stream", chat_stream)
    heads_up._cache.clear()
    about = "Nurse in Munich who cycles to work"
    assert client.put("/api/profile", json={"preferences": {"about": "  " + about + " "}}).json()[
        "preferences"]["about"] == about
    assert client.put("/api/profile", json={"preferences": {"about": "x" * 1001}}).status_code == 422

    def wait(view):
        for _ in range(100):
            if not client.get("/api/heads-up", params={"view": view}).json()["generating"]:
                return
            time.sleep(0.05)

    wait("personal")
    wait("default")
    personal, default = [s for s in seen if "news brief" in s]
    assert about in personal and about not in default
    client.post(f"/api/stories/{story}/chat", json={"message": "Why care?"})
    assert about in seen[-1] and "ABOUT THE READER" in seen[-1]


def test_chat_streams_and_persists(client, story, llm):
    import json

    assert client.post(f"/api/stories/{story}/chat", json={"message": "x" * 5000}).status_code == 422
    # Web search is enabled by default; the agent plans a search and decides none is needed.
    r = client.post(f"/api/stories/{story}/chat", json={"message": "What happened?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines() if line]
    assert [e["type"] for e in events] == ["step", "step", "step", "thinking", "step", "delta", "delta", "done"]
    assert events[1]["step"]["kind"] == "search" and events[1]["step"]["title"] == "No web search needed"
    assert events[4]["step"]["kind"] == "thinking" and events[4]["step"]["status"] == "ok"
    history = client.get(f"/api/stories/{story}/chat").json()["messages"]
    assert [m["content"] for m in history] == ["What happened?", "Short answer."]
    assert [s["kind"] for s in history[1]["steps"]] == ["search", "thinking"]
    assert history[1]["steps"][1]["detail"] == "Hmm. "

    # Disabling web_search skips the planning step completely.
    r2 = client.post(f"/api/stories/{story}/chat", json={"message": "No search please", "web_search": False})
    events2 = [json.loads(line) for line in r2.text.splitlines() if line]
    assert [e["type"] for e in events2] == ["step", "thinking", "step", "delta", "delta", "done"]


def test_chat_autonomous_multi_turn_web_search(client, story, llm, monkeypatch):
    from app import searchngx

    searched, scraped = [], []
    monkeypatch.setattr(searchngx, "search", lambda q, **_k: (
        searched.append(q), [{"title": f"Result {q}", "url": f"https://example.org/{len(searched)}", "description": "s"}])[1])
    monkeypatch.setattr(searchngx, "scrape", lambda u: (scraped.append(u), f"Body from {u}")[1])
    llm.queries = ["flood river level", "flood evacuation order"]
    r = client.post(f"/api/stories/{story}/chat", json={"message": "How deep and who evacuated?"})
    assert r.status_code == 200
    assert searched == ["flood river level", "flood evacuation order"]
    assert len(scraped) == 2
    saved = client.get(f"/api/stories/{story}/chat").json()["messages"][-1]["steps"]
    assert [s["kind"] for s in saved] == ["search", "read", "search", "read", "search", "thinking"]
    assert saved[4]["title"] == "Enough context gathered"


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
    llm.queries = ["flood news"]
    r = client.post(f"/api/stories/{story}/chat", json={"message": "Latest?"})
    steps = [json.loads(line)["step"] for line in r.text.splitlines() if '"step"' in line]
    assert steps[-3]["kind"] == "search" and steps[-3]["status"] == "error"
    assert "search host down" in steps[-3]["detail"]
    saved = client.get(f"/api/stories/{story}/chat").json()["messages"][1]["steps"]
    assert [s["kind"] for s in saved] == ["search", "thinking"]

    def dead_llm(*_a, **_k):
        raise httpx.ConnectError("ollama down")
        yield  # pragma: no cover

    monkeypatch.setattr(ollama, "chat_stream", dead_llm)
    r = client.post(f"/api/stories/{story}/chat", json={"message": "Again?", "web_search": False})
    events = [json.loads(line) for line in r.text.splitlines() if line]
    assert events[-1]["type"] == "error" and events[-2]["step"]["title"] == "Answer failed"
    assert client.get(f"/api/stories/{story}/chat").json()["messages"][-1]["steps"][0]["kind"] == "error"


def test_chat_after_failed_turn_branches_instead_of_chaining_empty_reply(client, story, llm, monkeypatch):
    url = f"/api/stories/{story}/chat"
    client.post(url, json={"message": "First question", "web_search": False})
    orig_stream = ollama.chat_stream

    def dead_llm(*_a, **_k):
        raise httpx.ConnectError("ollama down")
        yield  # pragma: no cover

    monkeypatch.setattr(ollama, "chat_stream", dead_llm)
    client.post(url, json={"message": "Failed follow-up", "web_search": False})
    msgs = client.get(url).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[-1]["content"] == ""

    # Sending a new message after the empty failed turn replaces it as a sibling version (2/2).
    monkeypatch.setattr(ollama, "chat_stream", orig_stream)
    client.post(f"/api/stories/{story}/chat", json={"message": "Retried follow-up", "web_search": False})
    msgs2 = client.get(url).json()["messages"]
    assert [m["content"] for m in msgs2] == ["First question", "Short answer.", "Retried follow-up", "Short answer."]
    assert msgs2[2]["version"] == 2 and len(msgs2[2]["versions"]) == 2


def test_extract_json_rejects_inner_array_of_truncated_object():
    truncated = '{\n  "new_facts": ["Fact 1", "Fact 2"],\n  "headline": "Truncated'
    with pytest.raises(ValueError, match="could not parse JSON"):
        ollama.extract_json(truncated)
    assert ollama.extract_json('Prose before [{"headline": "H"}] after') == [{"headline": "H"}]


def test_stories_pulse_counts_news_since(client, story):
    data = client.get("/api/stories").json()
    news_at = data["latest"]
    assert news_at == data["stories"][0]["news_at"]
    assert client.get("/api/stories/pulse", params={"since": news_at}).json() == {
        "new": 0, "latest": None, "pending": 0}
    with db.get_conn() as conn:
        conn.execute("UPDATE Story SET news_at = datetime('now', '+1 minute')")
    pulse = client.get("/api/stories/pulse", params={"since": news_at}).json()
    assert pulse["new"] == 1 and pulse["latest"] > news_at
    # Even when the newest story is hidden (read / below page limit), /api/stories
    # returns the DB-wide latest news_at so pulse doesn't re-count hidden stories.
    client.post(f"/api/stories/{story}/read")
    hidden = client.get("/api/stories", params={"hide_read": "true"}).json()
    assert hidden["stories"] == [] and hidden["latest"] == pulse["latest"]
    assert client.get("/api/stories/pulse", params={"since": hidden["latest"]}).json()["new"] == 0
    client.post("/api/auth/logout")
    assert client.get("/api/stories/pulse").json()["new"] == 0  # public, like the story list


def test_one_vote_per_user_and_story(client, story):
    import sqlite3

    for kind in ("up", "up", "down"):
        assert client.post("/api/feedback", json={"story_id": story, "kind": kind}).status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*), max(kind) FROM FeedbackLog").fetchone()[:] == (1, "down")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO FeedbackLog (user_id, story_id, kind) VALUES (1, ?, 'up')", (story,))


def test_chat_prompt_lists_only_the_newest_articles(monkeypatch):
    from app import chat

    monkeypatch.setattr(config, "CHAT_STORY_MAX_ARTICLES", 3)
    prompt = chat.story_system_prompt({"articles": [{"title": f"Art {i}"} for i in range(5)]})
    assert "Art 2" in prompt and "Art 3" not in prompt and "… and 2 older articles" in prompt


def test_think_tags_are_split_from_the_answer():
    split = ollama._ThinkSplitter()
    out = []
    for chunk in ("<thi", "nk>plan", "ning</th", "ink>\n\nAnswer <b>", "!"):
        out += split.feed(chunk)
    out += split.flush()
    kinds = {k: "".join(t for kk, t in out if kk == k) for k in ("thinking", "content")}
    assert kinds == {"thinking": "planning", "content": "\n\nAnswer <b>!"}


def test_startup_fails_without_any_login(monkeypatch):
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    with pytest.raises(RuntimeError, match="NEWS_AUTH_PASSWORD"):
        with TestClient(api.create_app()):
            pass
    with db.get_conn() as conn:  # an admin with a stored password is enough
        conn.execute("UPDATE AppUser SET password_hash = ? WHERE id = 1", (auth.hash_password("owner pw 123"),))
    with TestClient(api.create_app()) as c:
        assert c.get("/api/profile").status_code == 401  # no implicit user anymore
        assert c.get("/api/auth/session").json() == {"authenticated": False, "user": None}


def test_auth_allowlist(client, story, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    get = lambda p: client.get(p, follow_redirects=False).status_code  # noqa: E731
    assert get("/api/stories") == 200 and get(f"/api/stories/{story}") == 200
    assert get("/api/feeds") == 401 and get(f"/api/stories/{story}/chat") == 401
    assert get("/feeds") == 303
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "secret"}).status_code == 200
    assert get("/api/feeds") == 200
    auth._failures.clear()
