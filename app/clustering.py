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
import json
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


def ensure_embedding(conn: sqlite3.Connection, article: dict[str, Any], vec: Vector | None = None) -> Vector:
    """Return the stored vector, storing ``vec`` (or embedding it) once if missing."""
    stored = get_vector(conn, article["id"])
    if stored is not None:
        return stored
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
    """Normalized anchor vectors per story (non-broken members, else all members).

    Membership is re-read on every call (one indexed query), only the per-article
    vectors are cached, so one cache can serve a whole batch while stories change.
    Set ``conn`` before use on a new connection.
    """

    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn
        self._vecs: dict[int, Vector] = {}

    def __call__(self, story_id: int) -> list[Vector]:
        rows = self.conn.execute(
            "SELECT id, COALESCE(content_class, '') = 'broken' FROM Article WHERE story_id = ?",
            (story_id,),
        ).fetchall()
        ids = [r[0] for r in rows if not r[1]] or [r[0] for r in rows]
        out = []
        for aid in ids:
            if aid not in self._vecs:
                v = get_vector(self.conn, aid)
                if v is None:
                    continue
                self._vecs[aid] = normalize(v)
            out.append(self._vecs[aid])
        return out


def window_cutoff(hours: float | None = None) -> str:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=config.CLUSTER_WINDOW_HOURS if hours is None else hours)
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
RELATED_CLASSES = ("SAME_TOPIC_DISTINCT_EVENT", "CONTINUING_DEVELOPMENT")


def link_stories(conn: sqlite3.Connection, x: int, y: int, relation: str, source: str,
                 checked_at: str | None = None) -> None:
    """Remember a verifier verdict between two stories (StoryLink; latest verdict wins)."""
    if x == y:
        return
    a, b = sorted((x, y))
    conn.execute(
        "INSERT INTO StoryLink (a, b, relation, source, checked_at) VALUES (?, ?, ?, ?, COALESCE(?, datetime('now'))) "
        "ON CONFLICT (a, b) DO UPDATE SET relation = excluded.relation, source = excluded.source, "
        "checked_at = excluded.checked_at",
        (a, b, relation, source, checked_at),
    )


def merge_settled(conn: sqlite3.Connection, x: int, y: int) -> bool:
    """True if a story-vs-story check of x and y happened at/after both last changed."""
    a, b = sorted((x, y))
    return conn.execute(
        "SELECT 1 FROM StoryLink l JOIN Story sa ON sa.id = l.a JOIN Story sb ON sb.id = l.b "
        "WHERE l.a = ? AND l.b = ? AND l.source = 'merge' "
        "AND sa.updated_at <= l.checked_at AND sb.updated_at <= l.checked_at", (a, b),
    ).fetchone() is not None


def related_stories(conn: sqlite3.Connection, story_ids: list[int], limit: int = 5,
                    exclude: set[int] = frozenset()) -> dict[int, list[dict]]:
    """``{story_id: [{id, headline}]}``: linked same-topic stories, most recent news first.

    When ``exclude`` (hidden feed ids) is non-empty, related stories whose articles all
    come from excluded feeds are omitted.
    """
    out: dict[int, list[dict]] = {sid: [] for sid in story_ids}
    if not story_ids:
        return out
    ids, rels = json.dumps(story_ids), json.dumps(RELATED_CLASSES)
    vis = (" AND EXISTS (SELECT 1 FROM Article a WHERE a.story_id = s.id "
           "AND a.feed_id NOT IN (SELECT value FROM json_each(?)))") if exclude else ""
    vis_arg = (json.dumps(sorted(exclude)),) if exclude else ()
    rows = conn.execute(
        "SELECT l.a AS sid, s.id, s.headline, s.news_at FROM StoryLink l JOIN Story s ON s.id = l.b "
        "WHERE l.a IN (SELECT value FROM json_each(?)) AND l.relation IN (SELECT value FROM json_each(?)) "
        f"AND s.headline IS NOT NULL{vis} "
        "UNION ALL "
        "SELECT l.b, s.id, s.headline, s.news_at FROM StoryLink l JOIN Story s ON s.id = l.a "
        "WHERE l.b IN (SELECT value FROM json_each(?)) AND l.relation IN (SELECT value FROM json_each(?)) "
        f"AND s.headline IS NOT NULL{vis} ORDER BY 4 DESC",
        (ids, rels, *vis_arg, ids, rels, *vis_arg),
    )
    for r in rows:
        if len(out[r[0]]) < limit:
            out[r[0]].append({"id": r[1], "headline": r[2]})
    return out


def _best_clearing(cands: list[tuple[int, float]], threshold: float) -> tuple[int | None, float]:
    best = (None, -1.0)
    for sid, sim in cands:
        if sim >= threshold and sim > best[1]:
            best = (sid, sim)
    return best


def verify(conn: sqlite3.Connection, title: str, text: str, cands: list[tuple[int, float]],
           threshold: float, log_ref: Any = None,
           accept: frozenset[str] = harness.MERGE_CLASSES,
           priority: frozenset[int] = frozenset(),
           verdicts: dict[int, str] | None = None) -> tuple[int | None, float]:
    """LLM-verify the best few candidates (``priority`` ids first); a verifier failure rejects them all.

    ``verdicts`` (if given) receives ``{story_id: class}`` for every candidate the verifier judged.
    """
    rows = []
    ranked = sorted(cands, key=lambda c: (c[0] in priority, c[1]), reverse=True)
    for sid, sim in ranked:
        if len(rows) >= config.CLUSTER_VERIFY_MAX_CANDIDATES:
            break
        if sim < threshold and sid not in priority:
            continue
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
    if verdicts is not None:
        verdicts.update({c["id"]: decisions[c["id"]] for c in rows if c["id"] in decisions})
    for c in rows:
        cls = decisions.get(c["id"], "UNRELATED")
        log.info("verify %s story=%s sim=%.3f class=%s", log_ref, c["id"], c["sim"], cls)
        if cls in accept:
            return c["id"], c["sim"]
    return None, -1.0


def _pick(conn, article, cands, threshold_two_stage, threshold_cosine, verdicts=None):
    if config.CLUSTER_MODE == "two_stage" and article is not None:
        text = article.get("content_raw") or article.get("title") or ""
        return verify(conn, article.get("title") or "", text, cands, threshold_two_stage,
                      f"article={article.get('id')}", verdicts=verdicts)
    return _best_clearing(cands, threshold_cosine)


def decide_story(conn: sqlite3.Connection, vec: Vector,
                 article: dict[str, Any] | None = None,
                 anchors: AnchorCache | None = None,
                 verdicts: dict[int, str] | None = None) -> tuple[int, bool, bool]:
    """Return ``(story_id, created, revived)``; creates a placeholder story when nothing matches.

    Without ``article`` (maintenance scripts) the cosine path is used. ``verdicts`` collects
    the verifier's classes per candidate story (see ``verify``).
    """
    if anchors is None:
        anchors = AnchorCache()
    anchors.conn = conn
    cutoff = window_cutoff()
    sid, _ = _pick(conn, article, nearest_stories(conn, vec, anchors, since=cutoff),
                   config.CLUSTER_VERIFY_RETRIEVE_THRESHOLD, config.CLUSTER_SIM_THRESHOLD, verdicts)
    if sid is not None:
        return sid, False, False

    # Fresh stories were already judged in tier 1; re-asking only repeats the verdict.
    sid, sim = _pick(conn, article, nearest_stories(conn, vec, anchors, before=cutoff),
                     config.CLUSTER_REVIVE_SIM_THRESHOLD, config.CLUSTER_REVIVE_SIM_THRESHOLD, verdicts)
    if sid is not None:
        log.info("reviving story %d (anchor sim=%.3f)", sid, sim)
        return sid, False, True

    cur = conn.execute(
        "INSERT INTO Story (headline, summary, tags, metrics, quality_score) "
        "VALUES (NULL, NULL, '[]', '{}', 5.0)"
    )
    return cur.lastrowid, True, False
