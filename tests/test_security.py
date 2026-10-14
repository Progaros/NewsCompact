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
    ("GET", "/api/admin/users"), ("POST", "/api/admin/users"), ("PATCH", "/api/admin/users/2"),
    ("DELETE", "/api/admin/users/2"), ("POST", "/api/auth/password"), ("POST", "/api/share"),
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


def test_personal_ranking_needs_a_session(exposed):
    from app import scoring

    scoring.update_profile({"locations": ["munich"]}, tag_weights={"ai": 10})
    anon = exposed.get("/api/stories", params={"view": "personal"}).json()
    assert anon["view"] == "default" and anon["stories"][0]["relevance"]["user_interest"] == 0
    assert anon["stories"][0]["relevance"]["distance_points"] == 0
    _login(exposed)
    mine = exposed.get("/api/stories").json()
    assert mine["view"] == "personal" and mine["stories"][0]["relevance"]["user_interest"] == 10
    assert mine["stories"][0]["display_score"] > anon["stories"][0]["display_score"]
    default = exposed.get("/api/stories", params={"view": "default"}).json()
    assert default["view"] == "default" and default["stories"][0]["display_score"] == anon["stories"][0]["display_score"]
    sid = mine["stories"][0]["id"]
    assert exposed.get(f"/api/stories/{sid}", params={"view": "default"}).json()["relevance"]["user_interest"] == 0
    assert exposed.get("/api/stories", params={"view": "bogus"}).status_code == 422


def test_input_bounds(exposed):
    _login(exposed)
    assert exposed.post("/api/feeds", json={"title": "x" * 301, "xml_url": "https://a.example/rss"}).status_code == 422
    assert exposed.post("/api/feeds", json={"title": "x", "xml_url": "javascript:alert(1)"}).status_code == 422
    assert exposed.post("/api/stories/1/chat", json={"message": "x" * 4001}).status_code == 422
    assert exposed.get("/api/stories", params={"limit": 100000}).status_code == 422
    assert exposed.get("/api/stories", params={"keep": "1," * 3000}).status_code == 422


def test_users_have_separate_state_and_only_admins_manage(exposed):
    from app import db

    with db.get_conn() as conn:
        conn.execute("INSERT INTO AppUser (name, password_hash) VALUES ('anna', ?)", (auth.hash_password("anna-pw-1234"),))
    assert exposed.post("/api/auth/login", json={"username": "anna", "password": "wrong"}).status_code == 401
    assert exposed.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401
    r = exposed.post("/api/auth/login", json={"username": "ANNA", "password": "anna-pw-1234"})
    assert r.status_code == 200 and r.json()["user"] == {"id": 2, "name": "anna", "is_admin": False}
    assert exposed.get("/api/auth/session").json()["user"]["name"] == "anna"
    sid = exposed.get("/api/stories").json()["stories"][0]["id"]
    exposed.post(f"/api/stories/{sid}/read")
    exposed.post("/api/feedback", json={"story_id": sid, "kind": "up"})
    exposed.post(f"/api/stories/{sid}/chat", json={"message": "Anna asks"})
    anna = exposed.get("/api/profile").json()
    assert anna["tag_weights"] and "ntfy" not in anna and anna["user"]["is_admin"] is False
    for method, path in [("GET", "/api/feeds"), ("POST", "/api/refresh"), ("GET", "/api/reports/brief"),
                         ("DELETE", "/api/feeds/1")]:
        assert exposed.request(method, path).status_code == 403, path
    assert exposed.get("/feeds", follow_redirects=False).status_code == 303

    exposed.post("/api/auth/logout")
    _login(exposed)  # no user name = the owner, user 1 (admin)
    me = exposed.get("/api/auth/session").json()["user"]
    assert me == {"id": 1, "name": "admin", "is_admin": True}
    assert exposed.get("/api/stories").json()["stories"][0]["read"] is None
    assert exposed.get("/api/profile").json()["tag_weights"] == {}
    assert exposed.get(f"/api/stories/{sid}/chat").json()["messages"] == []
    assert exposed.get("/api/feeds").status_code == 200
    assert exposed.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200


def test_admin_manages_users(exposed):
    _login(exposed)
    url = "/api/admin/users"
    assert [u["name"] for u in exposed.get(url).json()["users"]] == ["admin"]
    assert exposed.post(url, json={"name": "x", "password": "long-enough"}).status_code == 422  # name too short
    assert exposed.post(url, json={"name": "bob", "password": "short"}).status_code == 422
    assert exposed.post(url, json={"name": "<script>", "password": "long-enough"}).status_code == 422
    users = exposed.post(url, json={"name": "bob", "password": "bob-pw-123"}).json()["users"]
    bob = users[-1]
    assert bob["name"] == "bob" and bob["is_admin"] == 0
    assert exposed.post(url, json={"name": "BOB", "password": "bob-pw-123"}).status_code == 409
    assert exposed.patch(f"{url}/1", json={"is_admin": False}).status_code == 409  # owner stays admin
    assert exposed.delete(f"{url}/1").status_code == 409
    assert exposed.patch(f"{url}/{bob['id']}", json={"is_admin": True}).json()["users"][-1]["is_admin"] == 1

    with TestClient(api.create_app(), base_url=BASE, client=("203.0.113.8", 40000)) as other:
        assert other.post("/api/auth/login", json={"username": "bob", "password": "bob-pw-123"}).status_code == 200
        assert other.get(url).status_code == 200  # promoted: bob is an admin now
        assert other.patch(f"{url}/{bob['id']}", json={"is_admin": False}).status_code == 409  # not yourself
        assert other.post("/api/auth/password", json={"current": "wrong", "new": "bob-new-pw"}).status_code == 403
        assert other.post("/api/auth/password", json={"current": "bob-pw-123", "new": "bob-new-pw"}).status_code == 200
        exposed.patch(f"{url}/{bob['id']}", json={"is_admin": False, "password": "reset-by-admin"})
        assert other.get("/api/profile").status_code == 401  # a reset ends the user's sessions
        assert other.post("/api/auth/login", json={"username": "bob", "password": "reset-by-admin"}).status_code == 200
        assert other.get(url).status_code == 403  # demoted
        other.post("/api/feedback", json={"story_id": 1, "kind": "up"})
        users = exposed.delete(f"{url}/{bob['id']}").json()["users"]
        assert [u["name"] for u in users] == ["admin"]
        assert other.get("/api/profile").status_code == 401
    assert exposed.get("/admin").status_code == 200


def test_share_links(exposed, monkeypatch):
    from app import heads_up

    import time

    heads_up._cache.clear()
    monkeypatch.setitem(heads_up._cache, "default", {"text": "Public brief", "ts": time.time()})
    monkeypatch.setitem(heads_up._cache, "user:1", {"text": "Brief for admin", "ts": time.time()})
    _login(exposed)
    sid = exposed.get("/api/stories").json()["stories"][0]["id"]
    assert exposed.get(f"/s/{sid}").status_code == 200

    public = exposed.post("/api/share", json={"kind": "brief", "view": "default"}).json()["url"]
    private = exposed.post("/api/share", json={"kind": "brief", "view": "personal"}).json()["url"]
    assert public.startswith("/p/") and len(public) == 3 + 8 and len(private) == 3 + 32
    exposed.post(f"/api/stories/{sid}/chat", json={"message": "What happened?"})
    answer = exposed.get(f"/api/stories/{sid}/chat").json()["messages"][-1]
    chat_url = exposed.post("/api/share", json={"kind": "chat", "story_id": sid, "message_id": answer["id"]}).json()["url"]
    assert exposed.post("/api/share", json={"kind": "chat", "story_id": sid,
                                            "message_id": answer["id"] - 1}).status_code == 404  # a question

    exposed.post("/api/auth/logout")
    assert exposed.get(public).status_code == 200
    snap = exposed.get("/api/share/" + private[3:]).json()
    assert snap["text"] == "Brief for admin" and snap["shared_by"] == "admin" and snap["view"] == "personal"
    chat_snap = exposed.get("/api/share/" + chat_url[3:]).json()
    assert chat_snap["question"] == "What happened?" and chat_snap["answer"] == "Short answer."
    assert exposed.get("/api/share/doesnotexist").status_code == 404
    assert exposed.get("/api/share/bad").status_code == 422
    assert exposed.get("/p/../../etc").status_code in (401, 404)
    assert exposed.get("/p/a%2F..%2F..%2Fetc").status_code in (404, 422)


def test_password_hashes():
    h = auth.hash_password("s3cret")
    assert h.startswith("scrypt$") and "s3cret" not in h and h != auth.hash_password("s3cret")
    assert auth.verify_password("s3cret", h) and not auth.verify_password("nope", h)
    assert not auth.verify_password("x", None) and not auth.verify_password("x", "garbage")


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
