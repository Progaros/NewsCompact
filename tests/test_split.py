import httpx
import pytest

from app import clustering, config, db, split

from conftest import add_article, topic_vector


@pytest.fixture(autouse=True)
def small_stories(monkeypatch):
    monkeypatch.setattr(config, "SPLIT_MIN_ARTICLES", 8)


def _story(feed_id, titles: list[str]) -> tuple[int, list[int]]:
    """A summarized story holding one article per title (with its embedding)."""
    with db.get_conn() as conn:
        sid = conn.execute("INSERT INTO Story (headline, summary, tags, metrics) "
                           "VALUES ('Saga', 'Everything at once.', '[]', '{}')").lastrowid
    ids = [add_article(feed_id, t, age=f"-{60 - i} minutes") for i, t in enumerate(titles)]
    with db.get_conn() as conn:
        for aid, title in zip(ids, titles):
            conn.execute("UPDATE Article SET story_id = ?, processed_at = datetime('now') WHERE id = ?", (sid, aid))
            clustering.ensure_embedding(conn, {"id": aid}, topic_vector(title))
    return sid, ids


def _mixed(feed_id):
    titles = [f"Flood report {i}" for i in range(7)] + [f"Market report {i}" for i in range(4)]
    return _story(feed_id, titles)


def test_incoherent_story_splits_into_linked_parts(feed_id, llm, client):
    sid, ids = _mixed(feed_id)
    copy = add_article(feed_id, "Market report copy")
    with db.get_conn() as conn:
        conn.execute("UPDATE Article SET story_id = ?, duplicate_of = ? WHERE id = ?", (sid, ids[-1], copy))
    assert client.post(f"/api/stories/{sid}/read").status_code == 200
    assert client.post("/api/feedback", json={"story_id": sid, "kind": "up"}).status_code == 200

    result = split.run()
    assert result == {"checked": 1, "split": 1, "parts": 1}
    with db.get_conn() as conn:
        new_id = conn.execute("SELECT max(id) FROM Story").fetchone()[0]
        assert new_id != sid
        # The largest group keeps the story, the other one (with its copies) moves.
        assert {r[0] for r in conn.execute("SELECT id FROM Article WHERE story_id = ?", (sid,))} == set(ids[:7])
        assert {r[0] for r in conn.execute("SELECT id FROM Article WHERE story_id = ?", (new_id,))} == {*ids[7:], copy}
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM Story")}
        assert rows[sid]["headline"].startswith("Headline") and rows[new_id]["headline"].startswith("Headline")
        assert rows[new_id]["split_checked_at"] is not None
        assert [tuple(r) for r in conn.execute("SELECT relation, source FROM StoryLink")] == [
            ("CONTINUING_DEVELOPMENT", "merge")]
        assert [tuple(r) for r in conn.execute("SELECT story_id FROM FeedbackLog")] == [(sid,)]
    part = client.get(f"/api/stories/{new_id}").json()
    assert part["read"] == "read"  # read marks carry over
    assert [r["id"] for r in part["related"]] == [sid]

    llm.calls.clear()
    assert split.run()["checked"] == 0  # unchanged stories are not re-checked
    assert llm.calls == []


def test_coherent_story_stays_whole(feed_id, llm):
    sid, _ = _story(feed_id, [f"Flood report {i}" for i in range(12)])
    assert split.run() == {"checked": 1, "split": 0, "parts": 0}
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM Story").fetchone()[0] == 1
        assert conn.execute("SELECT split_checked_at FROM Story").fetchone()[0] is not None
    assert "story" not in llm.calls


def test_small_or_close_groups_stay_together(feed_id, llm, monkeypatch):
    # Two stray articles are below SPLIT_MIN_GROUP; they fold into the main group.
    _story(feed_id, [f"Flood item {i}" for i in range(9)] + ["Market item", "Storm item"])
    assert split.run()["split"] == 0
    monkeypatch.setattr(config, "SPLIT_PART_SIM", 0.0)  # every part counts as the same event
    sid, _ = _mixed(feed_id)
    with db.get_conn() as conn:
        assert split.plan(conn, sid) is None


def test_split_rolls_back_a_part_on_ollama_timeout(feed_id, llm, monkeypatch):
    sid, ids = _mixed(feed_id)

    def timeout(*_a, **_kw):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(split.story, "update_story", timeout)
    assert split.run()["split"] == 0
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM Story").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM Article WHERE story_id = ?", (sid,)).fetchone()[0] == len(ids)
        assert conn.execute("SELECT split_checked_at FROM Story").fetchone()[0] is None  # retried next run
