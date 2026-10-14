import json

from app import config, db, scoring

from conftest import add_article


def _story(conn, headline="H", quality=6.0, updated="-1 hours", tags=("ai",), metrics=None):
    sid = conn.execute(
        "INSERT INTO Story (headline, summary, tags, metrics, quality_score, updated_at) "
        "VALUES (?, 'S', ?, ?, ?, datetime('now', ?))",
        (headline, json.dumps(list(tags)), json.dumps(metrics or {"urgency_level": 5}), quality, updated),
    ).lastrowid
    return sid


def test_components():
    assert scoring.credibility_score(1) == 0.25
    assert scoring.credibility_score(9) == 1.0
    assert scoring.time_decay_penalty(None) == 0.0
    assert scoring.distance_score({"location": "Munich, Germany"}) == 1.0
    assert scoring.distance_score({"location": "Berlin, Germany"}) == 0.6
    assert scoring.distance_score({"language": config.USER_LANGUAGE}) == 0.35
    assert scoring.distance_score({}) == 0.0
    home = {"home_city": "Berlin", "home_country": "", "language": "en"}
    assert scoring.distance_score({"location": "Berlin, Germany"}, home) == 1.0
    assert scoring.distance_score({"location": "Munich, Germany"}, home) == 0.6  # country falls back
    assert scoring.distance_score({"language": config.USER_LANGUAGE}, home) == 0.0


def test_ranking_archive_search_tag_and_min_quality(feed_id):
    with db.get_conn() as conn:
        fresh = _story(conn, "Fresh flood news", tags=("weather",))
        weak = _story(conn, "Weak gossip", quality=2.0)
        old = _story(conn, "Old story", updated=f"-{int(config.STORY_ACTIVE_DAYS) + 1} days")
        for sid in (fresh, weak, old):
            aid = add_article(feed_id, f"a{sid}")
            conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (sid, aid))

        ids = lambda items: [i["story"]["id"] for i in items]  # noqa: E731
        assert set(ids(scoring.ranked_stories(conn))) == {fresh, weak}
        assert ids(scoring.ranked_stories(conn, archived=True)) == [old]
        assert ids(scoring.ranked_stories(conn, query="flood")) == [fresh]
        assert ids(scoring.ranked_stories(conn, query="100%")) == []
        assert ids(scoring.ranked_stories(conn, tag="Weather")) == [fresh]

    scoring.update_profile({"min_quality": 5})
    with db.get_conn() as conn:
        assert ids(scoring.ranked_stories(conn)) == [fresh]


def test_source_trust_weights_credibility(feed_id):
    with db.get_conn() as conn:
        social, news = _story(conn, "Social"), _story(conn, "News")
        for i in range(3):
            fid = conn.execute("INSERT INTO Feed (title, xml_url, category) VALUES (?, ?, 'Reddit')",
                               (f"r{i}", f"https://r{i}.example/rss")).lastrowid
            conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (social, add_article(fid, f"post {i}")))
        conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (news, add_article(feed_id, "wire")))
        ranked = {i["story"]["id"]: i for i in scoring.ranked_stories(conn)}
    assert ranked[social]["source_count"] == 3 and ranked[social]["relevance"]["source_trust"] == 0.9
    assert ranked[news]["relevance"]["credibility"] == 2.5
    assert ranked[social]["relevance"]["credibility"] < ranked[news]["relevance"]["credibility"]

    scoring.update_profile(source_weights={"Reddit": 1.0})
    with db.get_conn() as conn:
        ranked = {i["story"]["id"]: i for i in scoring.ranked_stories(conn)}
    assert ranked[social]["relevance"]["credibility"] == 6.5


def _feed(conn, title, category, site=None):
    return conn.execute("INSERT INTO Feed (title, xml_url, site_url, category) VALUES (?, ?, ?, ?)",
                        (title, f"https://{title}.example/rss", site, category)).lastrowid


def _attach(conn, sid, fid, title, quality):
    aid = add_article(fid, title)
    conn.execute("UPDATE Article SET story_id = ?, quality = ? WHERE id = ?", (sid, quality, aid))


def test_weighted_median():
    assert scoring.weighted_median([(3, 1), (8, 1)]) == 5.5
    assert scoring.weighted_median([(3, 1), (5, 1), (8, 1)]) == 5
    assert scoring.weighted_median([(3, 0.3)] * 5 + [(8, 1.0)] * 2) == 8
    assert scoring.weighted_median([(3, 0)]) is None


def test_low_trust_reposts_dont_water_down_quality(feed_id):
    with db.get_conn() as conn:
        sid = _story(conn, quality=3.0)
        _attach(conn, sid, feed_id, "wire report", 8)
        for i in range(3):
            _attach(conn, sid, _feed(conn, f"r{i}", "Reddit"), f"repost {i}", 3)
        item = scoring.ranked_stories(conn)[0]
    assert item["quality"] == 8 and item["relevance"]["quality"] == 8.0
    scoring.update_profile({"min_quality": 7})
    with db.get_conn() as conn:
        assert [i["story"]["id"] for i in scoring.ranked_stories(conn)] == [sid]


def test_feeds_of_one_outlet_count_once(feed_id):
    with db.get_conn() as conn:
        sid = _story(conn)
        for i in range(3):
            _attach(conn, sid, _feed(conn, f"dlf{i}", "News", site="https://www.deutschlandfunk.de/x"), f"dlf {i}", 6)
        item = scoring.ranked_stories(conn)[0]
    assert item["source_count"] == 3 and item["relevance"]["source_trust"] == 1.0


def test_user_impact_matches_whole_words():
    prefs = {"locations": ["us"], "interest_regions": ["bavaria"]}
    assert scoring.user_impact_score(["music", "business"], prefs) == 0.5
    assert scoring.user_impact_score(["us-politics", "bavaria"], prefs) == 1.0


def test_feedback_nudges_and_profile_replaces_weights(feed_id):
    with db.get_conn() as conn:
        sid = _story(conn, tags=("AI", "Climate"))
    profile = scoring.apply_feedback(sid, "up")
    assert profile["tag_weights"] == {"ai": 1.0, "climate": 1.0}
    assert scoring.apply_feedback(999, "up") is None
    updated = scoring.update_profile(tag_weights={"ai": 50})
    assert updated["tag_weights"] == {"ai": 10.0}
    assert scoring.feedback_log()[0]["kind"] == "up"
