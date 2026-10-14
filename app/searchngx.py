"""Thin client for the SearXNG instance ("searchngx") used by the per-story chat.

Used only by the chat agent (app/chat.py) to gather fresh context before
answering. Both calls are *best-effort*: any network/parse failure returns an
empty result rather than raising, so a search outage can never break a chat
answer — it just degrades to the story context alone.

SearXNG JSON API (``search.formats`` must include ``json``)::

    GET {SEARCHNGX_BASE_URL}/search?q=…&format=json&categories=general,news
    -> {"results": [{"title", "url", "content", "engine"}, ...],
        "unresponsive_engines": [[name, reason], ...], ...}

Upstream engines regularly answer the instance with CAPTCHAs / rate limits;
an empty result is retried once with SEARCHNGX_FALLBACK_ENGINES.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config, ingest

log = logging.getLogger("news.chat.search")


def _client() -> httpx.Client:
    return httpx.Client(timeout=config.SEARCHNGX_TIMEOUT, follow_redirects=True)


def _auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if config.SEARCHNGX_API_KEY:
        headers["Authorization"] = f"Bearer {config.SEARCHNGX_API_KEY}"
    return headers


def _endpoint_url() -> str:
    base = config.SEARCHNGX_BASE_URL.rstrip("/")
    return f"{base}/{config.SEARCHNGX_ENDPOINT.strip('/')}"


def _query(c: httpx.Client, query: str, selector: dict[str, str]) -> dict[str, Any]:
    r = c.get(_endpoint_url(), params={"q": query, "format": "json", **selector}, headers=_auth_headers())
    r.raise_for_status()
    return r.json()


def search(query: str, max_results: int | None = None, *, strict: bool = False) -> list[dict[str, str]]:
    """Run a web search; return ``[{title, url, description}]`` (may be empty).

    ``strict`` re-raises request failures instead of returning no results.
    """
    max_results = max_results or config.SEARCHNGX_MAX_RESULTS
    selectors = [{"categories": config.SEARCHNGX_CATEGORIES}]
    if config.SEARCHNGX_FALLBACK_ENGINES:
        selectors.append({"engines": config.SEARCHNGX_FALLBACK_ENGINES})
    try:
        with _client() as c:
            for selector in selectors:
                data = _query(c, query, selector)
                out, seen = [], set()
                for res in data.get("results") or []:
                    url = (res.get("url") or "").strip()
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    out.append({
                        "title": (res.get("title") or "").strip(),
                        "url": url,
                        "description": (res.get("content") or res.get("description") or "").strip(),
                    })
                    if len(out) >= max_results:
                        break
                if out:
                    return out
                log.warning("searchngx: no results for %r via %s (unresponsive: %s)", query, selector,
                            data.get("unresponsive_engines"))
        return []
    except Exception as e:  # noqa: BLE001 — best-effort, degrade to no results
        log.warning("searchngx search failed for %r: %s", query, e)
        if strict:
            raise
        return []


def scrape(url: str) -> str:
    """Extract article text from an untrusted search result; "" on any failure.

    ``ingest.fetch_page_text`` only follows hops to public addresses and caps the body.
    """
    try:
        return ingest.fetch_page_text(url).strip()
    except Exception as e:  # noqa: BLE001 — best-effort, degrade to empty text
        log.debug("searchngx scrape failed for %s: %s", url, e)
        return ""
