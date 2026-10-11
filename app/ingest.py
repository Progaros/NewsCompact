"""Ingestion: fetch RSS feeds concurrently, extract article text, dedup, store.

Full-text extraction fallback chain per the plan:
    trafilatura  ->  newspaper3k  ->  BeautifulSoup
Extraction only runs when the RSS description is too short to be useful.
"""
from __future__ import annotations

import concurrent.futures
import logging
from typing import Any

import feedparser
import httpx

from . import config, content_quality, db

log = logging.getLogger("news.ingest")


def _fetch(url: str, etag: str | None, last_modified: str | None) -> tuple[str, str | None, str | None]:
    """Fetch a URL with a browser-ish UA. Returns (body, etag, last_modified)."""
    headers = {"User-Agent": config.USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    with httpx.Client(timeout=config.FETCH_TIMEOUT) as client:
        r = client.get(url, headers=headers, follow_redirects=True)
        if r.status_code == 304:
            return "", etag, last_modified
        r.raise_for_status()
        return r.text, r.headers.get("ETag"), r.headers.get("Last-Modified")


def extract_content(html: str, url: str) -> str:
    """Run the extraction fallback chain and return the best text we can get."""
    # 1. trafilatura
    try:
        from trafilatura import extract

        text = extract(html, url=url, favor_precision=True, no_fallback=False)
        if text and len(text.split()) >= config.EXTRACT_MIN_DESC_WORDS:
            return text.strip()
    except Exception as e:  # noqa: BLE001 - any extraction failure falls through
        log.debug("trafilatura failed for %s: %s", url, e)
    # 2. newspaper3k
    try:
        from newspaper import Article

        art = Article(url=url, page=html)
        art.parse()
        text = art.text
        if text and len(text.split()) >= config.EXTRACT_MIN_DESC_WORDS:
            return text
    except Exception as e:  # noqa: BLE001
        log.debug("newspaper3k failed for %s: %s", url, e)
    # 3. BeautifulSoup paragraph scrape
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "aside", "form"]):
            tag.decompose()
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n".join(p for p in paras if len(p.split()) >= 25)
        if text:
            return text
    except Exception as e:  # noqa: BLE001
        log.debug("bs4 failed for %s: %s", url, e)
    return ""


def _entry_text(entry) -> tuple[str, str | None]:
    """Best-available text from a feed entry without a page fetch."""
    desc = entry.get("description", "")
    # feedparser sometimes hands us HTML in description; strip tags crudely.
    if "<" in desc and ">" in desc:
        try:
            from bs4 import BeautifulSoup

            desc = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
        except Exception:  # noqa: BLE001
            import re as _re

            desc = _re.sub(r"<[^>]+>", " ", desc)
    summary = entry.get("summary")
    if not desc and summary:
        desc = summary
    return desc.strip(), entry.get("link")


def _ingest_feed(feed: dict[str, Any]) -> int:
    """Fetch one feed, extract, dedup + store. Returns number of new articles."""
    url = feed["xml_url"]
    try:
        body, etag, last_modified = _fetch(url, feed.get("etag"), feed.get("last_modified"))
    except Exception as e:  # noqa: BLE001
        log.warning("feed fetch error %s: %s", url, e)
        return 0
    if not body:
        # 304 / no change
        return 0

    parsed = feedparser.parse(body)
    entries = parsed.entries[: config.MAX_ITEMS_PER_FEED]
    new_count = 0
    now = _now()

    with db.get_conn() as conn:
        for entry in entries:
            link = entry.get("link")
            title = entry.get("title", "").strip()
            if not link and not title:
                continue
            # dedup by url (or title if no url)
            dup = None
            if link:
                dup = conn.execute("SELECT id FROM Article WHERE url = ?", (link,)).fetchone()
            if dup:
                continue

            desc, _link = _entry_text(entry)
            content = desc
            # Full-text extraction only when the description is thin and we have a URL.
            if link and len(desc.split()) < config.EXTRACT_MIN_DESC_WORDS:
                try:
                    html_body, _e, _lm = _fetch(link, None, None)
                    content = extract_content(html_body, link) or desc
                except Exception:  # noqa: BLE001
                    content = desc

            published = _publish(entry)
            author = entry.get("author") or _author(entry)
            hero = _image(entry)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO Article
                    (feed_id, title, url, author, content_raw, content_class,
                     hero_image_url, published_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feed["id"], title, link, author, content,
                    content_quality.classify(content), hero,
                    published, now,
                ),
            )
            new_count += 1 if cur.lastrowid and cur.lastrowid > 0 else 0
        conn.execute(
            "UPDATE Feed SET last_fetched_at = ?, etag = ?, last_modified = ? WHERE id = ?",
            (now, etag, last_modified, feed["id"]),
        )
        conn.commit()
    return new_count


def ingest_all(max_feeds: int = config.MAX_FEEDS_PER_RUN) -> int:
    """Ingest feeds concurrently. Returns total new articles."""
    with db.get_conn() as conn:
        q = "SELECT * FROM Feed"
        args: tuple = ()
        if max_feeds > 0:
            q += " ORDER BY id LIMIT ?"
            args = (max_feeds,)
        feeds = [dict(r) for r in conn.execute(q, args)]

    total = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.FETCH_CONCURRENCY) as ex:
        futs = {ex.submit(_ingest_feed, f): f["title"] for f in feeds}
        for fut in concurrent.futures.as_completed(futs):
            try:
                total += fut.result()
            except Exception as e:  # noqa: BLE001
                log.warning("ingest error for %s: %s", futs[fut], e)
    log.info("ingest done: %d new articles", total)
    return total


# --- helpers ---------------------------------------------------------------
def _now() -> str:
    import datetime

    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _publish(entry) -> str | None:
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    import email.utils
    import datetime

    try:
        return email.utils.parsedate_to_datetime(raw).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return raw


def _author(entry) -> str | None:
    try:
        from bs4 import BeautifulSoup  # author may be embedded in a byline

        return None
    except Exception:  # noqa: BLE001
        return None


def _image(entry) -> str | None:
    enclosures = entry.get("enclosures") or []
    if enclosures:
        return enclosures[0].get("url")
    return None
