import json
import math

import pytest

from app import config, db, scoring, tags

from conftest import add_article


def _story(conn, headline="H", quality=6.0, updated="-1 hours", tags=("ai",), metrics=None):
    sid = conn.execute(
        "INSERT INTO Story (headline, summary, tags, metrics, quality_score, updated_at) "
        "VALUES (?, 'S', ?, ?, ?, datetime('now', ?))",
        (headline, json.dumps(list(tags)), json.dumps(metrics or {"urgency_level": 5}), quality, updated),
    ).lastrowid
    return sid


def _ranked(conn, **kw):
    return {i["story"]["id"]: i for i in scoring.ranked_stories(conn, **kw)}


def test_components():
    assert scoring.coverage_score(0) == 0.0
    assert scoring.coverage_score(2) == pytest.approx(1 - math.exp(-1))
    gains = [scoring.coverage_score(n + 1) - scoring.coverage_score(n) for n in range(6)]
    assert gains == sorted(gains, reverse=True)  # each extra outlet adds less
    assert scoring.soft_cap(-0.2) == 0 and scoring.soft_cap(0.5) == 0.5
    assert scoring.soft_cap(1.0) < scoring.soft_cap(1.3) < 1.0  # no tie at the top
    assert scoring.distance_score({"location": "Munich, Germany"}) == 1.0
    assert scoring.distance_score({"location": "Berlin, Germany"}) == 0.6
    assert scoring.distance_score({"language": config.USER_LANGUAGE}) == 0.35
    assert scoring.distance_score({}) == 0.0
    home = {"home_city": "Berlin", "home_country": "", "language": "en"}
    assert scoring.distance_score({"location": "Berlin, Germany"}, home) == 1.0
    assert scoring.distance_score({"location": "Munich, Germany"}, home) == 0.6  # country falls back
    assert scoring.distance_score({"language": config.USER_LANGUAGE}, home) == 0.0
    assert scoring.geo_score(["bavaria"], {}, {"interest_regions": ["bavaria"], "language": "xx"}) == (
        0.5, "region", ["bavaria"])


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
    assert ranked[social]["source_count"] == 3 and ranked[social]["relevance"]["outlet_trust"] == 0.9
    assert ranked[news]["relevance"]["coverage"] == 0.39
    assert ranked[social]["relevance"]["coverage"] < ranked[news]["relevance"]["coverage"]

    scoring.update_profile(source_weights={"Reddit": 1.0})
    with db.get_conn() as conn:
        ranked = {i["story"]["id"]: i for i in scoring.ranked_stories(conn)}
    assert ranked[social]["relevance"]["coverage"] == 0.78


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


def test_cross_feed_copies_count_once_in_quality(feed_id):
    from app import story

    with db.get_conn() as conn:
        sid = _story(conn)
        wire = add_article(feed_id, "wire copy")
        conn.execute("UPDATE Article SET story_id = ?, quality = 3 WHERE id = ?", (sid, wire))
        for i in range(3):
            aid = add_article(_feed(conn, f"c{i}", "News"), f"copy {i}")
            conn.execute("UPDATE Article SET story_id = ?, quality = 3, duplicate_of = ? WHERE id = ?", (sid, wire, aid))
        _attach(conn, sid, feed_id, "report one", 8)
        _attach(conn, sid, feed_id, "report two", 8)
        item = scoring.ranked_stories(conn)[0]
        story.refresh_quality(conn, sid)
        stored = conn.execute("SELECT quality_score FROM Story WHERE id = ?", (sid,)).fetchone()[0]
    assert item["quality"] == 8 and stored == 8 and item["outlet_count"] == 4


def test_feeds_of_one_outlet_count_once(feed_id):
    with db.get_conn() as conn:
        sid = _story(conn)
        for i in range(3):
            _attach(conn, sid, _feed(conn, f"dlf{i}", "News", site="https://www.deutschlandfunk.de/x"), f"dlf {i}", 6)
        item = scoring.ranked_stories(conn)[0]
    assert item["source_count"] == 3 and item["outlet_count"] == 1 and item["relevance"]["outlet_trust"] == 1.0


def test_feed_subdomains_and_outlet_groups_are_one_outlet(monkeypatch):
    monkeypatch.setattr(config, "OUTLET_GROUPS", {"deutschlandfunkkultur.de": "deutschlandfunk.de"})
    assert scoring.outlet_key(None, "https://feeds.bbci.co.uk/news/rss.xml") == "bbci.co.uk"
    assert scoring.outlet_key(None, "https://nachrichtenfeeds.br.de/rdf") == "br.de"
    assert scoring.outlet_key("https://www.deutschlandfunkkultur.de/", None) == "deutschlandfunk.de"
    assert scoring.outlet_key(None, "https://googleprojectzero.blogspot.com/feeds") == "googleprojectzero.blogspot.com"


def test_region_hits_match_whole_words():
    prefs = {"locations": ["us"], "interest_regions": ["bavaria"]}
    assert scoring.region_hits(["music", "business"], prefs) == []
    assert scoring.region_hits(["us-politics", "bavaria"], prefs) == ["bavaria", "us"]


def test_freshness_is_multiplicative_and_status_dependent(feed_id):
    with db.get_conn() as conn:
        ids = {}
        for status, age in (("Ongoing", 0), ("Ongoing", 30), ("Breaking", 12), ("Resolved", 0)):
            sid = _story(conn, f"{status} {age}", updated=f"-{age} hours",
                         metrics={"urgency_level": 9, "status": status, "impact_scale": "Global"})
            conn.execute("UPDATE Story SET news_at = updated_at WHERE id = ?", (sid,))
            conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (sid, add_article(feed_id, f"a{sid}")))
            ids[(status, age)] = sid
        r = _ranked(conn)
    fresh, old = r[ids[("Ongoing", 0)]], r[ids[("Ongoing", 30)]]
    assert old["relevance"]["freshness"] == pytest.approx(0.5, abs=0.01)
    assert old["display"] < fresh["display"] * 0.55  # no amount of value outruns age
    assert r[ids[("Breaking", 12)]]["relevance"]["freshness"] == pytest.approx(0.5, abs=0.01)
    resolved = r[ids[("Resolved", 0)]]["relevance"]
    assert resolved["parts"]["urgency"] == pytest.approx(100 * config.W_PUB_URGENCY * config.RESOLVED_URGENCY_CAP, abs=0.1)
    assert all(0 <= i["display"] <= 100 for i in r.values())


def test_new_facts_raise_velocity_and_boost(feed_id):
    with db.get_conn() as conn:
        calm, busy = _story(conn, "Calm"), _story(conn, "Busy")
        for sid in (calm, busy):
            conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (sid, add_article(feed_id, f"a{sid}")))
        for _ in range(3):
            conn.execute("INSERT INTO StoryUpdate (story_id, significance, facts) VALUES (?, 3, '[\"x\"]')", (busy,))
        r = _ranked(conn)
    assert r[busy]["relevance"]["velocity"] > r[calm]["relevance"]["velocity"]
    assert r[busy]["relevance"]["updates_24h"] == 3 and r[busy]["relevance"]["parts"]["news"] > 0
    assert r[busy]["display"] > r[calm]["display"]


def test_tag_affinity_is_bounded_and_specific():
    profile = {"tag_weights": {"ai": 10, "macos": -8, "rust": 1}, "inferred_weights": {"germany": 3, "tollwood": 3}}
    sig = {s["tag"]: s for s in scoring.tag_signals(["ai", "macos", "rust", "germany", "tollwood"], profile,
                                                      (100, {"germany": 90, "tollwood": 1}))}
    assert sig["ai"]["signal"] == pytest.approx(math.tanh(2.5)) and sig["ai"]["source"] == "list"
    assert sig["germany"]["signal"] < sig["tollwood"]["signal"] / 3  # generic tags count little
    one = scoring.tag_affinity([sig["ai"]])
    assert one > 0.98 and scoring.tag_affinity([sig["ai"], sig["rust"]]) >= one  # an extra weak match never lowers
    assert abs(scoring.tag_affinity([sig["ai"], sig["macos"]])) < 0.05  # conflicting tags cancel
    many = [{"tag": str(i), "signal": 0.9, "source": "list"} for i in range(6)]
    assert scoring.tag_affinity(many) <= 1.0


def test_personal_value_is_bounded(feed_id):
    """A weak story with a perfect tag match can't beat a strong public story by more than W_AFFINITY."""
    with db.get_conn() as conn:
        weak = _story(conn, "Weak", quality=3.0, tags=("tollwood",), metrics={"urgency_level": 2, "impact_scale": "Local"})
        strong = _story(conn, "Strong", quality=9.0, tags=("war",), metrics={"urgency_level": 9, "impact_scale": "Global"})
        for sid in (weak, strong):
            conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (sid, add_article(feed_id, f"a{sid}")))
    scoring.update_profile(tag_weights={"tollwood": 10})
    with db.get_conn() as conn:
        r = _ranked(conn)
    assert r[weak]["relevance"]["parts"]["tags"] <= 100 * config.W_AFFINITY
    assert r[weak]["relevance"]["matches"] == [{"tag": "tollwood", "signal": 0.99, "source": "list"}]
    assert r[strong]["display"] > r[weak]["display"] - 100 * config.W_AFFINITY


def test_muted_tag_hides_story_from_personal_feed(feed_id):
    with db.get_conn() as conn:
        muted, kept = _story(conn, "Muted", tags=("baseball", "ai")), _story(conn, "Kept")
        for sid in (muted, kept):
            conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (sid, add_article(feed_id, f"a{sid}")))
    scoring.update_profile(tag_weights={"baseball": -10, "ai": 10})
    with db.get_conn() as conn:
        assert set(_ranked(conn)) == {kept}
        assert set(_ranked(conn, personal=False)) == {muted, kept}
        assert set(_ranked(conn, tag="baseball")) == {muted}  # an explicit tag filter still finds it


def test_votes_are_learned_not_written_to_the_tag_list(feed_id):
    with db.get_conn() as conn:
        sid = _story(conn, tags=("AI", "Climate"))
    scoring.update_profile(tag_weights={"ai": 3})
    profile = scoring.apply_feedback(sid, "up")
    assert profile["tag_weights"] == {"ai": 3.0}
    unit = config.VOTE_WEIGHT / math.sqrt(2)
    assert profile["inferred_weights"] == pytest.approx({"ai": unit, "climate": unit}, abs=0.01)
    switched = scoring.apply_feedback(sid, "down")
    assert switched["inferred_weights"] == pytest.approx({"ai": -unit, "climate": -unit}, abs=0.01)
    assert len(scoring.feedback_log()) == 1 and scoring.feedback_log()[0]["kind"] == "down"
    with db.get_conn() as conn:  # the tag list wins over what votes taught
        assert {s["tag"]: s["source"] for s in scoring.tag_signals(["ai", "climate"], switched)} == {
            "ai": "list", "climate": "learned"}
        conn.execute("UPDATE FeedbackLog SET created_at = datetime('now', '-90 days')")
        half = scoring.inferred_weights(conn, 1)
    assert half["climate"] == pytest.approx(-unit / 2, abs=0.01)  # down votes fade over 90 days
    cleared = scoring.apply_feedback(sid, None)
    assert cleared["inferred_weights"] == {} and scoring.feedback_log() == []
    assert scoring.apply_feedback(999, "up") is None
    updated = scoring.update_profile(tag_weights={"ai": 50})
    assert updated["tag_weights"] == {"ai": 10.0}


def test_story_chats_count_as_interest(feed_id):
    with db.get_conn() as conn:
        sid = _story(conn, tags=("rust",))
        conn.execute("INSERT INTO ChatSession (user_id, story_id, leaf_id) VALUES (1, ?, 1)", (sid,))
        assert scoring.inferred_weights(conn, 1) == pytest.approx({"rust": config.CHAT_WEIGHT})


def test_aliases_merge_tags_and_weights(feed_id):
    assert tags.normalize_tags(["München", "Munich", "KI", "Künstliche Intelligenz"]) == ["munich", "ai"]
    assert tags.merge_weights({"munchen": 10, "munich": 4, "politik": -3, "politics": 2}) == {
        "munich": 10.0, "politics": -3.0}
    with db.get_conn() as conn:
        conn.execute("INSERT INTO TagAlias (alias, canonical, status, method) VALUES "
                     "('datenschutz', 'privacy', 'approved', 'manual'), ('privacy', 'data privacy', 'approved', 'manual'), "
                     "('ruhr', 'germany', 'pending', 'manual')")
        tags.load_aliases(conn)
    try:
        assert tags.normalize_tags(["Datenschutz", "Privacy", "Ruhr"]) == ["data privacy", "ruhr"]  # chains resolve
        with db.get_conn() as conn:
            sid = _story(conn, tags=("Datenschutz",))
            conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (sid, add_article(feed_id, "a")))
        scoring.update_profile(tag_weights={"privacy": 5})
        assert scoring.get_user_profile()["tag_weights"] == {"data privacy": 5.0}
        with db.get_conn() as conn:
            assert _ranked(conn)[sid]["relevance"]["matches"][0]["tag"] == "data privacy"
    finally:
        tags.set_aliases({})
    tags.set_aliases({"a": "b", "b": "a"})  # cycles are dropped, not looped
    assert tags.normalize_tag("a") == "a"
    tags.set_aliases({})
