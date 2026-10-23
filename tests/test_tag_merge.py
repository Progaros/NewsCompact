import json

import pytest

from app import db, ollama, scoring, tag_merge, tags

from conftest import topic_vector

CONCEPTS = {"erderwarmung": "warming", "global warming": "warming", "datenschutz": "privacy",
            "privacy": "privacy", "weather": "weather"}
VERDICTS = {frozenset({"erderwarmung", "global warming"}): ("SAME_CONCEPT", 0.95, "global warming"),
            frozenset({"datenschutz", "privacy"}): ("SAME_CONCEPT", 0.6, "privacy")}


@pytest.fixture
def fake(monkeypatch):
    calls = {"embedded": [], "verified": []}

    def embed_texts(texts):
        names = [t.split("Query: ", 1)[1] for t in texts]
        calls["embedded"] += names
        return [topic_vector(CONCEPTS.get(n, n)) for n in names]

    def structured(schema, messages, **_kw):
        user = messages[-1]["content"]
        a = user.split("Tag A: ", 1)[1].split("\n", 1)[0]
        b = user.split("Tag B: ", 1)[1].split("\n", 1)[0]
        calls["verified"].append((a, b))
        relation, confidence, canonical = VERDICTS.get(frozenset({a, b}), ("RELATED", 0.9, a))
        return {"relation": relation, "confidence": confidence, "canonical": "A" if canonical == a else "B"}

    monkeypatch.setattr(ollama, "embed_texts", embed_texts)
    monkeypatch.setattr(ollama, "structured", structured)
    with db.get_conn() as conn:
        for tag in CONCEPTS:
            for i in range(3):
                conn.execute("INSERT INTO Story (headline, tags) VALUES (?, ?)", (f"{tag} story {i}", json.dumps([tag])))
    yield calls
    tags.set_aliases({})


def test_only_confident_same_concept_pairs_merge(fake):
    dry = tag_merge.run(apply=False)
    assert dry["approved"] == 1 and dry["pending"] == 1
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM TagAlias").fetchone()[0] == 0
    assert tags.normalize_tag("Erderwärmung") == "erderwarmung"

    result = tag_merge.run()
    assert {(r["alias"], r["canonical"], r["status"]) for r in result["rows"]} == {
        ("erderwarmung", "global warming", "approved"), ("datenschutz", "privacy", "pending")}
    assert tags.normalize_tag("Erderwärmung") == "global warming"
    assert tags.normalize_tag("Datenschutz") == "datenschutz"  # pending = no effect
    assert "weather" not in {t for pair in fake["verified"] for t in pair}  # not similar enough

    embedded, verified = len(fake["embedded"]), len(fake["verified"])
    again = tag_merge.run()
    assert again["pairs"] == 0 and len(fake["verified"]) == verified  # decided pairs aren't asked again
    assert len(fake["embedded"]) == embedded  # vectors are cached


def test_review_and_manual_aliases(fake):
    tag_merge.run()
    scoring.update_profile(tag_weights={"datenschutz": 4, "privacy": -2})
    with db.get_conn() as conn:
        with pytest.raises(ValueError):
            tag_merge.review(conn, "global warming", "approved")  # nothing pending under that alias
        assert tag_merge.review(conn, "datenschutz", "approved") == 1
    assert tags.normalize_tag("Datenschutz") == "privacy"
    assert scoring.get_user_profile()["tag_weights"] == {"privacy": 4.0}  # strongest wins, no sum

    with db.get_conn() as conn:
        assert tag_merge.add_manual(conn, "Unwetter", "Weather") == ("unwetter", "weather")
        assert tags.normalize_tag("unwetter") == "weather"
        tag_merge.review(conn, "unwetter", "rejected")
        with pytest.raises(ValueError):
            tag_merge.add_manual(conn, "weather", "Weather")
    assert tags.normalize_tag("unwetter") == "unwetter"
