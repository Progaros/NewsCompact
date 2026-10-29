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


def test_ollama_timeouts_back_off_instead_of_dead_lettering(feed_id, llm, monkeypatch):
    import httpx

    from app import ollama

    real = llm.structured

    def slow(schema, messages, **kw):
        if "breaking" in schema["properties"]:
            raise httpx.ReadTimeout("timed out")
        return real(schema, messages, **kw)

    monkeypatch.setattr(ollama, "structured", slow)
    aid = add_article(feed_id, "Flood hits town")
    add_article(feed_id, "Market rallies")
    assert pipeline.process_pending() == {"processed": 0, "failed": 1}  # paused after the first timeout
    with db.get_conn() as conn:
        a = conn.execute("SELECT * FROM Article WHERE id = ?", (aid + 1,)).fetchone()  # newest goes first
        assert conn.execute("SELECT count(*) FROM Story").fetchone()[0] == 0
    assert a["pipeline_failures"] == 0 and a["pipeline_timeouts"] == 1 and a["retry_after"]
    assert pipeline.process_pending() == {"processed": 0, "failed": 1}  # the other article, then pause
    assert pipeline.process_pending() == {"processed": 0, "failed": 0}  # both waiting out their backoff

    for _ in range(config.PIPELINE_MAX_TIMEOUTS - 1):
        with db.get_conn() as conn:
            conn.execute("UPDATE Article SET retry_after = NULL WHERE id = ?", (aid,))
        pipeline.process_pending()
    with db.get_conn() as conn:
        assert conn.execute("SELECT pipeline_failures FROM Article WHERE id = ?", (aid,)).fetchone()[0] == \
            config.PIPELINE_MAX_FAILURES  # parked after PIPELINE_MAX_TIMEOUTS in a row

    monkeypatch.setattr(ollama, "structured", real)
    with db.get_conn() as conn:
        conn.execute("UPDATE Article SET retry_after = NULL WHERE id = ?", (aid + 1,))
    assert pipeline.process_pending() == {"processed": 1, "failed": 0}
    with db.get_conn() as conn:
        ok = conn.execute("SELECT pipeline_timeouts, retry_after FROM Article WHERE id = ?", (aid + 1,)).fetchone()
    assert tuple(ok) == (0, None)


def test_verifier_timeout_retries_the_article_instead_of_a_new_story(feed_id, llm, monkeypatch):
    import httpx

    from app import ollama

    add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    real = llm.structured

    def slow_verify(schema, messages, **kw):
        if "classifications" in schema["properties"]:
            raise httpx.ReadTimeout("timed out")
        return real(schema, messages, **kw)

    monkeypatch.setattr(ollama, "structured", slow_verify)
    second = add_article(feed_id, "Flood update")
    assert pipeline.process_pending() == {"processed": 0, "failed": 1}
    assert _story_of(second) is None
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM Story").fetchone()[0] == 1


def test_long_summary_is_shortened_then_clipped(feed_id, llm, monkeypatch):
    from app import ollama, story

    monkeypatch.setattr(config, "SUMMARY_MAX_CHARS", 120)
    real, asks = llm.structured, []

    def wordy(schema, messages, **kw):
        out = real(schema, messages, **kw)
        if "summary" in schema["properties"]:
            asks.append(messages[-1]["content"])
            out["summary"] = "A fact about the flood. " * (12 if len(asks) == 1 else 6)
        return out

    monkeypatch.setattr(ollama, "structured", wordy)
    aid = add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    with db.get_conn() as conn:
        summary = conn.execute("SELECT summary FROM Story WHERE id = ?", (_story_of(aid),)).fetchone()[0]
    assert len(asks) == 2 and "the limit is 120" in asks[1]
    assert len(summary) <= 120 and summary.endswith("flood.")
    assert story.clip_summary("word " * 40).endswith("…")


def test_boilerplate_bodies_are_reextracted_and_stories_rewritten(feed_id, llm, monkeypatch):
    from app import ingest

    sidebar = "\n".join(f"- Other headline {i}" for i in range(16)) + " padding" * 40
    real = {f"https://example.org/{t}": f"Real text about {t}. " + "fact " * 150 for t in ("Flood", "Fire")}
    monkeypatch.setattr(ingest, "fetch_page_text", lambda url: real.get(url, sidebar))
    flood = add_article(feed_id, "Flood hits town", body=sidebar, url="https://example.org/Flood")
    keeper = add_article(feed_id, "Flood rescue continues")
    pipeline.process_pending()
    old_story = _story_of(flood)
    assert _story_of(keeper) == old_story
    fire = add_article(feed_id, "Fire in mill", body=sidebar, url="https://example.org/Fire")
    storm = add_article(feed_id, "Storm warning", body=sidebar, url="https://example.org/Storm")
    llm.calls.clear()

    pipeline.process_pending()
    with db.get_conn() as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT id, content_raw, content_class, story_id FROM Article")}
    assert rows[flood]["content_raw"].startswith("Real text about Flood")
    assert rows[fire]["content_raw"].startswith("Real text about Fire")
    assert rows[storm]["content_raw"] == "" and rows[storm]["content_class"] == "empty"  # page still chrome
    assert all(rows[a]["story_id"] is not None for a in (flood, fire, storm))  # re-processed
    assert rows[keeper]["story_id"] == old_story
    assert llm.calls[0] == "story"  # the story that kept a member was rewritten first
    assert pipeline.repair_boilerplate_bodies() == 0  # idempotent


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


def test_headline_follows_notable_developments_only(feed_id, llm):
    from app import story

    def headline():
        with db.get_conn() as conn:
            return conn.execute("SELECT headline FROM Story").fetchone()[0]

    add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    first = headline()
    llm.significance = 1
    add_article(feed_id, "Flood update minor")
    pipeline.process_pending()
    assert headline() == first
    llm.significance = 2
    add_article(feed_id, "Flood update notable")
    pipeline.process_pending()
    assert headline() != first and headline().startswith("Headline")

    assert story.pick_headline(None, "New", True, 0) == "New"
    assert story.pick_headline("Old", "New", False, 0) == "New"  # full rewrite (merge, rescore)
    assert story.pick_headline("Old", "A; B", True, 3) == "Old"
    assert story.pick_headline("Old", "x" * 201, True, 3) == "Old"


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


def test_feed_trust_decides_the_backlog_order(feed_id, llm, monkeypatch):
    with db.get_conn() as conn:
        rumors = conn.execute("INSERT INTO Feed (title, xml_url, category, trust) "
                              "VALUES ('rumors', 'https://rumors/rss', 'News', 0.3)").lastrowid
        good = conn.execute("INSERT INTO Feed (title, xml_url, category, trust) "
                            "VALUES ('good', 'https://good/rss', 'Reddit', 0.9)").lastrowid
    news = add_article(good, "Alpha news", age="-2 hours")
    post = add_article(rumors, "Beta post")
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


def test_word_for_word_copy_skips_harness_and_prompt2(feed_id, llm):
    body = "Flood waters rose in the town overnight. " * 40
    first = add_article(feed_id, "Flood hits town", body=body)
    pipeline.process_pending()
    with db.get_conn() as conn:
        summary = conn.execute("SELECT summary FROM Story").fetchone()[0]
    llm.calls.clear()
    copy = add_article(feed_id, "Flood hits the town", body=body[: len(body) // 2])  # reprint, shortened
    pipeline.process_pending()
    assert _story_of(copy) == _story_of(first)
    assert llm.calls == ["verify"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT quality FROM Article WHERE id = ?", (copy,)).fetchone()[0] == 7
        assert conn.execute("SELECT summary FROM Story").fetchone()[0] == summary

    llm.calls.clear()
    update = add_article(feed_id, "Flood hits town", body=body + "Two people died.",
                         url="https://example.org/flood-update")
    pipeline.process_pending()
    assert _story_of(update) == _story_of(first)
    assert llm.calls == ["verify", "harness", "story"]  # one new word is a possible new fact


def test_ollama_timings_are_collected_per_thread():
    from app import ollama

    ollama.take_stats()
    ollama._record({"load_duration": 2e9, "prompt_eval_count": 900, "prompt_eval_duration": 5e8,
                    "eval_count": 300, "eval_duration": 6e9})
    ollama._record({"prompt_eval_count": 100, "prompt_eval_duration": 1e8, "eval_count": 20, "eval_duration": 4e8})
    assert ollama.take_stats() == {"calls": 2, "load_s": 2.0, "in_tok": 1000, "in_s": 0.6,
                                   "out_tok": 320, "out_s": 6.4}
    assert ollama.take_stats()["calls"] == 0
