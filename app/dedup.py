"""Cross-feed duplicate detection.

The same story often arrives via several feeds (syndicated wire copy, Reddit
cross-posts, share links with tracking parameters). A new article whose
canonical URL, or normalized title of at least DEDUP_TITLE_MIN_WORDS words,
matches a recent article from an enabled feed is stored with ``duplicate_of``.
It skips the AI pipeline and joins the original's story once that has one, so
it still counts as a corroborating source.
"""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import config

# Query parameters that only track the click, never select content.
_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|dclid|mc_[a-z]+|ref|ref_src|ref_url|si|is|igshid|cmpid|wt_.*|at_.*)$", re.I)
_NON_WORD = re.compile(r"[^\w]+")


def url_key(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if not host:
        return None
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)))
    return f"{host}{p.path.rstrip('/')}" + (f"?{query}" if query else "")


def title_key(title: str | None) -> str | None:
    words = _NON_WORD.sub(" ", (title or "").lower()).split()
    return " ".join(words) if len(words) >= config.DEDUP_TITLE_MIN_WORDS else None


def find_original(conn: sqlite3.Connection, ukey: str | None, tkey: str | None) -> int | None:
    """Id of the earliest recent processable article with the same URL/title key."""
    if not ukey and not tkey:
        return None
    row = conn.execute(
        "SELECT COALESCE(a.duplicate_of, a.id) FROM Article a JOIN Feed f ON f.id = a.feed_id "
        "WHERE (a.url_key = ? OR a.title_key = ?) AND f.enabled = 1 "
        "AND COALESCE(a.content_class, '') != 'broken' "
        "AND a.created_at >= datetime('now', ?) ORDER BY a.id LIMIT 1",
        (ukey, tkey, f"-{int(config.DEDUP_WINDOW_HOURS)} hours"),
    ).fetchone()
    return row[0] if row else None


def attach_duplicates(conn: sqlite3.Connection) -> int:
    """Put waiting duplicates into their original's story; release orphaned ones."""
    attached = conn.execute(
        "UPDATE Article SET "
        "story_id = (SELECT o.story_id FROM Article o WHERE o.id = Article.duplicate_of), "
        "quality = (SELECT o.quality FROM Article o WHERE o.id = Article.duplicate_of), "
        "processed_at = datetime('now'), t_embed_s = 0, t_cluster_s = 0, t_harness_s = 0, "
        "t_story_s = 0, t_wall_s = 0 "
        "WHERE story_id IS NULL AND duplicate_of IS NOT NULL "
        "AND (SELECT o.story_id FROM Article o WHERE o.id = Article.duplicate_of) IS NOT NULL"
    ).rowcount
    # Original gone or dead-lettered: process the copy on its own.
    conn.execute(
        "UPDATE Article SET duplicate_of = NULL "
        "WHERE story_id IS NULL AND duplicate_of IS NOT NULL AND NOT EXISTS ("
        "  SELECT 1 FROM Article o WHERE o.id = Article.duplicate_of "
        "  AND COALESCE(o.pipeline_failures, 0) < ?)",
        (config.PIPELINE_MAX_FAILURES,),
    )
    return attached
