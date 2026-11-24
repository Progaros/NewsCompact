"""Experimental media / outlet analysis: SQL quality metrics, budgeted LLM sampling, /outlets API."""
from __future__ import annotations

from app import api, config, db, media_analysis, ollama, pipeline
from conftest import add_article


def test_media_analysis_sampling_budget_and_detail(client, feed_id, llm, monkeypatch):
    # Seed 4 feeds (3 others + feed_id) to test multi-outlet stories and blind spots.
    with db.get_conn() as conn:
        f2 = conn.execute("INSERT INTO Feed (title, xml_url, site_url) VALUES ('Beta', 'https://beta.example/rss', 'https://beta.example')").lastrowid
        f3 = conn.execute("INSERT INTO Feed (title, xml_url, site_url) VALUES ('Gamma', 'https://gamma.example/rss', 'https://gamma.example')").lastrowid
        f4 = conn.execute("INSERT INTO Feed (title, xml_url, site_url) VALUES ('Delta', 'https://delta.example/rss', 'https://delta.example')").lastrowid
        f5 = conn.execute("INSERT INTO Feed (title, xml_url, site_url) VALUES ('Eps', 'https://eps.example/rss', 'https://eps.example')").lastrowid

    body = "Parliament debated the new energy subsidy package in detail today. " * 25
    a1 = add_article(feed_id, "Energy bill sparks debate", content=body)
    pipeline.process_pending()

    # Create a major story covered by 4 other outlets (blind spot for feed_id's outlet).
    with db.get_conn() as conn:
        s_blind = conn.execute(
            "INSERT INTO Story (headline, summary, tags, metrics, quality_score, news_at) "
            "VALUES ('Major summit missed', 'Summary', '[\"politics\"]', '{\"urgency_level\": 8}', 8.0, datetime('now'))"
        ).lastrowid
        for fid in (f2, f3, f4, f5):
            aid = add_article(fid, f"Summit report {fid}", content=body)
            conn.execute("UPDATE Article SET story_id = ?, content_class = 'ok', quality = 8.0 WHERE id = ?", (s_blind, aid))
        # Ensure a1 is marked ok + has a story_id.
        conn.execute("UPDATE Article SET content_class = 'ok', quality = 7.5 WHERE id = ?", (a1,))

    # Mock anonymized LLM bias response and verify outlet name is NOT sent in prompt.
    seen_prompts: list[str] = []

    def fake_structured(schema, messages, **kw):
        assert kw.get("role") == "analysis"
        seen_prompts.append(messages[1]["content"])
        return {
            "political": True,
            "economic_axis": -0.4,
            "social_axis": 0.2,
            "tone": "opinionated",
            "clickbait": 3,
            "favored": ["Green Party", "Renewables"],
            "criticized": ["Coal Lobby"],
        }

    monkeypatch.setattr(ollama, "structured", fake_structured)
    monkeypatch.setattr(config, "MEDIA_ANALYSIS_ENABLED", True)

    res = media_analysis.run(max_articles=2, respect_budget=True)
    assert res["analyzed"] >= 1
    assert all("example.com" not in p and "beta.example" not in p for p in seen_prompts)

    # When an explicit cap is configured (e.g. 8%), exceeding it pauses background sampling.
    monkeypatch.setattr(config, "MEDIA_ANALYSIS_MAX_LLM_SHARE", 0.08)
    ollama.record_usage("qwen3-27b-IQ3", "story", in_tokens=900, out_tokens=100)
    ollama.record_usage("qwen3-27b-IQ3", "analysis", in_tokens=150, out_tokens=50)  # 200/1200 = 16.7% > 8%
    capped = media_analysis.run(max_articles=5, respect_budget=True)
    assert capped["analyzed"] == 0 and capped["skipped_budget"] is True

    # Overview & detail endpoints return compass coords, entities, and blind spots.
    ov = client.get("/api/outlets?days=30").json()
    assert ov["outlets"]
    assert ov["budget"]["share"] > 0.08
    first_outlet = next(o["outlet"] for o in ov["outlets"] if o["analyzed"] > 0)

    detail = client.get(f"/api/outlets/{first_outlet}?days=30").json()
    assert detail["summary"]["economic_axis"] == -0.4
    assert detail["summary"]["social_axis"] == 0.2
    assert detail["summary"]["favored"][0]["name"] == "Green Party"
    assert detail["summary"]["criticized"][0]["name"] == "Coal Lobby"
    assert len(detail["samples"]) >= 1

    # /feeds includes per-feed outlet + analysis summary.
    feeds_data = client.get("/api/feeds").json()["feeds"]
    assert any(f.get("outlet") == first_outlet and f.get("analysis") for f in feeds_data)


def test_outlets_auth_and_page(client):
    from fastapi.testclient import TestClient

    r = client.get("/outlets", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/analytics#outlets"
    page = client.get("/analytics")
    assert page.status_code == 200
    assert "compass-box" in page.text

    with TestClient(api.create_app(), follow_redirects=False) as anon:
        assert anon.get("/api/outlets").status_code == 401
        assert anon.get("/analytics").status_code == 303
