"""List-level rerank: MMR over the top of a ranked feed, so one topic or outlet can't fill the first screen.

    pick next = argmax  λ · (score · outlet_decay^n)/top_score - (1 - λ) · max similarity above

Similarity of two stories = max(rescaled cosine of their story vectors, StoryLink relation),
where a story vector is the normalized, content-class-weighted mean of its newest original
article vectors. Single-outlet stories also decay by DIVERSITY_OUTLET_DECAY for each earlier
single-outlet story from the same outlet (multi-outlet stories are not penalized).
Only the first DIVERSITY_POOL stories move, and only among themselves.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import sqlite3
import threading
from typing import Any

from . import config, db
from .clustering import dot, normalize

_PER_STORY = 8
# How much an article's text says about the story (title-only / broken bodies say little).
_SUFFICIENCY = {"ok": 1.0, "thin": 0.55, "broken": 0.1, "empty": 0.0}
_VEC_CACHE_MAX = 2000
_vec_cache: OrderedDict[tuple[str, int, tuple[tuple[int, float], ...]], list[float]] = OrderedDict()
_vec_lock = threading.Lock()


def story_vectors(conn: sqlite3.Connection, story_ids: list[int]) -> dict[int, list[float]]:
    members: dict[int, list[tuple[int, float]]] = {}
    for aid, sid, cls in conn.execute(
        "SELECT id, story_id, content_class FROM Article WHERE story_id IN (SELECT value FROM json_each(?)) "
        "AND duplicate_of IS NULL ORDER BY story_id, created_at DESC", (json.dumps(story_ids),)
    ):
        w = _SUFFICIENCY.get(cls or "", 0.5)
        picked = members.setdefault(sid, [])
        if w > 0 and len(picked) < _PER_STORY:
            picked.append((aid, w))
    db_key = str(config.DB_PATH)
    out: dict[int, list[float]] = {}
    missing: dict[int, tuple[tuple[str, int, tuple[tuple[int, float], ...]], list[tuple[int, float]]]] = {}
    with _vec_lock:
        for sid, picked in members.items():
            ckey = (db_key, sid, tuple(picked))
            hit = _vec_cache.get(ckey)
            if hit is not None:
                _vec_cache.move_to_end(ckey)
                out[sid] = hit
            else:
                missing[sid] = (ckey, picked)
    if missing:
        need_ids = list({aid for _, picked in missing.values() for aid, _ in picked})
        raw_vecs = {
            aid: normalize(db.unpack_floats(bytes(blob)))
            for aid, blob in conn.execute(
                f"SELECT article_id, embedding FROM {db.VECTOR_TABLE} "
                "WHERE article_id IN (SELECT value FROM json_each(?))",
                (json.dumps(need_ids),),
            )
        }
        computed: dict[tuple[str, int, tuple[tuple[int, float], ...]], list[float]] = {}
        for sid, (ckey, picked) in missing.items():
            acc: list[float] | None = None
            for aid, w in picked:
                v = raw_vecs.get(aid)
                if v is None:
                    continue
                acc = [w * x for x in v] if acc is None else [a + w * x for a, x in zip(acc, v)]
            if acc is not None:
                vec = normalize(acc)
                out[sid] = vec
                computed[ckey] = vec
        if computed:
            with _vec_lock:
                for ckey, vec in computed.items():
                    _vec_cache[ckey] = vec
                    _vec_cache.move_to_end(ckey)
                while len(_vec_cache) > _VEC_CACHE_MAX:
                    _vec_cache.popitem(last=False)
    return out


def single_outlets(conn: sqlite3.Connection, story_ids: list[int]) -> dict[int, str]:
    """``{story_id: outlet_key}`` for stories covered by exactly one distinct outlet."""
    from .scoring import outlet_key

    outlet_of = {fid: outlet_key(site, xml) or f"feed:{fid}"
                 for fid, site, xml in conn.execute("SELECT id, site_url, xml_url FROM Feed")}
    seen: dict[int, set[str]] = {}
    for sid, fid in conn.execute(
        "SELECT DISTINCT story_id, feed_id FROM Article "
        "WHERE story_id IN (SELECT value FROM json_each(?))",
        (json.dumps(story_ids),),
    ):
        seen.setdefault(sid, set()).add(outlet_of.get(fid) or f"feed:{fid}")
    return {sid: next(iter(outs)) for sid, outs in seen.items() if len(outs) == 1}


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
    outlet_decay = config.DIVERSITY_OUTLET_DECAY
    use_outlet_decay = 0.0 < outlet_decay < 1.0
    pool, rest = items[:config.DIVERSITY_POOL], items[config.DIVERSITY_POOL:]
    if len(pool) < 3 or (lam >= 1 and not use_outlet_decay):
        return items
    top = max(i["display"] for i in pool) or 1.0
    ids = [i["story"]["id"] for i in pool]
    vectors = story_vectors(conn, ids) if lam < 1 else {}
    links = link_similarity(conn, ids) if lam < 1 else {}
    singles = single_outlets(conn, ids) if use_outlet_decay else {}
    floor = config.DIVERSITY_SIM_FLOOR

    def similarity(a: int, b: int) -> float:
        sim = links.get((min(a, b), max(a, b)), 0.0)
        va, vb = vectors.get(a), vectors.get(b)
        if va is not None and vb is not None and floor < 1:
            sim = max(sim, (dot(va, vb) - floor) / (1 - floor))
        return sim

    closest: dict[int, tuple[float, dict[str, Any] | None]] = {sid: (0.0, None) for sid in ids}
    outlet_counts: dict[str, int] = {}
    rank = {sid: n for n, sid in enumerate(ids)}
    remaining, placed = list(pool), []
    while remaining:
        def mmr_key(i: dict[str, Any]) -> float:
            sid = i["story"]["id"]
            outlet = singles.get(sid)
            eff = i["display"] * (outlet_decay ** outlet_counts.get(outlet, 0)) if outlet else i["display"]
            return lam * eff / top - (1 - lam) * closest[sid][0]

        best = max(remaining, key=mmr_key)
        remaining.remove(best)
        bsid = best["story"]["id"]
        sim, above = closest[bsid]
        if above is not None and len(placed) > rank[bsid]:
            best["relevance"]["similar_to"] = {"id": above["story"]["id"], "headline": above["story"]["headline"],
                                               "sim": round(sim, 2)}
        placed.append(best)
        if bsid in singles:
            o = singles[bsid]
            outlet_counts[o] = outlet_counts.get(o, 0) + 1
        if lam < 1:
            for other in remaining:
                oid = other["story"]["id"]
                s = similarity(bsid, oid)
                if s > closest[oid][0]:
                    closest[oid] = (s, best)
    return placed + rest
