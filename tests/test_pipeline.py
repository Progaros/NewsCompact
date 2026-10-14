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
    llm.calls.clear()
    second = add_article(feed_id, "Flood elsewhere")
    pipeline.process_pending()
    assert _story_of(first) != _story_of(second)
    assert llm.calls.count("verify") == 1  # the revival tier does not re-ask about fresh stories
    with db.get_conn() as conn:
        link = conn.execute("SELECT a, b, relation, source FROM StoryLink").fetchone()
    assert tuple(link) == (_story_of(first), _story_of(second), "SAME_TOPIC_DISTINCT_EVENT", "article")


def test_stale_story_is_revived(feed_id, llm):
    first = add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    with db.get_conn() as conn:
        conn.execute("UPDATE Story SET updated_at = datetime('now', '-5 days')")
    second = add_article(feed_id, "Flood returns")
    pipeline.process_pending()
    assert _story_of(first) == _story_of(second)


def test_headline_only_quality_is_capped(feed_id, llm):
    aid = add_article(feed_id, "Short item", body="tiny", content_class="thin")
    pipeline.process_pending()
    with db.get_conn() as conn:
        q = conn.execute("SELECT quality FROM Article WHERE id = ?", (aid,)).fetchone()[0]
    assert q == config.TITLE_QUALITY_MAX < 7


def test_only_updates_with_new_facts_make_a_story_recent(feed_id, llm):
    from app import scoring

    add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    with db.get_conn() as conn:
        conn.execute("UPDATE Story SET news_at = datetime('now', '-6 hours'), updated_at = datetime('now', '-6 hours')")

    def state():
        with db.get_conn() as conn:
            s = conn.execute("SELECT * FROM Story").fetchone()
            return s["news_at"], s["updated_at"], scoring.ranked_stories(conn)[0]["display"]

    before = state()
    llm.significance = 0  # a rewording: no new facts
    add_article(feed_id, "Flood recap")
    pipeline.process_pending()
    after = state()
    assert after[0] == before[0] and after[1] > before[1] and abs(after[2] - before[2]) < 0.01

    llm.significance = 1
    add_article(feed_id, "Flood dam breaks")
    pipeline.process_pending()
    assert state()[0] > before[0] and state()[2] > before[2] + 1


def test_newest_articles_are_processed_first(feed_id, llm):
    old = add_article(feed_id, "Alpha old", age="-2 hours")
    new = add_article(feed_id, "Beta new")
    pipeline.process_pending()
    with db.get_conn() as conn:
        t = {r[0]: r[1] for r in conn.execute("SELECT id, processed_at FROM Article")}
    assert t[new] <= t[old]
    assert new > old


def test_low_trust_backlog_waits_for_the_rest(feed_id, llm, monkeypatch):
    with db.get_conn() as conn:
        reddit = conn.execute("INSERT INTO Feed (title, xml_url, category) "
                              "VALUES ('r', 'https://r/rss', 'Reddit')").lastrowid
    news = add_article(feed_id, "Alpha news", age="-2 hours")
    post = add_article(reddit, "Beta post")
    order = []
    real = pipeline.process_article
    monkeypatch.setattr(pipeline, "process_article", lambda art, *a, **k: order.append(art["id"]) or real(art, *a, **k))
    assert pipeline.process_pending() == {"processed": 2, "failed": 0}
    assert order == [news, post]


def test_batch_embedding_is_used_and_falls_back(feed_id, llm, monkeypatch):
    from app import clustering, ollama

    ids = [add_article(feed_id, f"Topic {n} story") for n in "abc"]
    assert pipeline.process_pending()["processed"] == 3
    assert llm.embed_batches == [3]
    with db.get_conn() as conn:
        assert all(clustering.get_vector(conn, i) is not None for i in ids)
        assert all(r[0] > 0 for r in conn.execute("SELECT t_embed_s FROM Article"))

    def boom(_texts):
        raise RuntimeError("embed count mismatch")
    monkeypatch.setattr(ollama, "embed_texts", boom)
    add_article(feed_id, "Topic d story")
    assert pipeline.process_pending()["processed"] == 1  # one-by-one fallback


def test_unparseable_prompt2_is_retried_once(feed_id, llm):
    llm.malformed = 1
    add_article(feed_id, "Flood hits town")
    assert pipeline.process_pending() == {"processed": 1, "failed": 0}
    assert llm.calls == ["harness", "story", "story"]
    llm.malformed = 2
    add_article(feed_id, "Volcano erupts", url="https://example.org/volcano")
    assert pipeline.process_pending() == {"processed": 0, "failed": 1}


def test_anchor_cache_follows_membership(feed_id, llm):
    from app import clustering

    llm.verdict = "UNRELATED"
    a, b = add_article(feed_id, "Flood hits town"), add_article(feed_id, "Volcano erupts")
    pipeline.process_pending()
    with db.get_conn() as conn:
        anchors = clustering.AnchorCache(conn)
        sa, sb = _story_of(a), _story_of(b)
        assert len(anchors(sa)) == 1
        conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (sa, b))
        assert len(anchors(sa)) == 2 and anchors(sb) == []


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
