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


def test_feedback_nudges_and_profile_replaces_weights(feed_id):
    with db.get_conn() as conn:
        sid = _story(conn, tags=("AI", "Climate"))
    profile = scoring.apply_feedback(sid, "up")
    assert profile["tag_weights"] == {"ai": 1.0, "climate": 1.0}
    assert scoring.apply_feedback(999, "up") is None
    updated = scoring.update_profile(tag_weights={"ai": 50})
    assert updated["tag_weights"] == {"ai": 10.0}
    assert scoring.feedback_log()[0]["kind"] == "up"
