"""List-level rerank: MMR over the top of a ranked feed, so one topic can't fill the first screen.

    pick next = argmax  λ · score/top_score - (1 - λ) · max similarity to the stories placed above

Similarity of two stories = max(rescaled cosine of their story vectors, StoryLink relation),
where a story vector is the normalized, content-class-weighted mean of its newest original
article vectors. Only the first DIVERSITY_POOL stories move, and only among themselves.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import config, db
from .clustering import dot, normalize

_PER_STORY = 8
# How much an article's text says about the story (title-only / broken bodies say little).
_SUFFICIENCY = {"ok": 1.0, "thin": 0.55, "broken": 0.1, "empty": 0.0}


def story_vectors(conn: sqlite3.Connection, story_ids: list[int]) -> dict[int, list[float]]:
    members: dict[int, list[tuple[int, float]]] = {}
    for aid, sid, cls in conn.execute(
        "SELECT id, story_id, content_class FROM Article WHERE story_id IN (SELECT value FROM json_each(?)) "
        "AND duplicate_of IS NULL ORDER BY created_at DESC", (json.dumps(story_ids),)
    ):
        w = _SUFFICIENCY.get(cls or "", 0.5)
        picked = members.setdefault(sid, [])
        if w > 0 and len(picked) < _PER_STORY:
            picked.append((aid, w))
    out = {}
    for sid, picked in members.items():
        acc: list[float] | None = None
        for aid, w in picked:
            row = conn.execute(f"SELECT embedding FROM {db.VECTOR_TABLE} WHERE article_id = ?", (aid,)).fetchone()
            if row is None:
                continue
            v = normalize(db.unpack_floats(bytes(row[0])))
            acc = [w * x for x in v] if acc is None else [a + w * x for a, x in zip(acc, v)]
        if acc is not None:
            out[sid] = normalize(acc)
    return out


def link_similarity(conn: sqlite3.Connection, story_ids: list[int]) -> dict[tuple[int, int], float]:
    """``{(a, b): similarity}`` (a < b) from verifier links between the given stories."""
    ids = json.dumps(story_ids)
    out = {}
    for a, b, relation in conn.execute(
        "SELECT a, b, relation FROM StoryLink WHERE a IN (SELECT value FROM json_each(?)) "
        "AND b IN (SELECT value FROM json_each(?))", (ids, ids)
    ):
        sim = config.DIVERSITY_LINK_SIM.get(relation, 0.0)
        if sim > out.get((a, b), 0.0):
            out[(a, b)] = sim
    return out


def rerank(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MMR over ``items[:DIVERSITY_POOL]`` (sorted by ``display``); a story pushed down for
    resembling one above gets ``relevance.similar_to = {id, headline, sim}``."""
    lam = config.DIVERSITY_LAMBDA
    pool, rest = items[:config.DIVERSITY_POOL], items[config.DIVERSITY_POOL:]
    if len(pool) < 3 or lam >= 1:
        return items
    top = max(i["display"] for i in pool) or 1.0
    ids = [i["story"]["id"] for i in pool]
    vectors = story_vectors(conn, ids)
    links = link_similarity(conn, ids)
    floor = config.DIVERSITY_SIM_FLOOR

    def similarity(a: int, b: int) -> float:
        sim = links.get((min(a, b), max(a, b)), 0.0)
        va, vb = vectors.get(a), vectors.get(b)
        if va is not None and vb is not None and floor < 1:
            sim = max(sim, (dot(va, vb) - floor) / (1 - floor))
        return sim

    closest: dict[int, tuple[float, dict[str, Any] | None]] = {sid: (0.0, None) for sid in ids}
    rank = {sid: n for n, sid in enumerate(ids)}
    remaining, placed = list(pool), []
    while remaining:
        best = max(remaining, key=lambda i: lam * i["display"] / top - (1 - lam) * closest[i["story"]["id"]][0])
        remaining.remove(best)
        sim, above = closest[best["story"]["id"]]
        if above is not None and len(placed) > rank[best["story"]["id"]]:
            best["relevance"]["similar_to"] = {"id": above["story"]["id"], "headline": above["story"]["headline"],
                                               "sim": round(sim, 2)}
        placed.append(best)
        for other in remaining:
            oid = other["story"]["id"]
            s = similarity(best["story"]["id"], oid)
            if s > closest[oid][0]:
                closest[oid] = (s, best)
    return placed + rest
