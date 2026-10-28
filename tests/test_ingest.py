import httpx
import feedparser
import pytest

from app import config, db, ingest

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Post A</title><link>https://www.reddit.com/r/x/comments/1/a/</link>
<description>Reading time: 1 min | Post permalink Hello world text. Top comments nice Comment permalink</description>
<pubDate>Mon, 12 Oct 2026 11:29:25 +0200</pubDate></item>
<item><title>Post B</title><link>https://news.example/b</link><description>short</description></item>
</channel></rss>"""


def _response(content: bytes, status=200, headers=None) -> httpx.Response:
    return httpx.Response(status, content=content, headers=headers or {},
                          request=httpx.Request("GET", "https://x"))


def test_published_date_is_utc():
    entry = feedparser.parse(RSS).entries[0]
    assert ingest._published_utc(entry) == "2026-10-12 09:29:25"


def test_content_encoded_beats_teaser_description():
    rss = b"""<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>T</title><item><title>A</title><link>https://x/a</link><description>Teaser.</description>
<content:encoded><![CDATA[<p>Full article text here.</p>]]></content:encoded></item></channel></rss>"""
    assert ingest._entry_description(feedparser.parse(rss).entries[0]) == "Full article text here."
    assert ingest._entry_description(feedparser.parse(RSS).entries[1]) == "short"


def test_link_list_extraction_is_rejected():
    sidebar = "\n".join(f"- Headline {i} with a teaser sentence about something" for i in range(16))
    assert ingest.is_link_list(sidebar)
    assert not ingest.is_link_list("Intro paragraph.\n- one point\n- two\nMore text.\nEven more.")
    article = "<html><body><article>" + "".join(f"<p>{'word ' * 30}</p>" for _ in range(5)) + "</article></body></html>"
    assert ingest.extract_content(article, "https://x/a").startswith("word")


def test_worth_fetching_skips_social_and_media_hosts():
    assert ingest._worth_fetching("https://news.example/a")
    assert not ingest._worth_fetching("https://www.reddit.com/r/x")
    assert not ingest._worth_fetching("https://i.redd.it/abc.png")
    assert not ingest._worth_fetching("ftp://example.org/x")


def _feed(feed_id):
    with db.get_conn() as conn:
        return dict(conn.execute("SELECT * FROM Feed WHERE id = ?", (feed_id,)).fetchone())


def _rss(*items):
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link><description>{d}</description>"
        + (f"<pubDate>{p}</pubDate>" if p else "") + "</item>"
        for t, link, d, p in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{body}</channel></rss>'.encode()


def test_ingest_feed_stops_on_shutdown(feed_id, monkeypatch):
    monkeypatch.setattr(config, "RETENTION_DAYS", 0)
    monkeypatch.setattr(ingest, "fetch", lambda url, *_a: _response(RSS, headers={"ETag": "e1"}))
    monkeypatch.setattr(ingest, "fetch_page_text", lambda url: "word " * 200)
    assert ingest.ingest_feed(_feed(feed_id), should_stop=lambda: True) == 0
    calls = iter([False, False, True])  # stop after the first entry
    assert ingest.ingest_feed(_feed(feed_id), should_stop=lambda: next(calls)) == 1
    assert _feed(feed_id)["etag"] is None  # the feed is read again in full next run


def test_ingest_feed_cleans_bodies_dedups_and_fetches_html_only(feed_id, monkeypatch):
    fetched = []
    monkeypatch.setattr(config, "RETENTION_DAYS", 0)
    monkeypatch.setattr(ingest, "fetch", lambda url, *_a: _response(RSS, headers={"ETag": "e1"}))
    monkeypatch.setattr(ingest, "fetch_page_text", lambda url: fetched.append(url) or "word " * 200)
    feed = _feed(feed_id)

    assert ingest.ingest_feed(feed) == 2
    assert ingest.ingest_feed(feed) == 0  # dedup by URL
    assert fetched == ["https://news.example/b"]  # no page fetch for reddit
    with db.get_conn() as conn:
        rows = {r["title"]: r for r in conn.execute("SELECT * FROM Article")}
        assert rows["Post A"]["content_raw"] == "Hello world text.\n\nTop comments:\n- nice"
        assert rows["Post A"]["content_class"] == "thin"
        assert rows["Post B"]["content_class"] == "ok"  # extracted from the page
        f = conn.execute("SELECT etag, last_error FROM Feed WHERE id = ?", (feed_id,)).fetchone()
        assert f["etag"] == "e1" and f["last_error"] is None


def test_cross_feed_copies_are_marked_duplicate(feed_id, monkeypatch):
    with db.get_conn() as conn:
        other = conn.execute("INSERT INTO Feed (title, xml_url) VALUES ('B', 'https://b/rss')").lastrowid
    title = "Parliament passes the new climate law today"
    feeds = {
        feed_id: _rss((title, "https://a.example/story?utm_source=x", "text " * 150, None),
                      ("Short title", "https://a.example/short", "text " * 150, None)),
        other: _rss((title + "!", "https://b.example/other-url", "text " * 150, None),
                    ("Different", "https://a.example/story/?fbclid=1", "text " * 150, None),
                    ("Short title", "https://b.example/short", "text " * 150, None)),
    }
    current = {}
    monkeypatch.setattr(ingest, "fetch", lambda url, *_a: _response(feeds[current["id"]]))
    for fid in (feed_id, other):
        current["id"] = fid
        ingest.ingest_feed(_feed(fid))
    with db.get_conn() as conn:
        rows = {r["url"]: r for r in conn.execute("SELECT id, url, duplicate_of FROM Article")}
    original = rows["https://a.example/story?utm_source=x"]["id"]
    assert rows["https://b.example/other-url"]["duplicate_of"] == original  # same title
    assert rows["https://a.example/story/?fbclid=1"]["duplicate_of"] == original  # same canonical URL
    assert rows["https://b.example/short"]["duplicate_of"] is None  # title too short to trust


def test_concurrently_stored_copies_collapse_to_one_original(feed_id, monkeypatch):
    from app import dedup

    with db.get_conn() as conn:
        other = conn.execute("INSERT INTO Feed (title, xml_url) VALUES ('B', 'https://b/rss')").lastrowid
    title = "Parliament passes the new climate law today"
    current = {}
    monkeypatch.setattr(ingest, "fetch", lambda url, *_a: _response(
        _rss((title, f"https://{current['id']}.example/story", "text " * 150, None))))
    current["id"] = feed_id
    ingest.ingest_feed(_feed(feed_id))
    real = dedup.find_original
    missed = []

    def racy(*a):  # the other feed's copy wasn't stored yet when this one checked
        if not missed:
            missed.append(1)
            return None
        return real(*a)

    monkeypatch.setattr(dedup, "find_original", racy)
    current["id"] = other
    ingest.ingest_feed(_feed(other))
    with db.get_conn() as conn:
        rows = conn.execute("SELECT id, duplicate_of FROM Article ORDER BY id").fetchall()
    assert [r["duplicate_of"] for r in rows] == [None, rows[0]["id"]]


def test_tombstoned_and_too_old_entries_are_skipped(feed_id, monkeypatch):
    import datetime as dt
    import email.utils

    old = email.utils.format_datetime(dt.datetime.now(dt.UTC) - dt.timedelta(days=config.RETENTION_DAYS + 1))
    rss = _rss(("Old one", "https://x/old", "t", old), ("Gone one", "https://x/gone", "t", None),
               ("Fresh one", "https://x/fresh", "t", None))
    monkeypatch.setattr(ingest, "fetch", lambda url, *_a: _response(rss))
    with db.get_conn() as conn:
        conn.execute("INSERT INTO ArticleTombstone (url) VALUES ('https://x/gone')")
    assert ingest.ingest_feed(_feed(feed_id)) == 1


def test_fetch_skips_non_ascii_validators(monkeypatch):
    sent = []

    def handler(request):
        sent.append(request.headers)
        return httpx.Response(200, content=RSS)

    monkeypatch.setattr(ingest, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    assert ingest.fetch("https://x/feed", '"abc\u201a"', "Sat, 24 Oct 2026 15:44:04 GMT").status_code == 200
    assert "If-None-Match" not in sent[0] and sent[0]["If-Modified-Since"].startswith("Sat")


def test_fetch_keeps_non_ascii_response_headers(monkeypatch):
    # muenchen-sehen.de sends "accelerometer=()‚ autoplay=..." (UTF-8 bytes) in Permissions-Policy.
    policy = "accelerometer=()\u201a autoplay=(self)".encode()
    monkeypatch.setattr(ingest, "_client", httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=RSS, headers=[(b"Permissions-Policy", policy), (b"ETag", b'"e1"')]))))
    r = ingest.fetch("https://x/feed")
    assert r.content == RSS and r.headers["ETag"] == '"e1"'


def test_fetch_retries_bot_walls_with_the_feed_user_agent(monkeypatch):
    agents = []

    def handler(request):
        ua = request.headers["User-Agent"]
        agents.append(ua)
        if ua == config.FEED_USER_AGENT:
            return httpx.Response(200, content=RSS)
        return httpx.Response(302 if "faz" in str(request.url) else 403 if "wiki" in str(request.url) else 404)

    monkeypatch.setattr(ingest, "_client", httpx.Client(transport=httpx.MockTransport(handler),
                                                       headers={"User-Agent": config.USER_AGENT}))
    assert ingest.fetch("https://faz.example/rss").content == RSS  # 302 without Location
    assert ingest.fetch("https://wiki.example/feed").content == RSS  # 403
    assert agents == [config.USER_AGENT, config.FEED_USER_AGENT] * 2
    agents.clear()
    with pytest.raises(httpx.HTTPStatusError):
        ingest.fetch("https://gone.example/feed")
    assert agents == [config.USER_AGENT]  # a plain 404 is not retried


def test_disabled_feeds_are_not_fetched(feed_id, monkeypatch):
    with db.get_conn() as conn:
        off = conn.execute("INSERT INTO Feed (title, xml_url, enabled) VALUES ('off', 'https://off/rss', 0)").lastrowid
    fetched = []
    monkeypatch.setattr(ingest, "fetch", lambda url, *_a: fetched.append(url) or _response(_rss()))
    ingest.ingest_all()
    assert fetched == [_feed(feed_id)["xml_url"]]
    assert _feed(off)["last_fetched_at"] is None


def test_fetch_error_is_recorded(feed_id, monkeypatch):
    def boom(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ingest, "fetch", boom)
    with db.get_conn() as conn:
        feed = dict(conn.execute("SELECT * FROM Feed WHERE id = ?", (feed_id,)).fetchone())
    assert ingest.ingest_feed(feed) == 0
    with db.get_conn() as conn:
        assert "refused" in conn.execute("SELECT last_error FROM Feed").fetchone()[0]


def test_failing_feed_backs_off_and_recovers(feed_id, monkeypatch):
    calls = []

    def boom(url, *_a):
        calls.append(url)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ingest, "fetch", boom)
    ingest.ingest_all()
    ingest.ingest_all()  # still inside the backoff window
    assert len(calls) == 1
    with db.get_conn() as conn:
        assert conn.execute("SELECT fetch_failures FROM Feed").fetchone()[0] == 1
        conn.execute("UPDATE Feed SET next_fetch_at = datetime('now', '-1 second')")
    ingest.ingest_all()
    with db.get_conn() as conn:
        failures, wait = conn.execute("SELECT fetch_failures, (julianday(next_fetch_at) - julianday('now')) * 86400 "
                                      "FROM Feed").fetchone()
    assert failures == 2 and wait > config.WORKER_INTERVAL_SECONDS * 1.5  # doubled

    monkeypatch.setattr(ingest, "fetch", lambda url, *_a: _response(RSS))
    with db.get_conn() as conn:
        conn.execute("UPDATE Feed SET next_fetch_at = datetime('now', '-1 second')")
    ingest.ingest_all()
    with db.get_conn() as conn:
        assert tuple(conn.execute("SELECT fetch_failures, next_fetch_at, last_error FROM Feed").fetchone()) == (
            0, None, None)
