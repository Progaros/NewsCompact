from app import db, merge, pipeline

from conftest import add_article


def _make_two_stories(feed_id, llm):
    """Two stories about the same event, split because the verifier said no at ingest time."""
    add_article(feed_id, "Quake strikes city")
    pipeline.process_pending()
    llm.verdict = "UNRELATED"
    add_article(feed_id, "Quake aftershocks")
    add_article(feed_id, "Quake rescue teams")
    pipeline.process_pending()
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM Story").fetchone()[0] >= 2


def test_merge_pass_folds_verified_duplicates(feed_id, llm):
    _make_two_stories(feed_id, llm)
    llm.verdict = "EXACT_MATCH"
    result = merge.run()
    assert result["merged"] >= 1
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(DISTINCT story_id) FROM Article").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM Story").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM Article WHERE t_merge_s IS NOT NULL").fetchone()[0] >= 1


def test_merge_pass_respects_rejection_and_marks_checked(feed_id, llm):
    _make_two_stories(feed_id, llm)
    result = merge.run()
    assert result["merged"] == 0 and result["verified"] >= 1
    llm.calls.clear()
    assert merge.run()["checked"] == 0  # unchanged stories are not re-checked
    assert llm.calls == []


def test_merge_budget(feed_id, llm, monkeypatch):
    from app import config

    _make_two_stories(feed_id, llm)
    monkeypatch.setattr(config, "MERGE_MAX_CHECKS_PER_RUN", 1)
    assert merge.run()["verified"] <= 1


def test_continuation_does_not_merge_stories(feed_id, llm):
    _make_two_stories(feed_id, llm)
    llm.verdict = "CONTINUING_DEVELOPMENT"
    result = merge.run()
    assert result["verified"] >= 1 and result["merged"] == 0


def test_shared_headline_terms_find_duplicates_embeddings_miss(feed_id, llm, monkeypatch):
    from app import config

    add_article(feed_id, "Wirtschaftsnobelpreis geht an zwei US-Forscher")
    add_article(feed_id, "Economics prize announced in Stockholm")
    pipeline.process_pending()  # different first words -> unrelated vectors -> two stories
    with db.get_conn() as conn:
        a, b = [r[0] for r in conn.execute("SELECT id FROM Story ORDER BY id")]
        conn.execute("UPDATE Story SET headline = 'Robert Hall and Michael Woodford awarded Nobel Prize', "
                     "news_at = datetime('now', '-3 hours') WHERE id = ?", (a,))
        conn.execute("UPDATE Story SET headline = 'Michael Woodford, Robert Hall win economics Nobel', "
                     "news_at = datetime('now', '-1 hours') WHERE id = ?", (b,))
    assert merge.headline_terms("Hall wins GLM-5.3 race in 2026.") == {"hall", "wins", "glm-5.3", "race", "2026"}

    monkeypatch.setattr(config, "MERGE_LEXICAL_MIN_TERMS", 99)
    assert merge.run()["verified"] == 0
    with db.get_conn() as conn:
        conn.execute("UPDATE Story SET merge_checked_at = NULL")
    monkeypatch.setattr(config, "MERGE_LEXICAL_MIN_TERMS", 3)
    assert merge.run()["merged"] == 1
    with db.get_conn() as conn:
        rows = conn.execute("SELECT news_at > datetime('now', '-2 hours') FROM Story").fetchall()
    assert len(rows) == 1 and rows[0][0] == 1  # the merged story keeps the newest news time
