import threading

import pytest

from app import clustering, config, db, ollama, scoring, search

from conftest import add_article, topic_vector


def _story(conn, feed_id, headline, summary="S", titles=(), tags='["politics"]', vectors=()):
    sid = conn.execute("INSERT INTO Story (headline, summary, tags, updated_at) VALUES (?, ?, ?, datetime('now'))",
                       (headline, summary, tags)).lastrowid
    for i, title in enumerate(titles or [headline]):
        aid = add_article(feed_id, title, url=f"https://example.org/{sid}/{i}")
        conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (sid, aid))
        clustering.ensure_embedding(conn, {"id": aid}, topic_vector(vectors[i] if vectors else title))
    return sid


def _ids(conn, query, **kw):
    return [i["story"]["id"] for i in scoring.ranked_stories(conn, query=query, diverse=False, **kw)]


@pytest.fixture
def query_embed(monkeypatch):
    """Query vectors follow the first word after the instruction (like topic_vector for articles)."""
    seen = []

    def embed_texts(texts, **_kw):
        seen.append(texts[0])
        return [topic_vector(t.split("Query: ", 1)[-1]) for t in texts]

    monkeypatch.setattr(ollama, "embed_texts", embed_texts)
    return seen


def test_fts_query_never_passes_operators():
    assert search.fts_query('Merz "OR" -(Zölle* NEAR') == '"merz"* AND "or" AND "zölle"* AND "near"*'
    assert search.fts_query("%%%") == ""


def test_word_search(feed_id):
    with db.get_conn() as conn:
        tariffs = _story(conn, feed_id, "Merz kündigt neue Zölle an", titles=["Kanzler spricht in Berlin"])
        other = _story(conn, feed_id, "Flood in Austria", summary="Österreich unter Wasser",
                       titles=["Heavy rain causes damage"])
        assert _ids(conn, "zolle merz") == [tariffs]  # words in any order, diacritics folded
        assert _ids(conn, "osterreich") == [other]  # summary
        assert _ids(conn, "kanzler") == [tariffs]  # article titles
        assert _ids(conn, "dama") == [other]  # prefix while typing
        assert _ids(conn, "merz flood") == []  # every word must match
        assert _ids(conn, '"unbalanced (') == []  # no FTS syntax error


def test_index_follows_story_and_article_changes(feed_id):
    with db.get_conn() as conn:
        a = _story(conn, feed_id, "First story", titles=["Volcano erupts"])
        b = _story(conn, feed_id, "Second story", titles=["Unrelated"])
        assert _ids(conn, "volcano") == [a]
        conn.execute("UPDATE Article SET story_id = ? WHERE story_id = ?", (b, a))  # merge
        assert _ids(conn, "volcano") == [b]
        conn.execute("UPDATE Story SET headline = 'Renamed eruption' WHERE id = ?", (b,))
        assert _ids(conn, "renamed volcano") == [b]
        conn.execute("DELETE FROM Story WHERE id = ?", (b,))
        assert conn.execute("SELECT count(*) FROM StorySearch WHERE rowid = ?", (b,)).fetchone()[0] == 0


def test_hybrid_adds_meaning_matches_and_fuses(feed_id, query_embed):
    with db.get_conn() as conn:
        both = _story(conn, feed_id, "Deluge warning for Vienna", vectors=["deluge"])
        meaning = _story(conn, feed_id, "Rivers burst their banks", vectors=["deluge"])
        words = _story(conn, feed_id, "Deluge of emails", vectors=["inbox"])
        _story(conn, feed_id, "Football results", vectors=["football"])
        items = scoring.ranked_stories(conn, query="deluge", semantic=True, diverse=False)
        assert {i["story"]["id"]: i["match"] for i in items} == {both: "both", meaning: "meaning", words: "words"}
        assert items[0]["story"]["id"] == both  # found by both lists
        assert set(_ids(conn, "deluge")) == {both, words}  # anonymous: words only
        assert query_embed[0].startswith("Instruct: ") and query_embed[0].endswith("\nQuery: deluge")


def test_semantic_falls_back_to_words(feed_id, monkeypatch):
    release = threading.Event()

    def slow(texts, **_kw):
        release.wait(5)
        return [topic_vector("deluge")]

    monkeypatch.setattr(ollama, "embed_texts", slow)
    monkeypatch.setattr(config, "SEARCH_SEMANTIC_TIMEOUT", 0.05)
    with db.get_conn() as conn:
        words = _story(conn, feed_id, "Deluge of emails", vectors=["inbox"])
        meaning = _story(conn, feed_id, "Rivers burst their banks", vectors=["deluge"])
        assert _ids(conn, "deluge", semantic=True) == [words]  # timeout
        pending = search._inflight["deluge"]
        monkeypatch.setattr(config, "SEARCH_EMBED_MAX_INFLIGHT", 1)
        assert search.query_vector("something else") is None  # slot still taken -> words only
        release.set()
        pending.result(5)
        assert set(_ids(conn, "deluge", semantic=True)) == {words, meaning}  # finished embedding was cached


def test_api_search_semantic_only_when_signed_in(client, feed_id, query_embed):
    with db.get_conn() as conn:
        sid = _story(conn, feed_id, "Rivers burst their banks", vectors=["deluge"])
    data = client.get("/api/stories?q=deluge").json()
    assert [(s["id"], s["match"]) for s in data["stories"]] == [(sid, "meaning")]
    client.post("/api/auth/logout")
    assert client.get("/api/stories?q=deluge").json()["count"] == 0
    assert client.get("/api/stories?q=rivers").json()["stories"][0]["match"] == "words"
