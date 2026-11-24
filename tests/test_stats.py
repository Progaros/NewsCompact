"""Model usage accounting: record_usage -> ModelTokenStats, /api/stats, /stats page."""
import datetime as dt

from app import config, db, ollama


def test_record_usage_aggregates(client):
    # Two calls into the same (model, role, day) bucket must fold together.
    day = ollama.record_usage("qwen3-27b-IQ3", "story", in_tokens=10, out_tokens=5, calls=1)
    assert day is not None
    assert ollama.record_usage("qwen3-27b-IQ3", "story", in_tokens=7, out_tokens=3, calls=1) == day
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT calls, in_tokens, out_tokens FROM ModelTokenStats "
            "WHERE model = 'qwen3-27b-IQ3' AND role = 'story' AND day = ?", (day,)
        ).fetchone()
    assert tuple(row) == (2, 17, 8)
    # A distinct role stays in its own row.
    assert ollama.record_usage("qwen3-27b-IQ3", "harness", in_tokens=1, out_tokens=1) == day
    with db.get_conn() as conn:
        rows = conn.execute("SELECT role FROM ModelTokenStats WHERE day = ? ORDER BY role", (day,)).fetchall()
    assert [r[0] for r in rows] == ["harness", "story"]


def test_record_usage_rejects_bad_role(client):
    # Unknown roles and zero-token calls are dropped (return None).
    assert ollama.record_usage("qwen3-27b-IQ3", "nope", in_tokens=5, out_tokens=1) is None
    assert ollama.record_usage("qwen3-27b-IQ3", "chat", in_tokens=0, out_tokens=0) is None
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ModelTokenStats WHERE role = 'nope'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ModelTokenStats WHERE in_tokens = 0 AND out_tokens = 0").fetchone()[0] == 0


def test_record_usage_day_buckets(client):
    # An explicit past day is kept separate from today.
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).date().isoformat()
    assert ollama.record_usage("qwen3-embedding", "embedding", in_tokens=100, day=past) == past
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT in_tokens FROM ModelTokenStats WHERE role = 'embedding' AND day = ?", (past,)
        ).fetchone()[0] == 100
        assert conn.execute(
            "SELECT COALESCE(SUM(in_tokens), 0) FROM ModelTokenStats "
            "WHERE role = 'embedding' AND day = ?", (db.utcnow()[:10],)
        ).fetchone()[0] == 0


def test_api_stats_requires_signin(client):
    # The client fixture is signed in; an anonymous client must be rejected.
    from fastapi.testclient import TestClient

    from app import api

    with TestClient(api.create_app(), follow_redirects=False) as anon:
        assert anon.get("/api/stats").status_code == 401
        assert anon.get("/stats").status_code == 303  # browsers are redirected to /


def test_api_stats_shape(client):
    ollama.record_usage("qwen3-27b-IQ3", "story", in_tokens=50, out_tokens=25)
    ollama.record_usage("qwen3-embedding", "embedding", in_tokens=40, out_tokens=0)
    data = client.get("/api/stats").json()
    assert data["days"] == 30
    assert data["chat_model"] == config.CHAT_MODEL
    assert data["embed_model"] == config.EMBED_MODEL
    assert data["llm"]["in_tokens"] == 50 and data["llm"]["out_tokens"] == 25
    assert data["llm"]["calls"] == 1
    assert data["embedding"]["in_tokens"] == 40
    assert data["embedding"]["calls"] == 1
    assert data["roles"]["story"] == {"calls": 1, "in_tokens": 50, "out_tokens": 25}
    assert data["roles"]["embedding"] == {"calls": 1, "in_tokens": 40, "out_tokens": 0}
    assert data["llm_models"] == ["qwen3-27b-IQ3"]
    assert data["embed_models"] == ["qwen3-embedding"]
    assert data["models"] == [
        {"model": "qwen3-27b-IQ3", "roles": ["story"], "calls": 1, "in_tokens": 50, "out_tokens": 25},
        {"model": "qwen3-embedding", "roles": ["embedding"], "calls": 1, "in_tokens": 40, "out_tokens": 0},
    ]
    # The series covers the whole window and reconciles with the rollups.
    assert len(data["series"]) == 30
    total = sum(d["tokens"] for d in data["series"])
    grand = data["llm"]["in_tokens"] + data["llm"]["out_tokens"] \
        + data["embedding"]["in_tokens"] + data["embedding"]["out_tokens"]
    assert total == grand
    for d in data["series"]:
        assert 0 <= d["embed_tokens"] <= d["tokens"]
    allowed = {"harness", "verify", "story", "chat", "brief", "embedding"}
    assert set(data["roles"]) <= allowed


def test_api_stats_days_window(client):
    a = client.get("/api/stats?days=7").json()
    b = client.get("/api/stats?days=1").json()
    assert len(a["series"]) == 7
    assert len(b["series"]) == 1
    # A 1-day window is a subset of the 7-day window.
    assert sum(d["tokens"] for d in b["series"]) <= sum(d["tokens"] for d in a["series"])


def test_api_stats_day_boundaries(client):
    # A row pinned to a past day must surface in that day's series entry.
    day = (dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).date().isoformat()
    assert ollama.record_usage("qwen3-embedding", "embedding", in_tokens=111, day=day) == day
    data = client.get("/api/stats").json()
    entry = next(d for d in data["series"] if d["day"] == day)
    assert entry["in_tokens"] >= 111
    assert entry["embed_tokens"] >= 111


def test_stats_page_served(client):
    r = client.get("/stats", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/analytics#usage"
    page = client.get("/analytics")
    assert page.status_code == 200
    assert "stat-chart" in page.text
    assert "analytics.js" in page.text


def test_chat_stream_synthesizes_usage_from_timings(monkeypatch):
    # llama.cpp sends ``timings`` (not OpenAI ``usage``) in the final chunk, so
    # chat_stream must derive prompt/completion tokens from prompt_n/predicted_n.
    lines = [
        'data: {"choices":[{"delta":{"content":"A "}}]}',
        'data: {"choices":[{"delta":{"content":"B"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"timings":{"prompt_n":11,"predicted_n":4}}',
    ]

    class _Stream:
        def iter_lines(self):
            return iter(lines)

        def raise_for_status(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _Client:
        def stream(self, *_a, **_k):
            return _Stream()

    monkeypatch.setattr(ollama, "_http", lambda: _Client())

    captured: dict = {}

    def on_usage(usage):
        captured.update(usage)

    chunks = list(ollama.chat_stream([{"role": "user", "content": "hi"}], on_usage=on_usage))
    assert "".join(text for _kind, text in chunks) == "A B"
    assert captured == {"prompt_tokens": 11, "completion_tokens": 4}


def test_chat_stream_reads_separate_openai_usage_chunk(monkeypatch):
    # OpenAI / Ollama with stream_options.include_usage send a trailing chunk with
    # choices=[] and usage={...}; it must be captured once and sent with stream_options.
    sent_payload: dict = {}
    lines = [
        'data: {"choices":[{"delta":{"content":"Hi"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":7}}',
        'data: [DONE]',
    ]

    class _Stream:
        def iter_lines(self):
            return iter(lines)

        def raise_for_status(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _Client:
        def stream(self, _method, _path, *, json, **_k):
            sent_payload.update(json)
            return _Stream()

    monkeypatch.setattr(ollama, "_http", lambda: _Client())

    calls: list[dict] = []
    chunks = list(ollama.chat_stream([{"role": "user", "content": "hi"}], on_usage=calls.append))
    assert "".join(text for _kind, text in chunks) == "Hi"
    assert sent_payload["stream_options"] == {"include_usage": True}
    assert calls == [{"prompt_tokens": 20, "completion_tokens": 7}]


def test_chat_and_embed_record_usage_on_native_ollama(monkeypatch):
    from app import stats

    class _Resp:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class _Client:
        def post(self, path, *, json, **_k):
            if path == "/api/embed":
                return _Resp({"embeddings": [[0.1, 0.2]], "prompt_eval_count": 33})
            if path == "/v1/chat/completions":
                return _Resp({
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 15, "completion_tokens": 6},
                })
            raise AssertionError(path)

    monkeypatch.setattr(config, "LLM_BASE_URL", "")
    monkeypatch.setattr(config, "EMBED_BASE_URL", "")
    monkeypatch.setattr(ollama, "_http", lambda: _Client())
    monkeypatch.setattr(ollama, "_embed_http", lambda: _Client())

    assert ollama.embed_texts(["hello"]) == [[0.1, 0.2]]
    assert ollama.chat([{"role": "user", "content": "hi"}], role="brief") == "ok"

    data = stats.summary()
    assert data["roles"]["embedding"]["in_tokens"] == 33
    assert data["roles"]["brief"] == {"calls": 1, "in_tokens": 15, "out_tokens": 6}


def test_embed_openai_handles_null_usage(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.5]}], "usage": None}

    class _Client:
        def post(self, *_a, **_k):
            return _Resp()

    monkeypatch.setattr(config, "EMBED_BASE_URL", "http://embed.test/v1")
    monkeypatch.setattr(ollama, "_embed_http", lambda: _Client())
    assert ollama.embed_texts(["x"]) == [[0.5]]


def test_structured_prose_fallback_preserves_role(monkeypatch):
    recorded_roles: list[str] = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": ""}}], "usage": {"prompt_tokens": 10, "completion_tokens": 0}}

    class _Client:
        def post(self, *_a, **_k):
            return _Resp()

    def fake_chat_json(messages, **kwargs):
        recorded_roles.append(kwargs.get("role"))
        return {"ok": True}

    monkeypatch.setattr(config, "LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setattr(ollama, "_http", lambda: _Client())
    monkeypatch.setattr(ollama, "chat_json", fake_chat_json)

    assert ollama.structured({}, [{"role": "user", "content": "x"}], prose_fallback=True, role="story") == {"ok": True}
    assert recorded_roles == ["story"]


def test_api_stats_models_respect_days_window(client):
    old_day = (dt.datetime.now(dt.UTC) - dt.timedelta(days=20)).date().isoformat()
    ollama.record_usage("old-model", "story", in_tokens=100, out_tokens=50, day=old_day)
    ollama.record_usage("new-model", "verify", in_tokens=20, out_tokens=10)

    week = client.get("/api/stats?days=7").json()
    month = client.get("/api/stats?days=30").json()
    assert week["llm_models"] == ["new-model"]
    assert set(month["llm_models"]) == {"old-model", "new-model"}
