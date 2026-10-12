from app import config, db, pipeline

from conftest import add_article


def _story_of(article_id):
    with db.get_conn() as conn:
        return conn.execute("SELECT story_id FROM Article WHERE id = ?", (article_id,)).fetchone()[0]


def test_new_article_creates_story_with_timings(feed_id, llm):
    aid = add_article(feed_id, "Flood hits town")
    assert pipeline.process_pending() == {"processed": 1, "failed": 0}
    with db.get_conn() as conn:
        a = conn.execute("SELECT * FROM Article WHERE id = ?", (aid,)).fetchone()
        s = conn.execute("SELECT * FROM Story WHERE id = ?", (a["story_id"],)).fetchone()
    assert a["processed_at"] and a["t_wall_s"] >= a["t_story_s"] >= 0
    assert s["headline"].startswith("Headline")
    assert '"location": "Munich, Germany"' in s["metrics"]
    assert s["tags"] == '["ai", "machine learning"]'
    assert llm.calls == ["harness", "story"]


def test_same_event_joins_after_verification(feed_id, llm):
    first = add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    second = add_article(feed_id, "Flood update: rescue")
    pipeline.process_pending()
    assert _story_of(first) == _story_of(second)
    assert "verify" in llm.calls


def test_verifier_rejection_creates_new_story(feed_id, llm):
    first = add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    llm.verdict = "SAME_TOPIC_DISTINCT_EVENT"
    second = add_article(feed_id, "Flood elsewhere")
    pipeline.process_pending()
    assert _story_of(first) != _story_of(second)


def test_newest_articles_are_processed_first(feed_id, llm):
    old = add_article(feed_id, "Alpha old", age="-2 hours")
    new = add_article(feed_id, "Beta new")
    pipeline.process_pending()
    with db.get_conn() as conn:
        t = {r[0]: r[1] for r in conn.execute("SELECT id, processed_at FROM Article")}
    assert t[new] <= t[old]
    assert new > old


def test_failure_counts_and_dead_letters(feed_id, llm):
    aid = add_article(feed_id, "Storm warning")
    llm.fail = True
    for _ in range(config.PIPELINE_MAX_FAILURES):
        assert pipeline.process_pending()["failed"] == 1
    assert pipeline.process_pending() == {"processed": 0, "failed": 0}
    with db.get_conn() as conn:
        a = conn.execute("SELECT story_id, pipeline_failures, content_class FROM Article WHERE id = ?", (aid,)).fetchone()
        assert conn.execute("SELECT count(*) FROM Story").fetchone()[0] == 0
    assert a["story_id"] is None and a["pipeline_failures"] == config.PIPELINE_MAX_FAILURES
    assert a["content_class"] == "ok"


def test_ollama_outage_does_not_burn_retries(feed_id, llm, monkeypatch):
    from app import ollama

    aid = add_article(feed_id, "Storm warning")
    llm.down = True
    assert pipeline.process_pending() == {"processed": 0, "failed": 0}  # pre-check pauses the run
    monkeypatch.setattr(ollama, "available", lambda: True)  # outage starts mid-run
    assert pipeline.process_pending() == {"processed": 0, "failed": 0}
    with db.get_conn() as conn:
        a = conn.execute("SELECT story_id, pipeline_failures FROM Article WHERE id = ?", (aid,)).fetchone()
    assert a["story_id"] is None and not a["pipeline_failures"]


def test_broken_and_disabled_feed_articles_are_skipped(feed_id, llm):
    add_article(feed_id, "Broken page", body="x", content_class="broken")
    with db.get_conn() as conn:
        other = conn.execute("INSERT INTO Feed (title, xml_url, enabled) VALUES ('off', 'https://off/rss', 0)").lastrowid
    add_article(other, "Disabled feed item")
    with db.get_conn() as conn:
        assert pipeline.pending_count(conn) == 0
        statuses = [r[0] for r in conn.execute(
            f"SELECT {pipeline.STATUS_SQL} FROM Article a JOIN Feed f ON f.id = a.feed_id")]
    assert statuses == ["skipped", "skipped"]


def test_junk_join_skips_llm_calls(feed_id, llm):
    first = add_article(feed_id, "Rumor about thing", body="tiny", content_class="thin")
    pipeline.process_pending()
    with db.get_conn() as conn:
        conn.execute("UPDATE Story SET quality_score = 1.0")
    llm.calls.clear()
    second = add_article(feed_id, "Rumor continues", body="tiny too", content_class="thin")
    pipeline.process_pending()
    assert _story_of(first) == _story_of(second)
    assert llm.calls == ["verify"]
