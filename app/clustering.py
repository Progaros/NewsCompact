"""Embedding + story assignment (vec0 KNN retrieval, optional LLM verifier).

A candidate story is scored by the article's max cosine similarity to any of
its *anchor* members (non-broken members; all members for broken-only
stories) — never a centroid, which drifted into catch-all "mush" stories.

Tier 1 joins a story updated within CLUSTER_WINDOW_HOURS; tier 2 revives an
older (stale) story instead of spawning a duplicate.
In ``two_stage`` mode the cosine step only retrieves candidates and the
harness verifier decides; only EXACT_MATCH / CONTINUING_DEVELOPMENT attach.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import sqlite3
from typing import Any

from . import config, db, harness, ollama
from .content_quality import classify as classify_content
from .content_quality import split_post

log = logging.getLogger("news.clustering")

EMBED_MIN_BODY_WORDS = 20
EMBED_BODY_WORDS = 100
_VT = db.VECTOR_TABLE

Vector = list[float]


def embed_text_for(article: dict[str, Any]) -> str:
    """Title-first text: title + first 100 post words; title only for non-'ok' bodies.

    Boilerplate bodies embed to near-identical vectors regardless of topic, and
    Reddit/HN comment threads are opinions, not the event — both stay out.
    """
    title = (article.get("title") or "").strip()
    raw, _comments = split_post(article.get("content_raw"))
    body = raw.split()
    if classify_content(raw) != "ok" or len(body) < EMBED_MIN_BODY_WORDS:
        return title or "news"
    return f"{title} {' '.join(body[:EMBED_BODY_WORDS])}".strip() or "news"


# --- vector storage --------------------------------------------------------
def get_vector(conn: sqlite3.Connection, article_id: int) -> Vector | None:
    row = conn.execute(f"SELECT embedding FROM {_VT} WHERE article_id = ?", (article_id,)).fetchone()
    return db.unpack_floats(bytes(row[0])) if row else None


def ensure_embedding(conn: sqlite3.Connection, article: dict[str, Any]) -> Vector:
    """Return the stored vector, embedding + storing it once if missing."""
    vec = get_vector(conn, article["id"])
    if vec is None:
        vec = ollama.embed_one(embed_text_for(article))
        conn.execute(f"INSERT INTO {_VT} (article_id, embedding) VALUES (?, ?)",
                     (article["id"], db.pack_floats(vec)))
    return vec


def delete_embeddings(conn: sqlite3.Connection, article_ids: list[int]) -> None:
    """vec0 rows are not covered by foreign-key cascades."""
    for aid in article_ids:
        conn.execute(f"DELETE FROM {_VT} WHERE article_id = ?", (aid,))


def normalize(v: Vector) -> Vector:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def dot(a: Vector, b: Vector) -> float:
    return sum(x * y for x, y in zip(a, b))


def knn(conn: sqlite3.Connection, vec: Vector, k: int) -> list[tuple[int, float]]:
    """Nearest stored articles: ``[(article_id, cosine similarity)]`` best first."""
    rows = conn.execute(
        f"SELECT article_id, distance FROM {_VT} WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (db.pack_floats(vec), k),
    )
    return [(r[0], 1.0 - r[1]) for r in rows]


# --- candidate stories -----------------------------------------------------
class AnchorCache:
    """Normalized anchor vectors per story (non-broken members, else all members)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._cache: dict[int, list[Vector]] = {}

    def __call__(self, story_id: int) -> list[Vector]:
        if story_id not in self._cache:
            rows = self.conn.execute(
                "SELECT id, COALESCE(content_class, '') = 'broken' FROM Article WHERE story_id = ?",
                (story_id,),
            ).fetchall()
            ids = [r[0] for r in rows if not r[1]] or [r[0] for r in rows]
            vecs = (get_vector(self.conn, aid) for aid in ids)
            self._cache[story_id] = [normalize(v) for v in vecs if v is not None]
        return self._cache[story_id]


def window_cutoff() -> str:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=config.CLUSTER_WINDOW_HOURS)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def nearest_stories(conn: sqlite3.Connection, vec: Vector, anchors: AnchorCache, *,
                    since: str | None = None, before: str | None = None,
                    exclude: int | None = None) -> list[tuple[int, float]]:
    """``[(story_id, max anchor similarity)]`` for the stories of the nearest articles.

    ``since`` / ``before`` keep only stories updated at/after resp. before that time.
    Gating on the nearest member is safe: max-anchor-sim >= sim to it.
    """
    neighbours = knn(conn, vec, config.KNN_CANDIDATES)
    if not neighbours:
        return []
    marks = ",".join("?" * len(neighbours))
    meta = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT a.id, a.story_id, s.updated_at FROM Article a JOIN Story s ON s.id = a.story_id "
        f"WHERE a.id IN ({marks})",
        [aid for aid, _ in neighbours],
    )}
    story_ids: list[int] = []
    for aid, _ in neighbours:
        if aid not in meta:
            continue
        sid, updated = meta[aid]
        if sid == exclude or sid in story_ids or (since and (updated or "") < since):
            continue
        if before and (updated or "") >= before:
            continue
        story_ids.append(sid)
    vec_u = normalize(vec)
    out = []
    for sid in story_ids[: config.MAX_CANDIDATE_STORIES]:
        story_anchors = anchors(sid)
        if story_anchors:
            out.append((sid, max(dot(vec_u, a) for a in story_anchors)))
    return out


# --- decision --------------------------------------------------------------
def _best_clearing(cands: list[tuple[int, float]], threshold: float) -> tuple[int | None, float]:
    best = (None, -1.0)
    for sid, sim in cands:
        if sim >= threshold and sim > best[1]:
            best = (sid, sim)
    return best


def verify(conn: sqlite3.Connection, title: str, text: str, cands: list[tuple[int, float]],
           threshold: float, log_ref: Any = None,
           accept: frozenset[str] = harness.MERGE_CLASSES) -> tuple[int | None, float]:
    """LLM-verify the best few candidates; a verifier failure rejects them all."""
    rows = []
    for sid, sim in sorted(cands, key=lambda c: c[1], reverse=True)[: config.CLUSTER_VERIFY_MAX_CANDIDATES]:
        if sim < threshold:
            break
        s = conn.execute("SELECT headline, summary FROM Story WHERE id = ?", (sid,)).fetchone()
        if s is not None:
            rows.append({"id": sid, "headline": s["headline"] or "", "summary": s["summary"] or "", "sim": sim})
    if not rows:
        return None, -1.0
    try:
        decisions = harness.classify_event_candidates(title, text, rows)
    except Exception:  # noqa: BLE001
        log.exception("event verification failed for %s", log_ref)
        return None, -1.0
    for c in rows:
        cls = decisions.get(c["id"], "UNRELATED")
        log.info("verify %s story=%s sim=%.3f class=%s", log_ref, c["id"], c["sim"], cls)
        if cls in accept:
            return c["id"], c["sim"]
    return None, -1.0


def _pick(conn, article, cands, threshold_two_stage, threshold_cosine):
    if config.CLUSTER_MODE == "two_stage" and article is not None:
        text = article.get("content_raw") or article.get("title") or ""
        return verify(conn, article.get("title") or "", text, cands, threshold_two_stage,
                      f"article={article.get('id')}")
    return _best_clearing(cands, threshold_cosine)


def decide_story(conn: sqlite3.Connection, vec: Vector,
                 article: dict[str, Any] | None = None) -> tuple[int, bool, bool]:
    """Return ``(story_id, created, revived)``; creates a placeholder story when nothing matches.

    Without ``article`` (maintenance scripts) the cosine path is used.
    """
    anchors = AnchorCache(conn)
    cutoff = window_cutoff()
    sid, _ = _pick(conn, article, nearest_stories(conn, vec, anchors, since=cutoff),
                   config.CLUSTER_VERIFY_RETRIEVE_THRESHOLD, config.CLUSTER_SIM_THRESHOLD)
    if sid is not None:
        return sid, False, False

    # Fresh stories were already judged in tier 1; re-asking only repeats the verdict.
    sid, sim = _pick(conn, article, nearest_stories(conn, vec, anchors, before=cutoff),
                     config.CLUSTER_REVIVE_SIM_THRESHOLD, config.CLUSTER_REVIVE_SIM_THRESHOLD)
    if sid is not None:
        log.info("reviving story %d (anchor sim=%.3f)", sid, sim)
        return sid, False, True

    cur = conn.execute(
        "INSERT INTO Story (headline, summary, tags, metrics, quality_score) "
        "VALUES (NULL, NULL, '[]', '{}', 5.0)"
    )
    return cur.lastrowid, True, False
