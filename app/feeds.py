"""Feed management: list with volume stats, add, preview, enable/disable, edit, delete.

Conflicts (duplicate URL) raise ValueError; unknown feeds return False.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import clustering, db, ingest, pipeline, worker


def list_with_stats() -> dict[str, Any]:
    with db.get_conn() as conn:
        feeds = [dict(r) for r in conn.execute("SELECT * FROM Feed ORDER BY category, title")]
        stats = {r["feed_id"]: r for r in conn.execute(
            "SELECT feed_id, "
            "COUNT(CASE WHEN created_at >= datetime('now', '-1 day') THEN 1 END) AS last_24h, "
            "COUNT(CASE WHEN created_at >= datetime('now', '-7 day') THEN 1 END) AS last_7d, "
            "COUNT(*) AS total FROM Article GROUP BY feed_id"
        )}
    cats: dict[str, dict[str, int]] = {}
    for f in feeds:
        f.pop("etag", None)
        f.pop("last_modified", None)
        s = stats.get(f["id"])
        f["last_24h"] = s["last_24h"] if s else 0
        f["last_7d"] = s["last_7d"] if s else 0
        f["article_total"] = s["total"] if s else 0
        g = cats.setdefault(f["category"] or "(uncategorized)",
                            {"feeds": 0, "enabled": 0, "errors": 0, "last_24h": 0, "last_7d": 0})
        g["feeds"] += 1
        g["errors"] += 1 if f["last_error"] else 0
        if f["enabled"]:  # category totals = active load only
            g["enabled"] += 1
            g["last_24h"] += f["last_24h"]
            g["last_7d"] += f["last_7d"]
    return {"feeds": feeds, "categories": dict(sorted(cats.items()))}


def add(title: str, xml_url: str, site_url: str | None, category: str | None) -> int:
    with db.get_conn() as conn:
        try:
            return conn.execute(
                "INSERT INTO Feed (title, xml_url, site_url, category, enabled) VALUES (?, ?, ?, ?, 1)",
                (title, xml_url, site_url, category),
            ).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"feed already exists: {xml_url}") from None


def preview_title(url: str) -> str:
    """The feed's title (else its first entry's); ValueError if unreachable or unparseable."""
    import feedparser

    try:
        r = ingest.fetch(url)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"fetch failed: {e}") from e
    parsed = feedparser.parse(r.content, resolve_relative_uris=False, sanitize_html=False)
    title = (parsed.feed.get("title") or "").strip()
    if not title and parsed.entries:
        title = (parsed.entries[0].get("title") or "").strip()
    if not title:
        raise ValueError("could not parse a feed title")
    return title


def set_category_enabled(category: str | None, enabled: bool) -> int:
    with db.get_conn() as conn:
        reset = ", fetch_failures = 0, next_fetch_at = NULL" if enabled else ""
        if category:
            n = conn.execute(f"UPDATE Feed SET enabled = ?{reset} WHERE category = ?",
                             (int(enabled), category)).rowcount
        else:
            n = conn.execute(f"UPDATE Feed SET enabled = ?{reset} WHERE category IS NULL OR category = ''",
                             (int(enabled),)).rowcount
    if enabled:
        worker.wake_pipeline()
    return n


_EDITABLE = frozenset({"enabled", "title", "category", "xml_url"})


def update(feed_id: int, changes: dict[str, Any]) -> bool:
    """Apply ``changes`` (keys from ``_EDITABLE``); False if the feed doesn't exist."""
    if not set(changes) <= _EDITABLE:
        raise KeyError(sorted(set(changes) - _EDITABLE))
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM Feed WHERE id = ?", (feed_id,)).fetchone() is None:
            return False
        if changes:
            assignments = ", ".join(f"{col} = ?" for col in changes)  # whitelisted above
            if "xml_url" in changes or changes.get("enabled"):
                assignments += ", fetch_failures = 0, next_fetch_at = NULL"  # fetch again right away
            try:
                conn.execute(f"UPDATE Feed SET {assignments} WHERE id = ?", (*changes.values(), feed_id))
            except sqlite3.IntegrityError:
                raise ValueError(f"xml_url already in use: {changes.get('xml_url')}") from None
    if changes.get("enabled"):
        worker.wake_pipeline()
    return True


def delete(feed_id: int) -> bool:
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM Feed WHERE id = ?", (feed_id,)).fetchone() is None:
            return False
        ids = [r[0] for r in conn.execute("SELECT id FROM Article WHERE feed_id = ?", (feed_id,))]
        clustering.delete_embeddings(conn, ids)
        conn.execute("DELETE FROM Feed WHERE id = ?", (feed_id,))  # cascades to Article
    pipeline.reap_orphaned_stories()
    return True
