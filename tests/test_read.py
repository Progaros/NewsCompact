from app import db, pipeline, story

from conftest import add_article


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
    assert _ids(client, hide_read="true", view="default") == ([first, second], 0)  # Everyone view always shows all
    assert _ids(client, hide_read="true", keep=str(first)) == ([first, second], 0)
    assert _ids(client) == ([first, second], 0)  # "show read": read ones keep their rank (no READER_FACTOR)
    payload = client.get(f"/api/stories/{first}").json()
    assert payload["read"] == "read" and payload["changes"] == []

    assert client.delete(f"/api/stories/{first}/read").status_code == 200
    assert _ids(client, hide_read="true") == ([first, second], 0)


def test_offline_replay_loses_to_a_later_change(client, feed_id, llm):
    add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    sid = _ids(client)[0][0]
    url = f"/api/stories/{sid}/read"
    # Unread 5 minutes ago, then a read click from 2 minutes ago replayed: applies, dated to the click.
    assert client.delete(url, params={"age": 300}).status_code == 200
    assert client.post(url, params={"age": 120}).status_code == 200
    with db.get_conn() as conn:
        lag = conn.execute("SELECT (julianday('now') - julianday(read_at)) * 86400 FROM StoryRead").fetchone()[0]
    assert 115 < lag < 130

    # Device B marks it unread now; device A's read click from 1 minute ago arrives after.
    assert client.delete(url).status_code == 200
    assert client.post(url, params={"age": 60}).status_code == 200
    assert client.get(f"/api/stories/{sid}").json()["read"] is None
    assert client.post(url, params={"age": 7 * 86400}).status_code == 422  # beyond the replay window
    assert client.post(url, params={"age": 7 * 86400}).status_code == 422  # beyond the replay window

    assert client.post("/api/feedback", json={"story_id": sid, "kind": "down"}).status_code == 200
    assert client.post("/api/feedback", json={"story_id": sid, "kind": "up", "age": 600}).status_code == 200
    assert client.get(f"/api/stories/{sid}").json()["vote"] == "down"


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
    assert data["changes"] == ["New detail"]  # minor + notable facts, deduped
    assert sid in _ids(client, hide_read="true")[0]


def test_urgency_escalation_resurfaces(client, feed_id, llm):
    add_article(feed_id, "Storm forms")
    pipeline.process_pending()
    sid = _ids(client)[0][0]
    client.post(f"/api/stories/{sid}/read")
    _age_read(sid)
    llm.significance, llm.urgency = 1, 7
    add_article(feed_id, "Storm hits coast")
    pipeline.process_pending()
    assert client.get(f"/api/stories/{sid}").json()["read"] == "updated"


def test_update_significance_rules():
    m = {"urgency_level": 4, "status": "Ongoing"}
    fact = {"new_facts": ["x"], "summary": "New."}
    assert story.update_significance({**fact, "update_significance": 2}, m, dict(m), True, "Old.") == 2
    assert story.update_significance({**fact, "update_significance": "x"}, m, dict(m), True, "Old.") == 1
    # no new facts / summary returned unchanged -> nothing to resurface
    assert story.update_significance({"new_facts": [""], "update_significance": 3}, m, dict(m), True, "Old.") == 0
    assert story.update_significance({"new_facts": ["x"], "summary": " old. ", "update_significance": 2},
                                     m, dict(m), True, "Old.") == 0
    # unrated full rewrite: new story is major, merge/rescore of an existing one is not
    assert story.update_significance({}, m, dict(m), False) == story.SIGNIFICANCE_MAX
    assert story.update_significance({}, m, dict(m), False, "Old.") == 0
    # escalation: only into the Breaking badge or a +2 jump to urgency >= 7
    low_breaking = {"urgency_level": 5, "status": "Breaking"}
    assert story.update_significance({**fact, "update_significance": 0}, m, low_breaking, True, "Old.") == 0
    breaking = {"urgency_level": 8, "status": "Breaking"}
    assert story.update_significance({**fact, "update_significance": 0}, m, breaking, True, "Old.") == story.SIGNIFICANCE_MAX
    jitter = {"urgency_level": 6, "status": "Ongoing"}
    assert story.update_significance({**fact, "update_significance": 1}, m, jitter, True, "Old.") == 1


def test_merge_does_not_resurface(client, feed_id, llm):
    from app import merge

    add_article(feed_id, "Quake strikes city")
    pipeline.process_pending()
    sid = _ids(client)[0][0]
    client.post(f"/api/stories/{sid}/read")
    _age_read(sid)
    aid = add_article(feed_id, "Quake aftermath")
    with db.get_conn() as conn:
        drop = conn.execute("INSERT INTO Story (headline, summary) VALUES ('Dup', 'Dup.')").lastrowid
        conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (drop, aid))
        merge._absorb(conn, sid, drop)
        story.update_story(conn, sid, force_full=True)
    assert client.get(f"/api/stories/{sid}").json()["read"] == "read"
