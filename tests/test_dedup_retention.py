from app import config, db, dedup, pipeline, retention, scoring

from conftest import add_article


def test_keys():
    assert dedup.url_key("https://www.Example.com/a/?utm_source=x&id=3&fbclid=1") == "example.com/a?id=3"
    assert dedup.url_key("https://m.youtube.com/watch?v=abc&si=xyz") == "youtube.com/watch?v=abc"
    assert dedup.url_key("https://youtu.be/ID?is=1") == dedup.url_key("https://youtu.be/ID?is=2")
    assert dedup.url_key(None) is None
    assert dedup.title_key("Parliament passes the climate law!") == "parliament passes the climate law"
    assert dedup.title_key("Too short title") is None


def test_duplicate_joins_original_story_without_llm(feed_id, llm):
    original = add_article(feed_id, "Flood hits town hard tonight")
    dup = add_article(feed_id, "Flood hits town hard tonight copy")
    with db.get_conn() as conn:
        conn.execute("UPDATE Article SET duplicate_of = ? WHERE id = ?", (original, dup))
        assert pipeline.pending_count(conn) == 1
    pipeline.process_pending()
    assert llm.calls == ["harness", "story"]  # only the original was processed
    with db.get_conn() as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT id, story_id, quality FROM Article")}
    assert rows[dup]["story_id"] == rows[original]["story_id"] is not None
    assert rows[dup]["quality"] == rows[original]["quality"]


def test_duplicate_of_dead_original_is_released(feed_id, llm):
    original = add_article(feed_id, "Storm warning issued for coast")
    dup = add_article(feed_id, "Storm warning issued for coast again")
    with db.get_conn() as conn:
        conn.execute("UPDATE Article SET duplicate_of = ? WHERE id = ?", (original, dup))
        conn.execute("UPDATE Article SET pipeline_failures = ? WHERE id = ?",
                     (config.PIPELINE_MAX_FAILURES, original))
    pipeline.process_pending()
    with db.get_conn() as conn:
        row = conn.execute("SELECT duplicate_of, story_id FROM Article WHERE id = ?", (dup,)).fetchone()
    assert row["duplicate_of"] is None and row["story_id"] is not None


def test_retention_deletes_expired_stories_and_keeps_votes(feed_id, llm):
    keep = add_article(feed_id, "Fresh topic")
    old = add_article(feed_id, "Ancient topic", age=f"-{int(config.RETENTION_DAYS) + 2} days")
    add_article(feed_id, "Never processed", body="x", content_class="broken",
                age=f"-{int(config.RETENTION_DAYS) + 2} days")
    pipeline.process_pending()
    with db.get_conn() as conn:
        old_story = conn.execute("SELECT story_id FROM Article WHERE id = ?", (old,)).fetchone()[0]
        conn.execute("UPDATE Story SET updated_at = datetime('now', ?) WHERE id = ?",
                     (f"-{int(config.RETENTION_DAYS) + 1} days", old_story))
        old_url = conn.execute("SELECT url FROM Article WHERE id = ?", (old,)).fetchone()[0]
    scoring.apply_feedback(old_story, "up")

    assert retention.run() == {"stories": 1, "articles": 2}
    with db.get_conn() as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM Article")] == [keep]
        assert conn.execute("SELECT count(*) FROM Article_Vectors").fetchone()[0] == 1
        assert conn.execute("SELECT 1 FROM ArticleTombstone WHERE url = ?", (old_url,)).fetchone()
        vote = conn.execute("SELECT headline, kind FROM FeedbackLog").fetchone()
    assert vote["kind"] == "up" and vote["headline"]
