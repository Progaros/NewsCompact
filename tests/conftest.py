"""Shared fixtures: a fresh temp database per test and a fake (deterministic) LLM."""
from __future__ import annotations

import hashlib
import math
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db, ollama  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "HEADS_UP_NTFY_TIMES", [])
    db.init_db()
    return config.DB_PATH


def topic_vector(text: str) -> list[float]:
    """Unit vector determined by the first word: same first word -> same direction."""
    topic = (text.split() or ["news"])[0].lower()
    seed = hashlib.sha256(topic.encode()).digest()
    v = [((seed[i % 32] + i * 7) % 97) - 48.0 for i in range(config.EMBED_DIM)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class FakeLLM:
    def __init__(self):
        self.calls: list[str] = []
        self.verdict = "EXACT_MATCH"
        self.fail = False
        self.down = False

    def available(self):
        return not self.down

    def embed_one(self, text):
        if self.down:
            raise httpx.ConnectError("refused")
        return topic_vector(text)

    def structured(self, schema, messages, **_kw):
        if self.fail:
            raise RuntimeError("ollama down")
        props = schema["properties"]
        if "breaking" in props:
            self.calls.append("harness")
            return {"quality": 7, "breaking": False}
        if "classifications" in props:
            self.calls.append("verify")
            return {"classifications": [self.verdict] * 3}
        self.calls.append("story")
        user = messages[-1]["content"]
        return {
            "headline": "Headline " + user[:30].replace("\n", " "),
            "summary": "First sentence. Second sentence.",
            "tags": ["AI", "Machine-Learning"],
            "quality": 6,
            "location": "Munich, Germany",
            "language": "de",
            "metrics": {"urgency_level": 4, "impact_scale": "Local", "status": "Ongoing"},
        }

    def chat(self, messages, **_kw):
        self.calls.append("chat")
        return "brief"

    def chat_stream(self, messages, **_kw):
        self.calls.append("chat")
        yield from ("Short ", "answer.")


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(ollama, "embed_one", fake.embed_one)
    monkeypatch.setattr(ollama, "structured", fake.structured)
    monkeypatch.setattr(ollama, "chat", fake.chat)
    monkeypatch.setattr(ollama, "chat_stream", fake.chat_stream)
    monkeypatch.setattr(ollama, "available", fake.available)
    return fake


@pytest.fixture
def feed_id():
    with db.get_conn() as conn:
        return conn.execute(
            "INSERT INTO Feed (title, xml_url, category) VALUES ('Feed', 'https://example.org/rss', 'News')"
        ).lastrowid


def add_article(feed_id: int, title: str, body: str = "", url: str | None = None, **extra) -> int:
    from app.content_quality import classify

    body = body or ("word " * 300)
    with db.get_conn() as conn:
        return conn.execute(
            "INSERT INTO Article (feed_id, title, url, content_raw, content_class, created_at, published_date) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', ?), datetime('now'))",
            (feed_id, title, url or f"https://example.org/{title.replace(' ', '-')}",
             body, extra.get("content_class", classify(body)), extra.get("age", "-1 minutes")),
        ).lastrowid
