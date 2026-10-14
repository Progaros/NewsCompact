"""Internet exposure with a password set: authz matrix, session, CSRF, headers, SSRF, DoS guards."""
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app import api, auth, bus, config, heads_up, ingest, pipeline, worker

from conftest import add_article

BASE = "https://news.example.org"
PASSWORD = "correct horse battery staple 42"

# Every route that must reject anonymous internet clients (method, path).
PRIVATE = [
    ("POST", "/api/stories/1/read"), ("DELETE", "/api/stories/1/read"),
    ("GET", "/api/stories/1/chat"), ("POST", "/api/stories/1/chat"), ("POST", "/api/stories/1/chat/switch"),
    ("POST", "/api/feedback"), ("GET", "/api/feedback"),
    ("GET", "/api/profile"), ("PUT", "/api/profile"),
    ("GET", "/api/reports/pipeline"), ("POST", "/api/reports/pipeline/send"),
    ("POST", "/api/refresh"),
    ("GET", "/api/feeds"), ("POST", "/api/feeds"), ("GET", "/api/feeds/preview?url=https://example.org"),
    ("POST", "/api/feeds/category"), ("PATCH", "/api/feeds/1"), ("DELETE", "/api/feeds/1"),
]
PUBLIC = ["/", "/list", "/api/stories", "/api/stories/1", "/api/articles", "/api/heads-up",
          "/api/health", "/api/auth/session", "/static/app.js"]


@pytest.fixture
def exposed(monkeypatch, llm, feed_id):
    monkeypatch.setattr(config, "AUTH_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "PUBLIC_BASE", BASE)
    monkeypatch.setattr(worker, "start", lambda: None)
    auth._sessions.clear()
    auth._failures.clear()
    add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    with TestClient(api.create_app(), base_url=BASE, client=("203.0.113.7", 40000)) as c:
        yield c


def _login(c):
    r = c.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    return r


def test_anonymous_internet_client_gets_only_public_views(exposed):
    for method, path in PRIVATE:
        r = exposed.request(method, path, json={})
        assert r.status_code == 401, (method, path, r.status_code)
    for path in PUBLIC:
        assert exposed.get(path).status_code == 200, path
    assert exposed.get("/feeds", follow_redirects=False).status_code == 303
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert exposed.get(path).status_code in (401, 404), path
    traversal = ("/static/../app/config.py", "/static/%2e%2e/api.py", "/static/..%2fconfig.py",
                 "/static/%2e%2e%2fconfig.py")
    _login(exposed)
    for path in traversal:
        r = exposed.get(path)
        assert r.status_code == 404 and "AUTH_PASSWORD" not in r.text, path


def test_login_cookie_flags_session_and_logout(exposed):
    assert exposed.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    r = _login(exposed)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    token = exposed.cookies.get(config.AUTH_COOKIE)
    assert len(token) >= 40
    assert exposed.get("/api/profile").status_code == 200
    assert exposed.post("/api/auth/logout").status_code == 200
    exposed.cookies.set(config.AUTH_COOKIE, token)  # replaying the old token after logout
    assert exposed.get("/api/profile").status_code == 401
    exposed.cookies.set(config.AUTH_COOKIE, "forged-token")
    assert exposed.get("/api/profile").status_code == 401


def test_login_bruteforce_is_throttled_even_for_the_right_password(exposed):
    for _ in range(5):
        assert exposed.post("/api/auth/login", json={"password": "guess"}).status_code == 401
    assert exposed.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429
    assert exposed.post("/api/auth/login", json={"password": "x" * 2000}).status_code == 422


def test_cross_site_requests_are_rejected_even_with_a_session(exposed):
    _login(exposed)
    evil = {"Origin": "https://evil.example"}
    assert exposed.post("/api/stories/1/read", headers=evil).status_code == 403
    assert exposed.delete("/api/feeds/1", headers=evil).status_code == 403
    # Lax cookies ride along on cross-site top-level GETs: the server must not act on them.
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert exposed.get("/api/feeds/preview?url=https://example.org", headers=cross).status_code == 403
    assert exposed.get("/api/reports/brief", headers=cross).status_code == 403
    assert exposed.get("/api/stories", headers=cross).status_code == 200  # public view stays linkable
    assert exposed.post("/api/stories/1/read", headers={"Origin": BASE}).status_code == 200


def test_security_headers(exposed):
    r = exposed.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_owner_read_state_is_not_public(exposed):
    _login(exposed)
    sid = exposed.get("/api/stories").json()["stories"][0]["id"]
    exposed.post(f"/api/stories/{sid}/read")
    assert exposed.get("/api/stories", params={"hide_read": "true"}).json()["hidden_read"] == 1
    exposed.post("/api/auth/logout")
    anon = exposed.get("/api/stories", params={"hide_read": "true"}).json()
    assert anon["hidden_read"] == 0 and anon["stories"][0]["read"] is None
    assert anon["stories"][0]["read_summary"] is None
    assert exposed.get(f"/api/stories/{sid}").json()["read"] is None


def test_input_bounds(exposed):
    _login(exposed)
    assert exposed.post("/api/feeds", json={"title": "x" * 301, "xml_url": "https://a.example/rss"}).status_code == 422
    assert exposed.post("/api/feeds", json={"title": "x", "xml_url": "javascript:alert(1)"}).status_code == 422
    assert exposed.post("/api/stories/1/chat", json={"message": "x" * 4001}).status_code == 422
    assert exposed.get("/api/stories", params={"limit": 100000}).status_code == 422
    assert exposed.get("/api/stories", params={"keep": "1," * 3000}).status_code == 422


def test_sse_connection_cap(exposed, monkeypatch):
    monkeypatch.setattr(bus, "subscriber_count", lambda: 10_000)
    assert exposed.get("/api/events").status_code == 503


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8787/api/profile", "http://localhost/", "http://127.0.0.1:11434/api/tags",
    "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
    "http://0x7f000001/", "http://0/", "file:///etc/passwd", "gopher://example.org/",
])
def test_non_public_targets_are_never_fetched(url):
    assert not ingest.is_public_url(url)


def test_feed_links_cannot_redirect_into_the_lan(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(302, headers={"Location": "http://192.168.1.1/admin"})

    monkeypatch.setattr(ingest, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    assert ingest.is_public_url("http://93.184.216.34/post")
    assert ingest.fetch_page_text("http://93.184.216.34/post") == ""
    assert seen == ["http://93.184.216.34/post"]  # the LAN hop was never requested


def test_public_heads_up_never_runs_parallel_generations(monkeypatch, feed_id, llm):
    add_article(feed_id, "Flood hits town")
    pipeline.process_pending()
    release, calls = threading.Event(), []

    def slow_chat(*_a, **_k):
        calls.append(1)
        release.wait(5)
        return "brief"

    monkeypatch.setattr(heads_up.ollama, "chat", slow_chat)
    heads_up._cache.clear()
    first = threading.Thread(target=heads_up.brief)
    first.start()
    while not calls:
        pass
    assert heads_up.brief() == (None, True)  # second caller: no new generation, no waiting
    release.set()
    first.join()
    assert len(calls) == 1 and heads_up.brief() == ("brief", True)
