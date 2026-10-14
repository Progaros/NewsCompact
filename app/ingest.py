"""RSS ingestion: concurrent feed fetch, full-text extraction, dedup by URL.

Bodies are normalized by content_quality.clean_body. Full-text extraction
(trafilatura → newspaper3k → BeautifulSoup) only runs for enabled feeds whose
cleaned description is shorter than EXTRACT_MIN_DESC_WORDS and whose link is
an HTML page worth fetching. Disabled feeds still store rows so their volume
stats stay live, but skip the page fetch.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import email.utils
import ipaddress
import logging
import re
import socket
import threading
from typing import Any, Callable

import feedparser
import httpx

from . import config, content_quality, db, dedup

log = logging.getLogger("news.ingest")

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def http() -> httpx.Client:
    """Shared pooled client for feeds and article pages (thread-safe)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=config.FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": config.USER_AGENT},
                limits=httpx.Limits(max_connections=config.FETCH_CONCURRENCY * 2),
            )
        return _client


def is_http_url(url: str | None) -> bool:
    return bool(url) and url.lower().startswith(("http://", "https://"))


def is_public_url(url: str) -> bool:
    """http(s) URL whose host resolves only to global addresses (no LAN, loopback, metadata)."""
    if not is_http_url(url):
        return False
    try:
        host = httpx.URL(url).host
        infos = socket.getaddrinfo(host, None)
    except (httpx.InvalidURL, OSError, UnicodeError):
        return False
    return bool(infos) and all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


# Pages with no extractable article text (media, JS apps, bot walls).
_NO_FETCH_HOSTS = ("reddit.com", "redd.it", "youtube.com", "youtu.be", "x.com", "twitter.com",
                   "instagram.com", "tiktok.com", "imgur.com")


def _worth_fetching(url: str | None) -> bool:
    if not is_http_url(url):
        return False
    try:
        host = (httpx.URL(url).host or "").lower()
    except httpx.InvalidURL:
        return False
    return not any(host == h or host.endswith("." + h) for h in _NO_FETCH_HOSTS)


_MAX_PAGE_BYTES = 5 * 1024 * 1024  # a stray video/image link must not be read into memory
_MAX_REDIRECTS = 5


def fetch_page_text(url: str) -> str:
    """Fetch an article page and extract its text ("" for non-HTML, oversized or non-public targets).

    Article links come from feed content (anyone can post a link on a subscribed
    subreddit), so every hop must resolve to a public address — otherwise LAN
    pages could be fetched and end up in public story summaries.
    """
    for _ in range(_MAX_REDIRECTS + 1):
        if not is_public_url(url):
            log.info("skip page fetch for non-public URL %s", url)
            return ""
        with http().stream("GET", url, follow_redirects=False) as r:
            if r.is_redirect:
                url = str(r.next_request.url) if r.next_request else ""
                continue
            r.raise_for_status()
            if "html" not in r.headers.get("content-type", "html").lower():
                return ""
            if int(r.headers.get("content-length") or 0) > _MAX_PAGE_BYTES:
                return ""
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > _MAX_PAGE_BYTES:
                    return ""
            html = bytes(body).decode(r.encoding or "utf-8", errors="replace")
        return extract_content(html, url)
    return ""


def fetch(url: str, etag: str | None = None, last_modified: str | None = None) -> httpx.Response:
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    r = http().get(url, headers=headers)
    if r.status_code != 304:
        r.raise_for_status()
        if len(r.content) > _MAX_PAGE_BYTES:
            raise ValueError("feed response exceeds size limit")
    return r


# --- text extraction -------------------------------------------------------
def extract_content(html: str, url: str) -> str:
    """Best-effort article text via the extraction fallback chain ("" if nothing usable)."""
    min_words = config.EXTRACT_MIN_DESC_WORDS
    try:
        from trafilatura import extract

        text = extract(html, url=url, favor_precision=True, no_fallback=False)
        if text and len(text.split()) >= min_words:
            return text.strip()
    except Exception as e:  # noqa: BLE001
        log.debug("trafilatura failed for %s: %s", url, e)
    try:
        from newspaper import Article

        art = Article(url=url)
        art.set_html(html)
        art.parse()
        if art.text and len(art.text.split()) >= min_words:
            return art.text.strip()
    except Exception as e:  # noqa: BLE001
        log.debug("newspaper3k failed for %s: %s", url, e)
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "aside", "form"]):
            tag.decompose()
        paras = (p.get_text(" ", strip=True) for p in soup.find_all("p"))
        return "\n".join(p for p in paras if len(p.split()) >= 25)
    except Exception as e:  # noqa: BLE001
        log.debug("bs4 failed for %s: %s", url, e)
    return ""


def _strip_html(text: str) -> str:
    if "<" not in text or ">" not in text:
        return text
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", text)


def _entry_description(entry: Any) -> str:
    return _strip_html(entry.get("description") or entry.get("summary") or "").strip()


def _published_utc(entry: Any) -> str | None:
    """Entry date normalized to naive UTC (feeds mix +0000/+0200/... offsets)."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:  # feedparser already converted to UTC
        return dt.datetime(*parsed[:6]).strftime("%Y-%m-%d %H:%M:%S")
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        d = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.UTC).replace(tzinfo=None)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _retention_cutoff() -> str | None:
    if config.RETENTION_DAYS <= 0:
        return None
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=config.RETENTION_DAYS)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def _hero_image(entry: Any) -> str | None:
    for enc in entry.get("enclosures") or []:
        url = enc.get("url") or enc.get("href")
        if is_http_url(url) and (enc.get("type") or "image").startswith("image"):
            return url
    return None


# --- feeds -----------------------------------------------------------------
def ingest_feed(feed: dict[str, Any]) -> int:
    """Fetch one feed and store new entries. Returns the number of new articles."""
    try:
        r = fetch(feed["xml_url"], feed.get("etag"), feed.get("last_modified"))
    except Exception as e:  # noqa: BLE001
        log.warning("feed fetch error %s: %s", feed["xml_url"], e)
        with db.get_conn() as conn:
            conn.execute("UPDATE Feed SET last_error = ? WHERE id = ?", (str(e)[:300], feed["id"]))
        return 0

    new = 0
    too_old = _retention_cutoff()
    with db.get_conn() as conn:
        if r.status_code != 304:
            parsed = feedparser.parse(r.content, resolve_relative_uris=False, sanitize_html=False)
            entries = parsed.entries[: config.MAX_ITEMS_PER_FEED]
            for entry in entries:
                link = (entry.get("link") or "").strip() or None
                title = (entry.get("title") or "").strip()
                if not link and not title:
                    continue
                if link and conn.execute(
                    "SELECT 1 FROM Article WHERE url = ? UNION ALL SELECT 1 FROM ArticleTombstone WHERE url = ?",
                    (link, link),
                ).fetchone():
                    continue
                published = _published_utc(entry)
                if published and too_old and published < too_old:
                    continue  # would be deleted by retention right away
                ukey, tkey = dedup.url_key(link), dedup.title_key(title)
                original = dedup.find_original(conn, ukey, tkey)
                content = content_quality.clean_body(_entry_description(entry))
                post, comments = content_quality.split_post(content)
                if (feed["enabled"] and original is None and _worth_fetching(link)
                        and len(post.split()) < config.EXTRACT_MIN_DESC_WORDS):
                    try:
                        page = content_quality.clean_body(fetch_page_text(link))
                        if page:
                            content = f"{page}\n\n{comments}" if comments else page
                    except Exception as e:  # noqa: BLE001
                        log.debug("page fetch failed %s: %s", link, e)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO Article (feed_id, title, url, author, content_raw, "
                    "content_class, hero_image_url, published_date, created_at, url_key, title_key, "
                    "duplicate_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (feed["id"], title, link, entry.get("author"), content,
                     content_quality.classify(content), _hero_image(entry),
                     published, db.utcnow(), ukey, tkey, original),
                )
                new += cur.rowcount
        conn.execute(
            "UPDATE Feed SET last_fetched_at = ?, etag = ?, last_modified = ?, last_error = NULL "
            "WHERE id = ?",
            (db.utcnow(), r.headers.get("ETag") or feed.get("etag"),
             r.headers.get("Last-Modified") or feed.get("last_modified"), feed["id"]),
        )
    return new


def ingest_all(on_new: Callable[[], None] | None = None) -> int:
    """Fetch all feeds concurrently; ``on_new`` fires whenever a feed stored new articles."""
    with db.get_conn() as conn:
        sql = "SELECT * FROM Feed ORDER BY enabled DESC, id"
        if config.MAX_FEEDS_PER_RUN > 0:
            sql += f" LIMIT {int(config.MAX_FEEDS_PER_RUN)}"
        feeds = [dict(r) for r in conn.execute(sql)]

    total = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.FETCH_CONCURRENCY) as ex:
        futures = {ex.submit(ingest_feed, f): f for f in feeds}
        for fut in concurrent.futures.as_completed(futures):
            try:
                n = fut.result()
            except Exception as e:  # noqa: BLE001
                log.warning("ingest error for %s: %s", futures[fut]["title"], e)
                continue
            total += n
            if n and on_new and futures[fut]["enabled"]:
                on_new()
    return total
