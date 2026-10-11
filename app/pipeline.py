"""The AI pipeline: Prompt1 (article assess) → embed → cluster → Prompt2 (story update).

Design notes:
- Embeddings are stored per article in the vec0 table `Article_Embeddings`,
  generated from *title-first* text (title + first 100 body words; title
  only for short bodies) — raw-body embeddings over-merged broken articles.
- Clustering is member-anchor mode: an article joins a story only if it is
  similar to at least one of the story's non-broken member embeddings (never
  the mean), restricted to stories updated within CLUSTER_WINDOW_HOURS. A
  stronger match can revive a stale story from any age (see decide_story).
- Prompt2 uses full context for new stories and an incremental current-summary
    plus new-article payload for multi-member stories.
"""
from __future__ import annotations

import datetime
import json
import logging
import math
import os
import re
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
    '"location": str, "language": str, '
    '"metrics": {"urgency_level": int 1-10, "impact_scale": "Local|Regional|National|Global", '
    '"status": "Breaking|Ongoing|Resolved"}}. '
    "location is the most specific place the news is about as 'City, Country' "
    "(e.g. 'Munich, Germany'), or 'Global' when it has no specific local setting. "
    "language is the two-letter code of the articles' main language (e.g. 'de', 'en'). "
    "Summary is 2-4 factual sentences. Do not invent facts not present in the articles."
)
_P2_INCREMENTAL_SYSTEM = (
    "You are updating an active news story. Review the Current Summary and the "
    "New Article. If the New Article introduces material updates, rewrite the "
    "summary in 2-4 factual sentences to incorporate them. If it adds no new "
    "facts, return the Current Summary exactly as written. Preserve the story "
    "headline unless the new facts require a clearer headline. Return the same "
    "JSON object schema as requested. location/language describe the story's "
    "setting — return them as-is unless the new article clearly changes it."
)
# JSON schema for the constrained /api/chat call (see _P2_SYSTEM). Keeps the
# prose-JSON parse failure structural: the model emits this shape, not text.
_P2_IMPACTS = ("Local", "Regional", "National", "Global")
_P2_STATUSES = ("Breaking", "Ongoing", "Resolved")
_P2_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "quality": {"type": "integer", "minimum": 1, "maximum": 10},
        # Distance sorting (see config.USER_CITY): where the story is about +
        # its language. Optional — old/incremental outputs may omit them.
        "location": {"type": "string"},
        "language": {"type": "string"},
        "metrics": {
            "type": "object",
            "properties": {
                "urgency_level": {"type": "integer", "minimum": 1, "maximum": 10},
                "impact_scale": {"type": "string", "enum": list(_P2_IMPACTS)},
                "status": {"type": "string", "enum": list(_P2_STATUSES)},
            },
            "required": ["urgency_level", "impact_scale", "status"],
        },
    },
    "required": ["headline", "summary", "tags", "quality", "metrics"],
}


def _sanitize_metrics(metrics: Any) -> dict:
    """Coerce Prompt2 metrics to valid values (log-only; never raises).

    Today only *missing* keys were filled (``setdefault``) — an out-of-range
    urgency or a misspelled enum passed straight through to the UI. Clamp +
    default the present-but-invalid values instead so the SSE urgency check
    and the frontend always see clean enums.
    """
    m = dict(metrics) if isinstance(metrics, dict) else {}
    try:
        urg = int(m.get("urgency_level"))
    except (TypeError, ValueError):
        urg = None
    if urg is None or not (1 <= urg <= 10):
        if m.get("urgency_level") is not None:
            log.warning("prompt2: invalid urgency_level %r -> 5", m.get("urgency_level"))
        m["urgency_level"] = 5
    m["urgency_level"] = urg if (1 <= urg <= 10) else 5
    if m.get("impact_scale") not in _P2_IMPACTS:
        if m.get("impact_scale") is not None:
            log.warning("prompt2: invalid impact_scale %r -> National", m.get("impact_scale"))
        m["impact_scale"] = "National"
    if m.get("status") not in _P2_STATUSES:
        if m.get("status") is not None:
            log.warning("prompt2: invalid status %r -\u003e Ongoing", m.get("status"))
        m["status"] = "Ongoing"
    return m


# Sentence-ish splitter for the summary length check (loose; prose punctuation).
_SENT_SPLIT = re.compile(r"[.!?]+\s*")


def _validate_p2(story_id: int, out: dict) -> None:
    """Sanity-check Prompt2 output and log violations (never raises).

    "Store raw output on violation" is done via a WARNING log with the story
    id + the offending fields: the diagnostics table is out of scope for now
    and the log is greppable. Hard failures (no headline at all, no summary)
    are downgraded to a fallback by the caller rather than failing the article.
    """
    headline = (out.get("headline") or "").strip()
    summary = (out.get("summary") or "").strip()
    problems = []
    if not headline:
        problems.append("empty headline")
    elif len(headline) > 200:
        problems.append(f"headline too long ({len(headline)} chars)")
    if not summary:
        problems.append("empty summary")
    else:
        n_sents = len([s for s in _SENT_SPLIT.split(summary) if s.strip()])
        if n_sents < 1 or n_sents > 6:
            problems.append(f"summary outside 1-6 sentences ({n_sents})")
    if problems:
        log.warning(
            "prompt2 violation for story %s: %s | raw=%s",
            story_id, "; ".join(problems), json.dumps(out, ensure_ascii=False)[:400],
        )

# Combined-text budget before we start compressing older articles to bullets.
_CONTEXT_BUDGET_CHARS = int(os.getenv("NEWS_CONTEXT_BUDGET_CHARS", str(200_000)))


# --- text handling ---------------------------------------------------------
# Title-first embedding text: title + first N words of body. A title is the
# densest signal per token; embedding raw bodies over-merged broken articles
# (short i.redd.it posts, image captions) — see findings.md.
# Bodies classified as broken (bot-block chrome, social feed boilerplate)
# embed to nearly identical vectors regardless of topic, so broken bodies are
# NEVER used as embedding input — title only. That is what caused the
# 25-member "Googlebooks" catch-all story in 2026-10-10.
EMBED_MIN_BODY_WORDS = 20
EMBED_BODY_WORDS = 100


def embed_text_for(article: dict[str, Any]) -> str:
    title = (article.get("title") or "").strip()
    content_raw = article.get("content_raw") or ""
    body = content_raw.split()
    # Only 'ok' bodies carry topic signal; broken/thin/empty bodies are
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
# Member-anchor mode: an article joins a story only if it is similar to at
# least one of the story's *anchor* (non-broken) members, never to the mean.
# Centroid mode averaged member embeddings into a point in the "middle" of
# the cluster, so articles ~0.40 from the real story sat ~0.60 from the mean
# and joined at the 0.55 threshold — story 7 became a 23-article mush of
# OpenAI-math/NIH/Nobel/funding blurbs (2026-10-11). Anchors exclude
# content_class='broken' members (title-only boilerplate vectors); a
# broken-only story falls back to all members so it still has an identity.
KNN_CANDIDATES = int(os.getenv("NEWS_KNN_CANDIDATES", "50"))
MAX_CANDIDATE_STORIES = int(os.getenv("NEWS_MAX_CENTROID_STORIES", "12"))


def _story_anchors(conn, story_id: int) -> list[list[float]]:
    """Normalized anchor vectors: non-broken member embeddings, falling back
    to all members when the story has no real-body anchor (broken-only)."""
    def _vecs(exclude_broken: bool) -> list[list[float]]:
        sql = (
            "SELECT e.embedding FROM Article_Embeddings e "
            "JOIN Article a ON a.id = e.article_id WHERE a.story_id = ?"
        )
        if exclude_broken:
            sql += " AND (a.content_class IS NULL OR a.content_class != 'broken')"
        rows = conn.execute(sql, (story_id,)).fetchall()
        return [_normalize(_unpack_vec(bytes(r["embedding"]))) for r in rows]

    vecs = _vecs(True)
    return vecs if vecs else _vecs(False)


def _unpack_vec(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def decide_story(conn, vec: list[float], article: dict[str, Any] | None = None) -> tuple[int, bool, bool]:
    """Return (story_id, created_new, revived). Creates a story when nothing clusters.

    Two-tier decision, both member-anchor based (see note above). Each
    candidate story is scored by the article's max similarity to any of the
    story's anchor (non-broken) members. Tier 1: join a story updated within
    CLUSTER_WINDOW_HOURS clearing CLUSTER_SIM_THRESHOLD. Tier 2: otherwise
    *revive* the best-matching story of any age at CLUSTER_REVIVE_SIM_THRESHOLD
    (slightly looser, so a resurfacing topic links to its old story instead of
    spawning a clone). Prompt2 bumps updated_at and re-summarizes from all
    articles, so the old context survives into the new summary.
    re-summarizes from all articles, so the old context survives into the
    new summary.
    """
    vec_u = _normalize(vec)
    q = db.pack_floats(vec)
    cutoff = (
        datetime.datetime.utcnow()
        - datetime.timedelta(hours=config.CLUSTER_WINDOW_HOURS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    def _candidate_sims(windowed: bool) -> list[tuple[int, float]]:
        """[(story_id, max anchor sim)] best (nearest article) first."""
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

        # Candidate stories: nearest article per story, best first, deduped.
        # Safe to gate on the full member set: the nearest member is a
        # member, so max-anchor-sim >= sim to it; if the nearest misses
        # every threshold, no member can clear it.
        seen: list[int] = []
        seen_set: set[int] = set()
        for r in conn.execute(sql, params).fetchall():
            sid = r["story_id"]
            if sid is not None and sid not in seen_set:
                seen_set.add(sid)
                seen.append(sid)

        out: list[tuple[int, float]] = []
        for sid in seen[:MAX_CANDIDATE_STORIES]:
            anchors = _story_anchors(conn, sid)
            if not anchors:
                continue
            sim = max(sum(x * y for x, y in zip(vec_u, a)) for a in anchors)
            out.append((sid, sim))
        return out

    def _best_clearing(cands, threshold: float) -> tuple[int | None, float]:
        best_story, best_sim = None, -1.0
        for sid, sim in cands:
            if sim >= threshold and sim > best_sim:
                best_story, best_sim = sid, sim
        return best_story, best_sim

    def _verify_candidates(cands, threshold: float) -> tuple[int | None, float]:
        """Verify high-recall candidates as exact event matches.

        Cosine retrieval supplies recall; the verifier prevents same-topic
        stories from merging. A missing article context falls back to the
        cosine path so maintenance scripts and old callers remain compatible.
        Verification failures reject that candidate rather than silently
        weakening the precision guard.
        """
        if article is None:
            return _best_clearing(cands, config.CLUSTER_SIM_THRESHOLD)
        candidate_rows = []
        ranked = sorted(cands, key=lambda item: item[1], reverse=True)
        for sid, sim in ranked[:config.CLUSTER_VERIFY_MAX_CANDIDATES]:
            if sim < threshold:
                break
            row = conn.execute(
                "SELECT headline, summary FROM Story WHERE id = ?", (sid,)
            ).fetchone()
            if row is None:
                continue
            candidate_rows.append({
                "id": sid,
                "headline": row["headline"] or "",
                "summary": row["summary"] or "",
                "similarity": sim,
            })
        if not candidate_rows:
            return None, -1.0
        try:
            decisions = _harness.classify_event_candidates(
                article.get("title") or "",
                _article_text(article),
                candidate_rows,
            )
        except Exception:  # noqa: BLE001
            log.exception("event verification failed for article %s", article.get("id"))
            return None, -1.0
        for candidate in candidate_rows:
            sid = candidate["id"]
            sim = candidate["similarity"]
            classification = decisions.get(sid, "UNRELATED")
            log.info(
                "event verification article=%s story=%s sim=%.3f class=%s",
                article.get("id"), sid, sim, classification,
            )
            if classification in {"EXACT_MATCH", "CONTINUING_DEVELOPMENT"}:
                return sid, sim
        return None, -1.0

    # --- tier 1: normal clustering inside the freshness window ---
    tier_one = _candidate_sims(windowed=True)
    if config.CLUSTER_MODE == "two_stage":
        best_story, best_sim = _verify_candidates(
            tier_one, config.CLUSTER_VERIFY_RETRIEVE_THRESHOLD
        )
    else:
        best_story, best_sim = _best_clearing(tier_one, config.CLUSTER_SIM_THRESHOLD)
    if best_story is not None:
        return best_story, False, False

    # --- tier 2: revive a stale story with a stronger match ---
    tier_two = _candidate_sims(windowed=False)
    if config.CLUSTER_MODE == "two_stage":
        revive_story, revive_sim = _verify_candidates(
            tier_two, config.CLUSTER_REVIVE_SIM_THRESHOLD
        )
    else:
        revive_story, revive_sim = _best_clearing(
            tier_two, config.CLUSTER_REVIVE_SIM_THRESHOLD
        )
    if revive_story is not None:
        log.info("reviving story %d (anchor sim=%.3f)", revive_story, revive_sim)
        return revive_story, False, True

    # create a new story
    cur = conn.execute("INSERT INTO Story (headline, summary, tags, metrics, quality_score) "
                       "VALUES (NULL, NULL, '[]', '{}', 5.0)")
    return cur.lastrowid, True, False


# --- Prompt 2: story-level -------------------------------------------------
def prompt2_update_story(conn, story_id: int, quality: int | None = None,
                         breaking: bool | None = None,
                         article_id: int | None = None,
                         force_full: bool = False) -> int:
    """Rewrite the story from its articles. Returns the prompt payload size
    in chars (0 when nothing to do) — used for load accounting."""
    articles = conn.execute(
        "SELECT * FROM Article WHERE story_id = ? ORDER BY published_date ASC",
        (story_id,),
    ).fetchall()
    if not articles:
        return 0
    arts = [dict(a) for a in articles]
    story = conn.execute(
        "SELECT headline, summary, metrics FROM Story WHERE id = ?" , (story_id,)
    ).fetchone()
    incremental = len(arts) >= 2 and story is not None and not force_full
    if incremental:
        new_article = next((a for a in arts if a.get("id") == article_id), arts[-1])
        new_body = " ".join((_article_text(new_article) or "").split()[:1500])
        user = (
            f"Current Headline:\n{story['headline'] or new_article.get('title', '')}\n\n"
            f"Current Summary:\n{story['summary'] or '(none)'}\n\n"
            f"New Article Title:\n{new_article.get('title', '')}\n\n"
            f"New Article Text:\n{new_body}"
        )
        system = _P2_INCREMENTAL_SYSTEM
    else:
        combined = _combined_story_text(arts)
        titles = "\n".join(f"- {a.get('title')}" for a in arts)
        user = f"Attached source articles:\n{titles}\n\nFull article text:\n{combined}"
        system = _P2_SYSTEM
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    # Schema-constrained (native /api/chat + format): the model emits the
    # object, so the prose-JSON / bare-list parse failure is killed
    # structurally. chat_json_constrained falls back to prose chat_json when
    # the constrained endpoint returns empty content (the Qwen ``think``
    # interaction), so this never regresses the old path.
    out = ollama.chat_json_constrained(_P2_SCHEMA, messages, temperature=0.2)
    # The prose fallback can still wrap the object in an array ([{...}]) or
    # return a bare list. Recover a dict when possible; otherwise raise so the
    # article is rolled back and retried next cycle (consistent with prose).
    if isinstance(out, list):
        out = next((x for x in out if isinstance(x, dict)), None)
    if not isinstance(out, dict):
        raise ValueError(f"Prompt2 returned non-object JSON: {str(out)[:200]!r}")
    _validate_p2(story_id, out)
    metrics = _sanitize_metrics(out.get("metrics", {}))
    tags = out.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    # Normalize at storage time so interest-weight matching (scoring) always
    # agrees across LLM spellings ("AI" vs "ai", "machine-learning", ...).
    tags = normalize_tags(tags)[:6]
    # Story quality = robust aggregate (median) of the member per-article
    # qualities stored by the harness. Broken fetches are scored from their
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
    # A breaking boolean forces status so the SSE urgency check is consistent
    # (sanitized already; keep it last so it can't be clobbered by the enum check).
    if breaking:
        metrics["status"] = "Breaking"
    # Distance sorting (scoring.distance_score): location + language come from
    # this same Prompt2 call — no separate classifier. Optional in the schema:
    # on an incremental update an omission keeps what the story already had,
    # so a passing article can't wipe the field.
    location = str(out.get("location") or "").strip()[:120]
    language = str(out.get("language") or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}(-[a-z]{2,4})?", language):
        language = ""
    if story is not None:
        try:
            prev_metrics = json.loads(story["metrics"] or "{}")
        except json.JSONDecodeError:
            prev_metrics = {}
        if not location:
            location = str(prev_metrics.get("location") or "").strip()[:120]
        if not language:
            language = str(prev_metrics.get("language") or "").strip()
    if location:
        metrics["location"] = location
    if language:
        metrics["language"] = language

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


def cleanup_orphaned_stories() -> int:
    """Delete stories with zero articles ("ghost stories").

    A story can lose its members without ever being deleted: e.g. two
    concurrent pipeline passes (the pre-lock refresh endpoint) both create a
    story for the same article, and the article's story_id ends up pointing
    at one twin only. The story row keeps its Prompt2 headline/summary but
    shows "0 sources · 0 articles" in the UI. Stories created within a
    cycle always have their article assigned by cycle end, so reaping at
    the start of every cycle is safe. Cheap: one NOT-INDEX-LOOKUP scan
    over a small table.
    """
    with db.get_conn() as conn:
        ghosts = conn.execute(
            "SELECT COUNT(*) FROM Story s WHERE NOT EXISTS "
            "(SELECT 1 FROM Article a WHERE a.story_id = s.id)"
        ).fetchone()[0]
        if ghosts:
            conn.execute(
                "DELETE FROM Story WHERE id NOT IN "
                "(SELECT DISTINCT story_id FROM Article WHERE story_id IS NOT NULL)"
            )
            log.info("reaped %d orphaned stories (zero articles)", ghosts)
    return ghosts


def process_new_articles(limit: int = 50, emit: Any = None) -> int:
    """Find unassigned articles, embed, cluster, and refresh their stories.

    `emit` is an optional callable(record: dict) used to push SSE notifications.
    Returns the number of articles processed.
    """
    repair_stale_placeholder_stories()
    cleanup_orphaned_stories()
    processed = 0
    with db.get_conn() as conn:
        # Disabled feeds (Feed.enabled = 0) still ingest RSS rows but wait in the
        # unclustered backlog: no AI spend until re-enabled.
        pending = conn.execute(
            "SELECT a.* FROM Article a JOIN Feed f ON f.id = a.feed_id "
            "WHERE a.story_id IS NULL AND f.enabled = 1 "
            "AND COALESCE(a.content_class, 'empty') != 'broken' "
            "AND COALESCE(a.pipeline_failures, 0) < ? ORDER BY a.id LIMIT ?",
            (config.PIPELINE_MAX_FAILURES, limit),
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
                new_story_id, created, revived = decide_story(conn, vec, art)
                t_knn = time.perf_counter() - t0
                story_id = new_story_id
                conn.execute(
                    "UPDATE Article SET story_id = ?, created_at = COALESCE(created_at, datetime('now')) WHERE id=?",
                    (story_id, art["id"]),
                )
                # Junk-joins-junk skip: a `thin` article joining a junk story
                # (quality_score <= JUNK_SKIP_QUALITY_THRESHOLD, or all members
                # broken/thin) adds no signal worth two 27B calls — skip the
                # harness and Prompt2 and keep the story's existing summary,
                # headline and tags. New stories (created=True) never skip: the
                # first summary must always exist.
                junk_skip = False
                if not created:
                    s_row = conn.execute(
                        "SELECT quality_score FROM Story WHERE id=?", (story_id,)
                    ).fetchone()
                    member_classes = {
                        m["content_class"] or ""
                        for m in conn.execute(
                            "SELECT content_class FROM Article WHERE story_id=? AND id != ?",
                            (story_id, art["id"]),
                        )
                    }
                    s_quality = s_row["quality_score"] if s_row is not None else None
                    junk_skip = (
                        art.get("content_class") == "thin"
                        and (
                            (s_quality is not None
                             and s_quality <= config.JUNK_SKIP_QUALITY_THRESHOLD)
                            or member_classes <= {"", "broken", "thin"}
                        )
                    )
                    if junk_skip:
                        log.info(
                            "junk-skip article=%s story=%s story_quality=%s members=%s",
                            art.get("id"), story_id, s_quality, sorted(member_classes),
                        )
                # Fast schema-constrained scoring (Jev-style harness): quality +
                # breaking flag. A harness failure must not kill the article —
                # fall back to Prompt2's parsed values. Skipped on junk-on-junk
                # joins (quality stays NULL there — the story's median quality
                # is unaffected).
                t0 = time.perf_counter()
                if junk_skip:
                    assessment = {}
                else:
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
                # so a single broken fetch can't drag a real story down.
                conn.execute(
                    "UPDATE Article SET quality = ? WHERE id = ?",
                    (assessment.get("quality"), art["id"]),
                )
                t0 = time.perf_counter()
                if junk_skip:
                    # Junk-on-junk: story keeps its existing summary/quality.
                    p2_chars = 0
                else:
                    p2_chars = prompt2_update_story(
                        conn, story_id,
                        quality=assessment.get("quality"), breaking=assessment.get("breaking"),
                        article_id=art["id"],
                        force_full=revived,
                    )
                t_p2 = time.perf_counter() - t0
            # notify on breaking stories
            with db.get_conn() as conn:
                # Persist the successful run's per-stage wall times for /list.
                # Stages store 0.0 when skipped (junk-on-junk) — NULL means the
                # article was never fully processed. Resets the failure counter.
                wall_s = time.perf_counter() - wall0
                conn.execute(
                    "UPDATE Article SET pipeline_failures = 0, "
                    "t_embed_s = ?, t_cluster_s = ?, t_harness_s = ?, "
                    "t_story_s = ?, t_wall_s = ? WHERE id = ?",
                    (t_embed, t_knn, t_harness, t_p2, wall_s, art["id"]),
                )
                s = conn.execute("SELECT * FROM Story WHERE id=?", (story_id,)).fetchone()
                if s is not None:
                    metrics = json.loads(s["metrics"] or "{}")
                    if metrics.get("urgency_level", 0) >= 8 and metrics.get("status") == "Breaking" and emit:
                        emit({"type": "story_update", "story_id": s["id"], "breaking": True})
            log.info(
                "perf article=%s story=%s created=%s wall=%.1fs cpu=%.1fs "
                "embed=%.2fs knn=%.2fs harness=%.2fs p2=%.2fs p2chars=%d junk_skip=%d",
                art["id"], story_id, created,
                time.perf_counter() - wall0, _proc_cpu() - cpu0,
                t_embed, t_knn, t_harness, t_p2, p2_chars, int(junk_skip),
            )
        except Exception:  # noqa: BLE001 - one bad article must not stall the cycle
            log.exception("pipeline error for article %s", art.get("id"))
            # Un-assign so the article is retried next cycle; drop the
            # placeholder story if this cycle created it.
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE Article SET story_id = NULL, quality = NULL, "
                    "content_class = NULL, pipeline_failures = "
                    "COALESCE(pipeline_failures, 0) + 1 WHERE id = ?",
                    (art["id"],),
                )
                failures = conn.execute(
                    "SELECT pipeline_failures FROM Article WHERE id = ?",
                    (art["id"],),
                ).fetchone()["pipeline_failures"]
            if failures >= config.PIPELINE_MAX_FAILURES:
                log.error(
                    "dead-letter article %s after %d pipeline failures",
                    art["id"], failures,
                )
            if created and story_id is not None:
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM Story WHERE id = ?", (story_id,))
            continue
        processed += 1
    return processed
