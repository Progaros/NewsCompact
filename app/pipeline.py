"""Per-article AI pipeline: embed → cluster → harness → Prompt 2.

Articles are processed serially (the single GPU chat stream is the
bottleneck). Every successful run stores per-stage wall times plus
``processed_at`` so /list can show both the work time and the queue wait.
"""
from __future__ import annotations

import collections
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from typing import Any

import httpx

from . import clustering, config, content_quality, db, dedup, harness, ingest, ollama, story, tags

log = logging.getLogger("news.pipeline")

# Articles the pipeline will pick up. Broken bodies are never processed
# (title-only boilerplate); disabled feeds wait until re-enabled.
PENDING_WHERE = (
    "a.story_id IS NULL AND a.duplicate_of IS NULL "
    "AND COALESCE(a.content_class, 'empty') != 'broken' "
    "AND COALESCE(a.pipeline_failures, 0) < ? "
    "AND (a.retry_after IS NULL OR a.retry_after <= datetime('now')) "
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


def article_list(*, limit: int, order: str = "newest", status: str = "", query: str = "") -> dict[str, Any]:
    """Article rows with pipeline status and stage timings (the /list page); ``count`` ignores ``limit``."""
    direction = "ASC" if order == "oldest" else "DESC"
    where, params = [], []
    if status:
        where.append(f"({STATUS_SQL}) = ?")
        params.append(status)
    if query:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(a.title LIKE ? ESCAPE '\\' OR f.title LIKE ? ESCAPE '\\')")
        params += [like, like]
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    base = f"FROM Article a JOIN Feed f ON f.id = a.feed_id {where_sql}"
    with db.get_conn() as conn:
        total = conn.execute(f"SELECT count(*) {base}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT a.id, a.title, a.url, a.author, a.published_date, a.created_at,
                   a.processed_at, a.content_class, a.quality, a.story_id, f.title AS feed,
                   ({STATUS_SQL}) AS status,
                   a.t_embed_s, a.t_cluster_s, a.t_harness_s, a.t_story_s, a.t_wall_s, a.t_merge_s,
                   (julianday(a.processed_at) - julianday(a.created_at)) * 86400 AS latency_s
            {base}
            ORDER BY a.created_at {direction}, a.id {direction}
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    articles = []
    for r in rows:
        latency, wall = r["latency_s"], r["t_wall_s"]
        articles.append({
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "feed": r["feed"],
            "author": r["author"],
            "published_date": r["published_date"],
            "created_at": r["created_at"],
            "processed_at": r["processed_at"],
            "class": r["content_class"],
            "quality": r["quality"],
            "story_id": r["story_id"],
            "status": r["status"],
            "timings": {
                "wait": max(0.0, latency - wall) if latency is not None and wall is not None else None,
                "embed": r["t_embed_s"],
                "cluster": r["t_cluster_s"],
                "harness": r["t_harness_s"],
                "story": r["t_story_s"],
                "wall": wall,
                "latency": latency,
                "merge": r["t_merge_s"],
            },
        })
    return {"count": total, "articles": articles}


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Totals for /api/health and the story list header."""
    row = conn.execute(
        "SELECT (SELECT count(*) FROM Article), (SELECT count(*) FROM Story), "
        "(SELECT count(*) FROM Article WHERE story_id IS NULL AND COALESCE(pipeline_failures, 0) >= ?), "
        "(SELECT MAX(created_at) FROM Article), "
        "(SELECT MAX(news_at) FROM Story WHERE headline IS NOT NULL)",
        (config.PIPELINE_MAX_FAILURES,),
    ).fetchone()
    return {"articles": row[0], "stories": row[1], "pending": pending_count(conn), "dead_letter": row[2],
            "last_article": row[3], "latest_news": row[4]}


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


def repair_boilerplate_bodies() -> int:
    """Re-extract bodies one feed served identically under several titles (site chrome, not the
    article) and re-pend those articles; stories that keep other members are rewritten.
    """
    with db.get_conn() as conn:
        arts = [dict(r) for r in conn.execute(
            "SELECT a.id, a.url, a.story_id, a.content_raw, f.enabled FROM Article a JOIN Feed f ON f.id = a.feed_id "
            "JOIN (SELECT feed_id, content_raw FROM Article WHERE content_raw != '' GROUP BY feed_id, content_raw "
            "      HAVING count(DISTINCT title) >= ?) g ON g.feed_id = a.feed_id AND g.content_raw = a.content_raw",
            (config.BOILERPLATE_MIN_TITLES,),
        )]
    if not arts or not ollama.available():  # surviving stories need a Prompt 2 rewrite
        return 0
    bodies: dict[int, str] = {}
    for a in arts:
        body = ""
        if a["enabled"] and ingest._worth_fetching(a["url"]):
            try:
                body = content_quality.clean_body(ingest.fetch_page_text(a["url"]))
            except Exception as e:  # noqa: BLE001 - falls back to a title-only article
                log.debug("re-extraction failed for %s: %s", a["url"], e)
        bodies[a["id"]] = body
    seen = collections.Counter(bodies.values())
    touched: set[int] = set()
    with db.get_conn() as conn:
        for a in arts:
            body = bodies[a["id"]]
            if body == a["content_raw"] or seen[body] > 1:
                body = ""
            conn.execute(
                "UPDATE Article SET content_raw = ?, content_class = ?, story_id = NULL, quality = NULL, "
                "pipeline_failures = 0, processed_at = NULL WHERE id = ?",
                (body, content_quality.classify(body), a["id"]),
            )
            conn.execute("UPDATE Article SET story_id = NULL WHERE duplicate_of = ?", (a["id"],))
            clustering.delete_embeddings(conn, [a["id"]])
            if a["story_id"] is not None:
                touched.add(a["story_id"])
        for sid in touched:
            try:
                story.update_story(conn, sid, force_full=True)
            except Exception:  # noqa: BLE001 - the next joining article rewrites it
                log.exception("rewrite after boilerplate repair failed for story %s", sid)
    log.info("repaired %d boilerplate bodies (%d stories touched)", len(arts), len(touched))
    return len(arts)


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


_WORDS = re.compile(r"\w+")


def _words(a: dict[str, Any] | sqlite3.Row) -> set[str]:
    return set(_WORDS.findall(f"{a['title'] or ''} {a['content_raw'] or ''}".casefold()))


def _copy_of(conn: sqlite3.Connection, art: dict[str, Any], vec: list[float], story_id: int) -> sqlite3.Row | None:
    """A member this article reprints: a very similar full text that already has every word of it.

    Such a join can't add a fact (one new word like "verurteilt" already disqualifies it), so
    Harness and Prompt 2 would only spend ~8 s confirming that.
    """
    if art.get("content_class") != "ok":
        return None
    v = clustering.normalize(vec)
    members = []
    for m in conn.execute("SELECT id, title, content_raw, quality FROM Article WHERE story_id = ? AND id != ? "
                          "AND duplicate_of IS NULL AND content_class = 'ok' AND quality IS NOT NULL",
                          (story_id, art["id"])):
        w = clustering.get_vector(conn, m["id"])
        if w is not None and (sim := clustering.dot(v, clustering.normalize(w))) >= config.COPY_SKIP_SIM:
            members.append((sim, m))
    words = _words(art)
    for _, m in sorted(members, key=lambda x: -x[0]):
        if words <= _words(m):
            return m
    return None


def process_article(art: dict[str, Any], emit: Callable[[dict], None] | None = None, *,
                    anchors: clustering.AnchorCache | None = None,
                    embedded: tuple[list[float], float] | None = None) -> str:
    """Run the full pipeline for one article.

    ``anchors`` may be shared across a batch; ``embedded`` = ``(vector, seconds)`` from a batch
    embedding call. Returns "ok", "failed" (counted towards the dead letter), "timeout" (Ollama
    too slow: retried after a doubling delay) or "unavailable" (Ollama unreachable: rolled back
    without counting).
    """
    wall0, cpu0 = time.perf_counter(), time.process_time()
    t: dict[str, float] = {"embed": 0.0, "cluster": 0.0, "harness": 0.0, "story": 0.0}
    story_id: int | None = None
    created = junk = False
    copy: sqlite3.Row | None = None
    p2_chars = 0
    ollama.take_stats()

    def stage(name: str, t0: float) -> float:
        t[name] = time.perf_counter() - t0
        return time.perf_counter()

    try:
        with db.get_conn() as conn:
            t0 = time.perf_counter()
            vec = clustering.ensure_embedding(conn, art, embedded[0] if embedded else None)
            t0 = stage("embed", t0)
            if embedded:
                t["embed"] += embedded[1]

            verdicts: dict[int, str] = {}
            story_id, created, revived = clustering.decide_story(conn, vec, art, anchors, verdicts)
            conn.execute("UPDATE Article SET story_id = ? WHERE id = ?", (story_id, art["id"]))
            t0 = stage("cluster", t0)

            junk = not created and _is_junk_join(conn, art, story_id)
            if not (created or revived or junk):
                copy = _copy_of(conn, art, vec, story_id)
            assessment: dict[str, Any] = {}
            if junk:
                log.info("junk-skip article=%s story=%s", art["id"], story_id)
            elif copy is not None:
                log.info("copy-skip article=%s story=%s copy_of=%s", art["id"], story_id, copy["id"])
                assessment = {"quality": copy["quality"]}
            else:
                try:
                    assessment = harness.assess_article(art.get("title") or "", art.get("content_raw") or art.get("title") or "")
                except (httpx.TimeoutException, *ollama.UNAVAILABLE_ERRORS):
                    raise
                except Exception:  # noqa: BLE001 - quality stays NULL, story still updates
                    log.exception("harness failed for article %s", art["id"])
            conn.execute("UPDATE Article SET quality = ? WHERE id = ?", (assessment.get("quality"), art["id"]))
            t0 = stage("harness", t0)

            if copy is not None:
                story.refresh_quality(conn, story_id)
            elif not junk:
                p2_chars = story.update_story(
                    conn, story_id, breaking=bool(assessment.get("breaking")),
                    article_id=art["id"], force_full=revived,
                )
            stage("story", t0)

            for other, cls in verdicts.items():
                if cls == "SAME_TOPIC_DISTINCT_EVENT":
                    clustering.link_stories(conn, story_id, other, cls, "article")

            wall = time.perf_counter() - wall0
            conn.execute(
                "UPDATE Article SET pipeline_failures = 0, pipeline_timeouts = 0, retry_after = NULL, "
                "processed_at = datetime('now'), "
                "t_embed_s = ?, t_cluster_s = ?, t_harness_s = ?, t_story_s = ?, t_wall_s = ? "
                "WHERE id = ?",
                (t["embed"], t["cluster"], t["harness"], t["story"], wall, art["id"]),
            )
            s = conn.execute("SELECT metrics FROM Story WHERE id = ?", (story_id,)).fetchone()
    except Exception as e:  # noqa: BLE001 - one bad article must not stall the queue
        unavailable = isinstance(e, ollama.UNAVAILABLE_ERRORS)
        timed_out = not unavailable and isinstance(e, httpx.TimeoutException)
        if unavailable:
            log.warning("ollama unreachable while processing article %s: %s", art.get("id"), e)
        elif timed_out:
            log.warning("ollama timed out on article %s; retrying later", art.get("id"))
        else:
            log.exception("pipeline error for article %s", art.get("id"))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE Article SET story_id = NULL, quality = NULL, "
                "pipeline_failures = COALESCE(pipeline_failures, 0) + ? WHERE id = ?",
                (0 if unavailable or timed_out else 1, art["id"]),
            )
            if timed_out:
                n = conn.execute("UPDATE Article SET pipeline_timeouts = COALESCE(pipeline_timeouts, 0) + 1 "
                                 "WHERE id = ? RETURNING pipeline_timeouts", (art["id"],)).fetchone()[0]
                delay = min(config.PIPELINE_TIMEOUT_BACKOFF_MINUTES * 2 ** (n - 1), 24 * 60)
                conn.execute("UPDATE Article SET retry_after = datetime('now', ?), "
                             "pipeline_failures = CASE WHEN ? THEN ? ELSE pipeline_failures END WHERE id = ?",
                             (f"+{int(delay)} minutes", n >= config.PIPELINE_MAX_TIMEOUTS,
                              config.PIPELINE_MAX_FAILURES, art["id"]))
            if created and story_id is not None:
                conn.execute("DELETE FROM Story WHERE id = ?", (story_id,))
            failures = conn.execute(
                "SELECT pipeline_failures FROM Article WHERE id = ?", (art["id"],)
            ).fetchone()[0]
        if failures >= config.PIPELINE_MAX_FAILURES:
            log.error("dead-letter article %s after %d failures", art["id"], failures)
        return "unavailable" if unavailable else "timeout" if timed_out else "failed"

    llm = ollama.take_stats()
    log.info(
        "perf article=%s story=%s created=%s wall=%.1fs cpu=%.1fs embed=%.2fs knn=%.2fs "
        "harness=%.2fs p2=%.2fs p2chars=%d junk_skip=%d copy_skip=%d "
        "llm_calls=%d load=%.1fs in=%dtok/%.1fs out=%dtok/%.1fs",
        art["id"], story_id, created, time.perf_counter() - wall0, time.process_time() - cpu0,
        t["embed"], t["cluster"], t["harness"], t["story"], p2_chars, int(junk), int(copy is not None),
        llm["calls"], llm["load_s"], llm["in_tok"], llm["in_s"], llm["out_tok"], llm["out_s"],
    )
    metrics = db.json_dict(s["metrics"]) if s is not None else {}
    if emit and metrics.get("status") == "Breaking" and (metrics.get("urgency_level") or 0) >= 8:
        emit({"type": "story_update", "story_id": story_id, "breaking": True})
    return "ok"


# --- batch -----------------------------------------------------------------
def process_pending(*, emit: Callable[[dict], None] | None = None,
                    should_stop: Callable[[], bool] = lambda: False) -> dict[str, int]:
    """Drain the pending queue, newest first so a backlog never delays fresh news
    (low-trust categories wait for the rest; see ``_drain``).

    Each article is tried at most once per call.
    """
    repair_placeholder_stories()
    repair_boilerplate_bodies()
    reap_orphaned_stories()
    tried: set[int] = set()
    result = {"processed": 0, "failed": 0}
    with db.get_conn() as conn:
        dedup.attach_duplicates(conn)
        if not pending_count(conn):
            return result
        tags.load_aliases(conn)  # new stories store canonical tags
    if not ollama.available():
        log.warning("model server at %s unreachable; pipeline paused", ollama.endpoint())
        return result
    try:
        _drain(result, tried, emit, should_stop)
    finally:
        with db.get_conn() as conn:
            dedup.attach_duplicates(conn)
    return result


def _embed_batch(batch: list[dict[str, Any]]) -> dict[int, tuple[list[float], float]]:
    """``{article_id: (vector, seconds share)}`` for batch articles without a stored vector.

    One Ollama call per chunk instead of one per article; vectors are only stored when
    the article is processed, so pending ones never crowd the KNN results. On failure
    the articles fall back to embedding one by one.
    """
    with db.get_conn() as conn:
        todo = [a for a in batch if clustering.get_vector(conn, a["id"]) is None]
    out: dict[int, tuple[list[float], float]] = {}
    for i in range(0, len(todo), config.EMBED_BATCH):
        chunk = todo[i : i + config.EMBED_BATCH]
        t0 = time.perf_counter()
        try:
            vecs = ollama.embed_texts([clustering.embed_text_for(a) for a in chunk])
        except Exception as e:  # noqa: BLE001 - per-article embedding still works (or reports unavailable)
            log.warning("batch embedding failed (%s); embedding one by one", e)
            return out
        share = (time.perf_counter() - t0) / len(chunk)
        out.update({a["id"]: (v, share) for a, v in zip(chunk, vecs)})
    return out


def _drain(result: dict[str, int], tried: set[int], emit: Callable[[dict], None] | None,
           should_stop: Callable[[], bool]) -> None:
    # Newest first; low-trust feeds (Reddit ...) only after the rest of a backlog. Nothing is skipped.
    while not should_stop():
        with db.get_conn() as conn:
            rows = conn.execute(
                f"SELECT a.* FROM Article a JOIN Feed f ON f.id = a.feed_id "
                f"LEFT JOIN FeedCategory c ON c.name = COALESCE(f.category, '') WHERE {PENDING_WHERE} "
                "ORDER BY COALESCE(f.trust, c.trust, ?) < ?, a.id DESC LIMIT ?",
                (config.PIPELINE_MAX_FAILURES, config.SOURCE_TRUST_DEFAULT, config.BACKLOG_LOW_TRUST_BELOW,
                 config.PIPELINE_BATCH + len(tried)),
            ).fetchall()
        batch = [dict(r) for r in rows if r["id"] not in tried][: config.PIPELINE_BATCH]
        if not batch:
            return
        embedded = _embed_batch(batch)
        anchors = clustering.AnchorCache()
        for art in batch:
            if should_stop():
                return
            tried.add(art["id"])
            outcome = process_article(art, emit, anchors=anchors, embedded=embedded.get(art["id"]))
            if outcome == "unavailable":
                return
            result["processed" if outcome == "ok" else "failed"] += 1
            if outcome == "timeout":  # Ollama is overloaded: pause until the next wake-up
                return
