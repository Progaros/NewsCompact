"""Per-article AI pipeline: embed → cluster → harness → Prompt 2.

Articles are processed serially (the single GPU chat stream is the
bottleneck). Every successful run stores per-stage wall times plus
``processed_at`` so /list can show both the work time and the queue wait.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Callable

from . import clustering, config, db, dedup, harness, ollama, story

log = logging.getLogger("news.pipeline")

# Articles the pipeline will pick up. Broken bodies are never processed
# (title-only boilerplate); disabled feeds wait until re-enabled.
PENDING_WHERE = (
    "a.story_id IS NULL AND a.duplicate_of IS NULL "
    "AND COALESCE(a.content_class, 'empty') != 'broken' "
    "AND COALESCE(a.pipeline_failures, 0) < ? "
    "AND a.feed_id IN (SELECT id FROM Feed WHERE enabled = 1)"
)


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        f"SELECT count(*) FROM Article a WHERE {PENDING_WHERE}", (config.PIPELINE_MAX_FAILURES,)
    ).fetchone()[0]


# Display status of an article row (needs Article a JOIN Feed f):
# duplicate | assigned | dead | skipped (broken body or disabled feed) | pending.
STATUS_SQL = (
    "CASE WHEN a.duplicate_of IS NOT NULL THEN 'duplicate' "
    "WHEN a.story_id IS NOT NULL THEN 'assigned' "
    f"WHEN COALESCE(a.pipeline_failures, 0) >= {int(config.PIPELINE_MAX_FAILURES)} THEN 'dead' "
    "WHEN a.content_class = 'broken' OR f.enabled = 0 THEN 'skipped' "
    "ELSE 'pending' END"
)


# --- self-healing (run before every batch) ---------------------------------
def repair_placeholder_stories() -> int:
    """Re-pend articles of NULL-headline stories (process died before Prompt 2 ran)."""
    with db.get_conn() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM Story WHERE headline IS NULL")]
        for sid in ids:
            conn.execute("UPDATE Article SET story_id = NULL WHERE story_id = ?", (sid,))
            conn.execute("DELETE FROM Story WHERE id = ?", (sid,))
    if ids:
        log.info("repaired %d placeholder stories", len(ids))
    return len(ids)


def reap_orphaned_stories() -> int:
    """Delete stories that lost all their articles."""
    with db.get_conn() as conn:
        n = conn.execute(
            "DELETE FROM Story WHERE NOT EXISTS (SELECT 1 FROM Article a WHERE a.story_id = Story.id)"
        ).rowcount
    if n:
        log.info("reaped %d orphaned stories", n)
    return n


# --- one article -----------------------------------------------------------
def _is_junk_join(conn: sqlite3.Connection, art: dict[str, Any], story_id: int) -> bool:
    """A thin article joining a junk story adds nothing worth two 27B calls."""
    if art.get("content_class") != "thin":
        return False
    s = conn.execute("SELECT quality_score FROM Story WHERE id = ?", (story_id,)).fetchone()
    if s is not None and s["quality_score"] is not None and s["quality_score"] <= config.JUNK_SKIP_QUALITY_THRESHOLD:
        return True
    classes = {r[0] or "" for r in conn.execute(
        "SELECT content_class FROM Article WHERE story_id = ? AND id != ?", (story_id, art["id"])
    )}
    return classes <= {"", "broken", "thin"}


def process_article(art: dict[str, Any], emit: Callable[[dict], None] | None = None) -> str:
    """Run the full pipeline for one article.

    Returns "ok", "failed" (counted towards the dead letter) or "unavailable"
    (Ollama unreachable: rolled back without counting).
    """
    wall0, cpu0 = time.perf_counter(), time.process_time()
    t: dict[str, float] = {"embed": 0.0, "cluster": 0.0, "harness": 0.0, "story": 0.0}
    story_id: int | None = None
    created = junk = False
    p2_chars = 0

    def stage(name: str, t0: float) -> float:
        t[name] = time.perf_counter() - t0
        return time.perf_counter()

    try:
        with db.get_conn() as conn:
            t0 = time.perf_counter()
            vec = clustering.ensure_embedding(conn, art)
            t0 = stage("embed", t0)

            story_id, created, revived = clustering.decide_story(conn, vec, art)
            conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (story_id, art["id"]))
            t0 = stage("cluster", t0)

            junk = not created and _is_junk_join(conn, art, story_id)
            assessment: dict[str, Any] = {}
            if junk:
                log.info("junk-skip article=%s story=%s", art["id"], story_id)
            else:
                try:
                    assessment = harness.assess_article(art.get("title") or "", art.get("content_raw") or art.get("title") or "")
                except Exception:  # noqa: BLE001 - quality stays NULL, story still updates
                    log.exception("harness failed for article %s", art["id"])
            conn.execute("UPDATE Article SET quality = ? WHERE id = ?", (assessment.get("quality"), art["id"]))
            t0 = stage("harness", t0)

            if not junk:
                p2_chars = story.update_story(
                    conn, story_id, breaking=bool(assessment.get("breaking")),
                    article_id=art["id"], force_full=revived,
                )
            stage("story", t0)

            wall = time.perf_counter() - wall0
            conn.execute(
                "UPDATE Article SET pipeline_failures = 0, processed_at = datetime('now'), "
                "t_embed_s = ?, t_cluster_s = ?, t_harness_s = ?, t_story_s = ?, t_wall_s = ? "
                "WHERE id = ?",
                (t["embed"], t["cluster"], t["harness"], t["story"], wall, art["id"]),
            )
            s = conn.execute("SELECT metrics FROM Story WHERE id = ?", (story_id,)).fetchone()
    except Exception as e:  # noqa: BLE001 - one bad article must not stall the queue
        unavailable = isinstance(e, ollama.UNAVAILABLE_ERRORS)
        if unavailable:
            log.warning("ollama unreachable while processing article %s: %s", art.get("id"), e)
        else:
            log.exception("pipeline error for article %s", art.get("id"))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE Article SET story_id = NULL, quality = NULL, "
                "pipeline_failures = COALESCE(pipeline_failures, 0) + ? WHERE id = ?",
                (0 if unavailable else 1, art["id"]),
            )
            if created and story_id is not None:
                conn.execute("DELETE FROM Story WHERE id = ?", (story_id,))
            failures = conn.execute(
                "SELECT pipeline_failures FROM Article WHERE id = ?", (art["id"],)
            ).fetchone()[0]
        if failures >= config.PIPELINE_MAX_FAILURES:
            log.error("dead-letter article %s after %d failures", art["id"], failures)
        return "unavailable" if unavailable else "failed"

    log.info(
        "perf article=%s story=%s created=%s wall=%.1fs cpu=%.1fs embed=%.2fs knn=%.2fs "
        "harness=%.2fs p2=%.2fs p2chars=%d junk_skip=%d",
        art["id"], story_id, created, time.perf_counter() - wall0, time.process_time() - cpu0,
        t["embed"], t["cluster"], t["harness"], t["story"], p2_chars, int(junk),
    )
    metrics = db.json_dict(s["metrics"]) if s is not None else {}
    if emit and metrics.get("status") == "Breaking" and (metrics.get("urgency_level") or 0) >= 8:
        emit({"type": "story_update", "story_id": story_id, "breaking": True})
    return "ok"


# --- batch -----------------------------------------------------------------
def process_pending(*, emit: Callable[[dict], None] | None = None,
                    should_stop: Callable[[], bool] = lambda: False) -> dict[str, int]:
    """Drain the pending queue, newest first so a backlog never delays fresh news.

    Each article is tried at most once per call.
    """
    repair_placeholder_stories()
    reap_orphaned_stories()
    tried: set[int] = set()
    result = {"processed": 0, "failed": 0}
    with db.get_conn() as conn:
        dedup.attach_duplicates(conn)
        if not pending_count(conn):
            return result
    if not ollama.available():
        log.warning("ollama at %s unreachable; pipeline paused", config.OLLAMA_BASE_URL)
        return result
    try:
        _drain(result, tried, emit, should_stop)
    finally:
        with db.get_conn() as conn:
            dedup.attach_duplicates(conn)
    return result


def _drain(result: dict[str, int], tried: set[int], emit: Callable[[dict], None] | None,
           should_stop: Callable[[], bool]) -> None:
    while not should_stop():
        with db.get_conn() as conn:
            rows = conn.execute(
                f"SELECT a.* FROM Article a WHERE {PENDING_WHERE} ORDER BY a.id DESC LIMIT ?",
                (config.PIPELINE_MAX_FAILURES, config.PIPELINE_BATCH + len(tried)),
            ).fetchall()
        batch = [dict(r) for r in rows if r["id"] not in tried][: config.PIPELINE_BATCH]
        if not batch:
            return
        for art in batch:
            if should_stop():
                return
            tried.add(art["id"])
            outcome = process_article(art, emit)
            if outcome == "unavailable":
                return
            result["processed" if outcome == "ok" else "failed"] += 1
