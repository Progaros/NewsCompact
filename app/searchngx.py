"""Thin client for the local ``searchngx`` web search (searchapi.io-style).

Used only by the per-story chat agent (app/chat.py) to gather fresh context
before answering. Both calls are *best-effort*: any network/parse failure
returns an empty result rather than raising, so a search outage can never
break a chat answer — it just degrades to the story context alone.

The instance is assumed to expose a searchapi.io-compatible endpoint
(config.SEARCHNGX_BASE_URL + config.SEARCHNGX_ENDPOINT) returning::

    {"results": [{"title", "url", "description"}, ...], ...}
"""
from __future__ import annotations

import logging

import httpx

from . import config, ingest

log = logging.getLogger("news.chat.search")


def _client() -> httpx.Client:
    return httpx.Client(timeout=config.SEARCHNGX_TIMEOUT, follow_redirects=True)


def _auth_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.SEARCHNGX_API_KEY:
        headers["Authorization"] = f"Bearer {config.SEARCHNGX_API_KEY}"
    return headers


def _endpoint_url() -> str:
    base = config.SEARCHNGX_BASE_URL.rstrip("/")
    return f"{base}/{config.SEARCHNGX_ENDPOINT.strip('/')}"


def search(query: str, max_results: int | None = None) -> list[dict[str, str]]:
    """Run a web search; return ``[{title, url, description}]`` (may be empty)."""
    max_results = max_results or config.SEARCHNGX_MAX_RESULTS
    try:
        with _client() as c:
            r = c.post(
                _endpoint_url(),
                json={"engine": "google", "query": query, "max_results": max_results},
                headers=_auth_headers(),
            )
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or []
        out = []
        for res in results[:max_results]:
            url = (res.get("url") or "").strip()
            if not url:
                continue
            out.append(
                {
                    "title": (res.get("title") or "").strip(),
                    "url": url,
                    "description": (res.get("description") or "").strip(),
                }
            )
        return out
    except Exception as e:  # noqa: BLE001 — best-effort, degrade to no results
        log.debug("searchngx search failed for %r: %s", query, e)
        return []


def scrape(url: str) -> str:
    """Fetch a URL and extract article text (ingest's trafilatura/newspaper
    chain). Returns "" on any failure. Reuses ingest.extract_content so the
    chat agent reads the same text the pipeline ingests."""
    try:
        with _client() as c:
            r = c.get(url, headers={"User-Agent": config.USER_AGENT})
            r.raise_for_status()
            return ingest.extract_content(r.text, url).strip()
    except Exception as e:  # noqa: BLE001 — best-effort, degrade to empty text
        log.debug("searchngx scrape failed for %s: %s", url, e)
        return ""
