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
    llm.verdict = "CONTINUING_DEVELOPMENT"
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
