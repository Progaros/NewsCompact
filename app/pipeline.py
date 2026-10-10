"""The AI pipeline: Prompt1 (article assess) → embed → cluster → Prompt2 (story update).

Design notes:
- Embeddings are stored per article in the vec0 table `Article_Embeddings`,
  generated from *title-first* text (title + first 100 body words; title
  only for short bodies) — raw-body embeddings over-merged junk articles.
- Clustering is star/centroid mode: an article joins a story only if it is
  similar to the story's centroid (mean of member embeddings), restricted to
  stories updated within CLUSTER_WINDOW_HOURS. A stronger match can revive
  a stale story from any age (see decide_story).
- Prompt2 rewrites the whole story from its attached articles. If the combined
  text is large, older articles are compressed to bullets (map-reduce fallback).
"""
from __future__ import annotations

import datetime
import json
import logging
import math
import os
import resource
import statistics
import struct
import time
from typing import Any

from . import config, db, ollama
from .content_quality import classify as classify_content
from .tags import normalize_tags
from .harness import LocalJevHarness

log = logging.getLogger("news.pipeline")

# Shared, stateless harness instance (one httpx client under the hood).
_harness = LocalJevHarness()

_P1_SYSTEM = (
    "You are a news analyst. Read the article and respond with ONLY a JSON object "
    '{"tags": [...], "quality": N} where tags are 2-5 lowercase category labels '
    "and quality is an integer 1-10 for factual rigor and lack of sensationalism."
)
_P2_SYSTEM = (
    "You are a news editor. Given the source articles of one ongoing story, respond "
    "with ONLY a JSON object: "
    '{"headline": str, "summary": str, "tags": [...], "quality": int, '
    '"metrics": {"urgency_level": int 1-10, "impact_scale": "Local|Regional|National|Global", '
    '"status": "Breaking|Ongoing|Resolved"}}. '
    "Summary is 2-4 factual sentences. Do not invent facts not present in the articles."
)

# Combined-text budget before we start compressing older articles to bullets.
_CONTEXT_BUDGET_CHARS = int(os.getenv("NEWS_CONTEXT_BUDGET_CHARS", str(200_000)))


# --- text handling ---------------------------------------------------------
# Title-first embedding text: title + first N words of body. A title is the
# densest signal per token; embedding raw bodies over-merged junk articles
# (short i.redd.it posts, image captions) — see findings.md.
# Bodies classified as junk (bot-block chrome, social feed boilerplate) embed
# to nearly identical vectors regardless of topic, so junk bodies are
# NEVER used as embedding input — title only. That is what caused the
# 25-member "Googlebooks" catch-all story in 2026-10-10.
EMBED_MIN_BODY_WORDS = 20
EMBED_BODY_WORDS = 100


def embed_text_for(article: dict[str, Any]) -> str:
    title = (article.get("title") or "").strip()
    content_raw = article.get("content_raw") or ""
    body = content_raw.split()
    # Only 'ok' bodies carry topic signal; junk/thin/empty bodies are
    # boilerplate (bot-block pages, social chrome) and would cluster by
    # boilerplate similarity instead of topic similarity.
    if classify_content(content_raw) != "ok" or len(body) < EMBED_MIN_BODY_WORDS:
        return title or "news"
    return f"{title} {' '.join(body[:EMBED_BODY_WORDS])}".strip() or "news"


def _article_text(article: dict[str, Any]) -> str:
    return article.get("content_raw") or article.get("title") or ""


def _combined_story_text(articles: list[dict[str, Any]]) -> str:
    """Join article texts, compressing the older ones once we exceed the budget."""
    newest_first = list(reversed(articles))
    parts: list[str] = []
    used = 0
    for i, a in enumerate(newest_first):
        body = _article_text(a)
        header = f"### {a['title'] or 'untitled'}\n"
        if i == 0:
            parts.append(header + body)
            used += len(header) + len(body)
            continue
        # once over budget, compress older items to a single-line bullet
        if used + len(body) > _CONTEXT_BUDGET_CHARS:
            blurb = " ".join(body.split())[:400]
            bullet = f"- {a.get('title')} :: {blurb}"
            parts.append(bullet)
            used += len(bullet)
        else:
            parts.append(header + body)
            used += len(header) + len(body)
    return "\n\n".join(parts)


# --- embeddings ------------------------------------------------------------
def ensure_embedding(conn, article: dict[str, Any]) -> list[float] | None:
    """Return the article's vector, embedding + storing it if missing.

    Returns the existing vector if present (no re-embed, no duplicate row),
    or a freshly computed one. Only returns None when there is genuinely no
    text to embed (a bare 'news' fallback would embed, so this is rare).
    Previously returned None for the "already embedded" case too, which made
    the caller re-embed AND insert a second vec0 row on every pipeline retry,
    double-weighting centroids.
    """
    row = conn.execute(
        "SELECT embedding FROM Article_Embeddings WHERE article_id = ?",
        (article["id"],),
    ).fetchone()
    if row is not None:
        return _unpack_vec(bytes(row["embedding"]))
    text = embed_text_for(article)
    if not text.strip():
        return None
    vec = ollama.embed_one(text)
    conn.execute(
        "INSERT INTO Article_Embeddings (article_id, embedding) VALUES (?, ?)",
        (article["id"], db.pack_floats(vec)),
    )
    return vec


# --- Prompt 1: article-level ------------------------------------------------
def prompt1_assess(title: str, content: str) -> dict[str, Any]:
    user = f"Title: {title}\n\n" + content[:12000]
    out = ollama.chat_json(
        [
            {"role": "system", "content": _P1_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    tags = out.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    quality = out.get("quality", 5)
    try:
        quality = max(1, min(10, float(quality)))
    except (TypeError, ValueError):
        quality = 5.0
    return {"tags": normalize_tags(tags)[:5], "quality": quality}


# --- clustering ------------------------------------------------------------
# Star/centroid mode: an article joins a story only if it is similar to the
# story's *centroid* (mean of member embeddings), not to any single member.
# Chain mode (matching any member) chained topic-level similarity into
# 100+ article junk dumps — see findings.md. Centroids are recomputed per
# call from member embeddings, excluding content_class='junk' members: junk
# titles embed to generic blog-like vectors that would dilute the story's
# identity and let more junk join (self-reinforcing catch-alls, 2026-10-10).
# No schema change needed.
KNN_CANDIDATES = int(os.getenv("NEWS_KNN_CANDIDATES", "50"))
MAX_CENTROID_STORIES = int(os.getenv("NEWS_MAX_CENTROID_STORIES", "12"))


def _member_embeddings(conn, story_id: int, exclude_junk: bool) -> list:
    sql = (
        "SELECT e.embedding FROM Article_Embeddings e "
        "JOIN Article a ON a.id = e.article_id WHERE a.story_id = ?"
    )
    if exclude_junk:
        sql += " AND (a.content_class IS NULL OR a.content_class != 'junk')"
    return conn.execute(sql, (story_id,)).fetchall()


def _unpack_vec(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _story_centroid(conn, story_id: int) -> list[float] | None:
    rows = _member_embeddings(conn, story_id, exclude_junk=True)
    if not rows:
        # All members are junk (or none have embeddings yet): fall back to
        # everything so the story still has an identity.
        rows = _member_embeddings(conn, story_id, exclude_junk=False)
    if not rows:
        return None
    acc = [0.0] * config.EMBED_DIM
    for r in rows:
        v = _unpack_vec(bytes(r["embedding"]))
        for k in range(len(v)):
            acc[k] += v[k]
    n = len(rows)
    return _normalize([x / n for x in acc])


def decide_story(conn, vec: list[float]) -> tuple[int, bool]:
    """Return (story_id, created_new). Creates a story when nothing clusters.

    Two-tier decision, both centroid-based (see note above):
    1. Join a story updated within CLUSTER_WINDOW_HOURS with
       centroid sim >= CLUSTER_SIM_THRESHOLD (normal clustering).
    2. Otherwise, *revive* the best-matching story of any age with a stricter
       centroid sim >= CLUSTER_REVIVE_SIM_THRESHOLD. The story's updated_at is
       bumped by Prompt2 (which re-summarizes from all its articles, so the
       old context survives into the new summary).
    """
    vec_u = _normalize(vec)
    q = db.pack_floats(vec)
    cutoff = (
        datetime.datetime.utcnow()
        - datetime.timedelta(hours=config.CLUSTER_WINDOW_HOURS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    def _best_centroid_match(windowed: bool) -> tuple[int | None, float]:
        sql = (
            "SELECT a.story_id, vec_distance_cosine(e.embedding, ?) AS dist "
            "FROM Article_Embeddings e JOIN Article a ON a.id = e.article_id "
            "WHERE a.story_id IS NOT NULL"
        )
        params: list = [q]
        if windowed:
            sql += " AND a.story_id IN (SELECT id FROM Story WHERE updated_at >= ?)"
            params.append(cutoff)
        sql += " ORDER BY dist ASC LIMIT ?"
        params.append(KNN_CANDIDATES)

        # Candidate stories: nearest article per story, best first.
        candidates: dict[int, float] = {}
        for r in conn.execute(sql, params).fetchall():
            sid = r["story_id"]
            if sid is not None and sid not in candidates:
                candidates[sid] = 1.0 - float(r["dist"])

        best_story, best_sim = None, -1.0
        for sid in list(candidates)[:MAX_CENTROID_STORIES]:
            c = _story_centroid(conn, sid)
            if c is None:
                continue
            sim = sum(a * b for a, b in zip(vec_u, c))
            if sim > best_sim:
                best_story, best_sim = sid, sim
        return best_story, best_sim

    # --- tier 1: normal clustering inside the freshness window ---
    best_story, best_sim = _best_centroid_match(windowed=True)
    if best_story is not None and best_sim >= config.CLUSTER_SIM_THRESHOLD:
        return best_story, False

    # --- tier 2: revive a stale story with a stronger match ---
    revive_story, revive_sim = _best_centroid_match(windowed=False)
    if (
        revive_story is not None
        and revive_sim >= config.CLUSTER_REVIVE_SIM_THRESHOLD
    ):
        log.info("reviving stale story %d (centroid sim=%.3f)", revive_story, revive_sim)
        return revive_story, False

    # create a new story
    cur = conn.execute("INSERT INTO Story (headline, summary, tags, metrics, quality_score) "
                       "VALUES (NULL, NULL, '[]', '{}', 5.0)")
    return cur.lastrowid, True


# --- Prompt 2: story-level -------------------------------------------------
def prompt2_update_story(conn, story_id: int, quality: int | None = None,
                         breaking: bool | None = None) -> int:
    """Rewrite the story from its articles. Returns the prompt payload size
    in chars (0 when nothing to do) — used for load accounting."""
    articles = conn.execute(
        "SELECT * FROM Article WHERE story_id = ? ORDER BY published_date ASC",
        (story_id,),
    ).fetchall()
    if not articles:
        return 0
    arts = [dict(a) for a in articles]
    combined = _combined_story_text(arts)
    titles = "\n".join(f"- {a.get('title')}" for a in arts)
    user = f"Attached source articles:\n{titles}\n\nFull article text:\n{combined}"
    out = ollama.chat_json(
        [{"role": "system", "content": _P2_SYSTEM}, {"role": "user", "content": user}],
        temperature=0.2,
    )
    # The model sometimes wraps the object in an array ([{...}]) or returns a
    # bare list. Recover a dict when possible; otherwise raise so the article
    # is rolled back and retried next cycle (consistent with the prose case).
    if isinstance(out, list):
        out = next((x for x in out if isinstance(x, dict)), None)
    if not isinstance(out, dict):
        raise ValueError(f"Prompt2 returned non-object JSON: {str(out)[:200]!r}")
    metrics = out.get("metrics", {}) or {}
    metrics.setdefault("urgency_level", 5)
    metrics.setdefault("impact_scale", "National")
    metrics.setdefault("status", "Ongoing")
    tags = out.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    # Normalize at storage time so interest-weight matching (scoring) always
    # agrees across LLM spellings ("AI" vs "ai", "machine-learning", ...).
    tags = normalize_tags(tags)[:6]
    # Story quality = robust aggregate (median) of the member per-article
    # qualities stored by the harness. Junk fetches are scored from their
    # headline (see content_quality), so one bot-block page can no longer drag
    # a real story down — this replaces the old last-writer-wins behaviour.
    # Fallback for stories whose articles predate the per-article quality column.
    quals = [a["quality"] for a in arts if a.get("quality") is not None]
    if quals:
        final_quality = round(float(statistics.median(quals)), 2)
    else:
        raw_quality: Any = quality if quality is not None else out.get("quality", 5)
        try:
            final_quality = max(1.0, min(10.0, float(raw_quality)))
        except (TypeError, ValueError):
            final_quality = 5.0
    headline = (out.get("headline") or "").strip() or arts[-1].get("title", "Untitled")
    summary = (out.get("summary") or "").strip()
    # A breaking boolean forces status so the SSE urgency check is consistent.
    if breaking:
        metrics["status"] = "Breaking"

    conn.execute(
        "UPDATE Story SET headline=?, summary=?, tags=?, metrics=?, quality_score=? "
        "WHERE id=?",
        (
            headline,
            summary,
            json.dumps(tags),
            json.dumps(metrics),
            final_quality,
            story_id,
        ),
    )
    conn.execute(
        "UPDATE Story SET updated_at = datetime('now') WHERE id = ?", (story_id,)
    )
    conn.commit()
    return len(user)


# --- orchestration ---------------------------------------------------------
def _proc_cpu() -> float:
    """Process CPU seconds (user+sys) for load accounting."""
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def repair_stale_placeholder_stories() -> int:
    """Un-pend articles stuck in placeholder stories (headline never filled).

    A NULL-headline story can only exist when the process died mid-cycle
    (after story creation, before Prompt2 ran). Drop the story and re-pend
    its article(s); they get re-clustered on the next pass. Safe to call at
    the start of every cycle: stories created within a cycle are never
    NULL-headed at cycle start.
    """
    with db.get_conn() as conn:
        orphans = conn.execute(
            "SELECT id FROM Story WHERE headline IS NULL"
        ).fetchall()
        for o in orphans:
            conn.execute("UPDATE Article SET story_id = NULL WHERE story_id = ?", (o["id"],))
            conn.execute("DELETE FROM Story WHERE id = ?", (o["id"],))
    if orphans:
        log.info("repaired %d stale placeholder stories", len(orphans))
    return len(orphans)


def process_new_articles(limit: int = 50, emit: Any = None) -> int:
    """Find unassigned articles, embed, cluster, and refresh their stories.

    `emit` is an optional callable(record: dict) used to push SSE notifications.
    Returns the number of articles processed.
    """
    repair_stale_placeholder_stories()
    processed = 0
    with db.get_conn() as conn:
        # Disabled feeds (Feed.enabled = 0) still ingest RSS rows but wait in the
        # unclustered backlog: no AI spend until re-enabled.
        pending = conn.execute(
            "SELECT a.* FROM Article a JOIN Feed f ON f.id = a.feed_id "
            "WHERE a.story_id IS NULL AND f.enabled = 1 ORDER BY a.id LIMIT ?",
            (limit,),
        ).fetchall()
    for article in pending:
        art = dict(article)
        story_id: int | None = None
        created = False
        wall0 = time.perf_counter()
        cpu0 = _proc_cpu()
        t_embed = t_knn = t_harness = t_p2 = 0.0
        p2_chars = 0
        try:
            with db.get_conn() as conn:
                # Embed, then cluster. (Prompt1 runs implicitly per story via
                # Prompt2, which emits tags+quality; the per-article Prompt1
                # call was skipped for speed — see plan.md changelog.)
                t0 = time.perf_counter()
                vec = ensure_embedding(conn, art)
                if vec is None:
                    # No text at all — embed the bare fallback (stored for parity).
                    vec = ollama.embed_one(embed_text_for(art) or "news")
                    conn.execute(
                        "INSERT INTO Article_Embeddings (article_id, embedding) VALUES (?, ?)",
                        (art["id"], db.pack_floats(vec)),
                    )
                t_embed = time.perf_counter() - t0
                t0 = time.perf_counter()
                new_story_id, created = decide_story(conn, vec)
                t_knn = time.perf_counter() - t0
                story_id = new_story_id
                conn.execute(
                    "UPDATE Article SET story_id = ?, created_at = COALESCE(created_at, datetime('now')) WHERE id=?",
                    (story_id, art["id"]),
                )
                # Fast schema-constrained scoring (Jev-style harness): quality +
                # breaking flag. A harness failure must not kill the article —
                # fall back to Prompt2's parsed values.
                t0 = time.perf_counter()
                try:
                    assessment = _harness.assess_article(
                        art.get("title", ""), _article_text(art)
                    )
                    log.info(
                        "harness article=%s quality=%s breaking=%s",
                        art.get("id"), assessment.get("quality"), assessment.get("breaking"),
                    )
                except Exception:  # noqa: BLE001
                    log.exception("harness assess failed for article %s", art.get("id"))
                    assessment = {}
                t_harness = time.perf_counter() - t0
                # Persist the per-article quality + content class. Story quality
                # is the median of these across member articles (see prompt2),
                # so a single junk fetch can't drag a real story down.
                conn.execute(
                    "UPDATE Article SET quality = ?, content_class = ? WHERE id = ?",
                    (assessment.get("quality"), assessment.get("content_class"), art["id"]),
                )
                t0 = time.perf_counter()
                p2_chars = prompt2_update_story(
                    conn, story_id,
                    quality=assessment.get("quality"), breaking=assessment.get("breaking"),
                )
                t_p2 = time.perf_counter() - t0
            # notify on breaking stories
            with db.get_conn() as conn:
                s = conn.execute("SELECT * FROM Story WHERE id=?", (story_id,)).fetchone()
                if s is not None:
                    metrics = json.loads(s["metrics"] or "{}")
                    if metrics.get("urgency_level", 0) >= 8 and metrics.get("status") == "Breaking" and emit:
                        emit({"type": "story_update", "story_id": s["id"], "breaking": True})
            log.info(
                "perf article=%s story=%s created=%s wall=%.1fs cpu=%.1fs "
                "embed=%.2fs knn=%.2fs harness=%.2fs p2=%.2fs p2chars=%d",
                art["id"], story_id, created,
                time.perf_counter() - wall0, _proc_cpu() - cpu0,
                t_embed, t_knn, t_harness, t_p2, p2_chars,
            )
        except Exception:  # noqa: BLE001 - one bad article must not stall the cycle
            log.exception("pipeline error for article %s", art.get("id"))
            # Un-assign so the article is retried next cycle; drop the
            # placeholder story if this cycle created it.
            with db.get_conn() as conn:
                conn.execute("UPDATE Article SET story_id = NULL WHERE id = ?", (art["id"],))
            if created and story_id is not None:
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM Story WHERE id = ?", (story_id,))
            continue
        processed += 1
    return processed
