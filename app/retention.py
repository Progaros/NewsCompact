"""Retention: keep the database bounded.

Stories without an update for RETENTION_DAYS are deleted together with their
articles, vectors and chat; articles that never joined a story (pending,
skipped, disabled feeds, dead letter) go after the same age. Deleted URLs are
remembered as tombstones so feeds that still list them don't re-ingest them.
Votes survive (FeedbackLog snapshots headline + tags).
"""
from __future__ import annotations

import datetime as dt
import logging

from . import clustering, config, db

log = logging.getLogger("news.retention")

_TOMBSTONE_DAYS = 30


def _ago(days: float) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def run() -> dict[str, int]:
    result = {"stories": 0, "articles": 0}
    if config.RETENTION_DAYS <= 0:
        return result
    cutoff = _ago(config.RETENTION_DAYS)
    with db.get_conn() as conn:
        stories = [r[0] for r in conn.execute(
            "SELECT id FROM Story WHERE COALESCE(updated_at, created_at) < ?", (cutoff,))]
        articles = conn.execute(
            "SELECT id, url FROM Article WHERE story_id IN (SELECT id FROM Story WHERE "
            "COALESCE(updated_at, created_at) < ?) OR (story_id IS NULL AND created_at < ?)",
            (cutoff, cutoff),
        ).fetchall()
        conn.execute("BEGIN")
        try:
            conn.executemany("INSERT OR IGNORE INTO ArticleTombstone (url) VALUES (?)",
                             [(r["url"],) for r in articles if r["url"]])
            clustering.delete_embeddings(conn, [r["id"] for r in articles])
            conn.executemany("DELETE FROM Article WHERE id = ?", [(r["id"],) for r in articles])
            conn.executemany("DELETE FROM Story WHERE id = ?", [(sid,) for sid in stories])
            conn.execute("DELETE FROM ArticleTombstone WHERE deleted_at < ?", (_ago(_TOMBSTONE_DAYS),))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    result.update(stories=len(stories), articles=len(articles))
    if articles or stories:
        db.reclaim_space()
        log.info("retention: deleted %d stories and %d articles older than %g days",
                 len(stories), len(articles), config.RETENTION_DAYS)
    return result
