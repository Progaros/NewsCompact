"""Explicit-only multi-vector semantic profile for "For you" ranking.

Builds a small set of unit-length interest vectors from explicit user signals only
(never viewing/dwell behavior):
1. Weighted tags on the user's tag list (cached in ``TagVector``).
2. Story vectors of the user's recent 👍/👎 votes (time-decayed, excluding self-match).
3. ``preferences.about`` (cached in memory per user + text hash).

Each candidate story's vector (``diversity.story_vectors``) is matched against these
anchors using rescaled cosine similarity above ``SEMANTIC_SIM_FLOOR`` and combined per
sign by noisy-OR, matching ``scoring.tag_affinity``. If the embedding server is offline,
missing tag/about vectors are skipped silently without breaking the feed.
"""
from __future__ import annotations

from collections import OrderedDict
import datetime as dt
import hashlib
import logging
import math
import sqlite3
import threading
import time
from typing import Any

from . import config, db, diversity, ollama
from .clustering import dot, normalize

log = logging.getLogger("news.semantic-profile")

_TAG_INSTRUCT = "Represent this news topic tag for semantic matching: "
_EMBED_TIMEOUT = 2.0
_EMBED_FAIL_BACKOFF_S = 60.0
_SCORE_CACHE_MAX = 8000

_about_lock = threading.Lock()
_about_cache: dict[tuple[int, str], list[float]] = {}
_embed_fail_until = 0.0
_score_lock = threading.Lock()
_score_cache: OrderedDict[tuple[str, str, int, int], tuple[float, dict[str, Any] | None] | None] = OrderedDict()


def clear_cache() -> None:
    global _embed_fail_until
    with _about_lock:
        _about_cache.clear()
        _embed_fail_until = 0.0
    with _score_lock:
        _score_cache.clear()


def _fade(age_days: float, half_life_days: float) -> float:
    return 0.5 ** (max(0.0, age_days) / max(0.5, half_life_days))


def _load_tag_vectors(conn: sqlite3.Connection, tags: list[str]) -> dict[str, list[float]]:
    if not tags:
        return {}
    ph = ",".join("?" * len(tags))
    have = {
        t: normalize(db.unpack_floats(bytes(blob)))
        for t, blob in conn.execute(f"SELECT tag, embedding FROM TagVector WHERE tag IN ({ph})", tags)
    }
    missing = [t for t in tags if t not in have]
    if missing and time.monotonic() >= _embed_fail_until:
        try:
            vecs = ollama.embed_texts([_TAG_INSTRUCT + t for t in missing], timeout=_EMBED_TIMEOUT)
            rows = []
            for t, v in zip(missing, vecs):
                nv = normalize(v)
                have[t] = nv
                rows.append((t, db.pack_floats(nv)))
            if rows:
                conn.executemany("INSERT OR REPLACE INTO TagVector (tag, embedding) VALUES (?, ?)", rows)
        except Exception as e:  # noqa: BLE001
            _set_embed_backoff()
            log.debug("semantic profile: skipping %d unembedded tag(s): %s", len(missing), e)
    return have


def _set_embed_backoff() -> None:
    global _embed_fail_until
    with _about_lock:
        _embed_fail_until = time.monotonic() + _EMBED_FAIL_BACKOFF_S


def _about_vector(user_id: int, about: str) -> list[float] | None:
    text = (about or "").strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    key = (user_id, digest)
    with _about_lock:
        hit = _about_cache.get(key)
        if hit is not None:
            return hit
        if time.monotonic() < _embed_fail_until:
            return None
    try:
        vec = normalize(ollama.embed_texts([text], timeout=_EMBED_TIMEOUT)[0])
    except Exception as e:  # noqa: BLE001
        _set_embed_backoff()
        log.debug("semantic profile: skipping about embedding for user %s: %s", user_id, e)
        return None
    with _about_lock:
        # Drop any older hash for this user.
        for k in [k for k in _about_cache if k[0] == user_id]:
            _about_cache.pop(k, None)
        _about_cache[key] = vec
    return vec


def build_anchors(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    user_id: int,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    """Build explicit interest anchors ``[{vec, weight, label, source, story_id?}]``."""
    anchors: list[dict[str, Any]] = []

    # 1. Explicit tag list (skip muted tags — those are handled by the hard mute filter).
    raw_weights = {
        t: float(w)
        for t, w in (profile.get("tag_weights") or {}).items()
        if w and float(w) > config.TAG_MUTE_WEIGHT
    }
    if raw_weights:
        top_tags = sorted(raw_weights, key=lambda t: -abs(raw_weights[t]))[:30]
        tvecs = _load_tag_vectors(conn, top_tags)
        for t in top_tags:
            v = tvecs.get(t)
            if v is not None:
                anchors.append({
                    "vec": v,
                    "weight": math.tanh(raw_weights[t] / config.TAG_WEIGHT_SCALE),
                    "label": f"#{t}",
                    "source": "tag",
                    "story_id": None,
                })

    # 2. Recent 👍/👎 story vectors (time-decayed).
    vote_rows = conn.execute(
        "SELECT story_id, headline, kind, created_at FROM FeedbackLog "
        "WHERE user_id = ? AND story_id IS NOT NULL AND kind IN ('up', 'down') "
        "ORDER BY id DESC LIMIT ?",
        (user_id, config.SEMANTIC_MAX_VOTES),
    ).fetchall()
    if vote_rows:
        sids = list({int(r["story_id"]) for r in vote_rows})
        svecs = diversity.story_vectors(conn, sids)
        hl_up = config.VOTE_HALF_LIFE_DAYS.get("up", 60.0)
        hl_down = config.VOTE_HALF_LIFE_DAYS.get("down", 90.0)
        for r in vote_rows:
            sid = int(r["story_id"])
            v = svecs.get(sid)
            if v is None:
                continue
            ts = db.parse_ts(r["created_at"])
            age_d = max(0.0, (now - ts).total_seconds() / 86400.0) if ts else 0.0
            if r["kind"] == "up":
                w = 0.85 * _fade(age_d, hl_up)
            else:
                w = -0.70 * _fade(age_d, hl_down)
            if abs(w) >= 0.05:
                anchors.append({
                    "vec": v,
                    "weight": w,
                    "label": (r["headline"] or f"story #{sid}")[:72],
                    "source": "vote",
                    "story_id": sid,
                })

    # 3. Free-text "About you" description.
    about_vec = _about_vector(user_id, (profile.get("preferences") or {}).get("about") or "")
    if about_vec is not None:
        anchors.append({
            "vec": about_vec,
            "weight": 0.55,
            "label": "about you",
            "source": "about",
            "story_id": None,
        })

    return anchors


def _anchors_fingerprint(user_id: int, anchors: list[dict[str, Any]], floor: float) -> str:
    """Stable short key for a user's current anchor set (rounded weights so hour-level decay hits cache)."""
    parts = [f"{user_id}:{floor:.3f}"]
    for a in anchors:
        parts.append(f"{a['source']}:{a['label']}:{a['story_id']}:{a['weight']:.2f}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def score_stories(
    conn: sqlite3.Connection,
    story_ids: list[int],
    profile: dict[str, Any],
    user_id: int,
    now: dt.datetime,
    *,
    vectors: dict[int, list[float]] | None = None,
) -> dict[int, tuple[float, dict[str, Any] | None]]:
    """Return ``{story_id: (affinity -1..1, best_match)}`` for candidate stories."""
    if not story_ids or config.W_SEMANTIC <= 0:
        return {}
    anchors = build_anchors(conn, profile, user_id, now)
    if not anchors:
        return {}
    svecs = vectors if vectors is not None else diversity.story_vectors(conn, story_ids)
    if not svecs:
        return {}

    floor = max(0.0, min(0.95, config.SEMANTIC_SIM_FLOOR))
    span = 1.0 - floor
    db_key = str(config.DB_PATH)
    afp = _anchors_fingerprint(user_id, anchors, floor)
    out: dict[int, tuple[float, dict[str, Any] | None]] = {}
    to_compute: list[tuple[int, list[float], tuple[str, str, int, int]]] = []

    with _score_lock:
        for sid, sv in svecs.items():
            ckey = (db_key, afp, sid, id(sv))
            if ckey in _score_cache:
                _score_cache.move_to_end(ckey)
                cached = _score_cache[ckey]
                if cached is not None:
                    out[sid] = cached
            else:
                to_compute.append((sid, sv, ckey))

    computed: list[tuple[tuple[str, str, int, int], tuple[float, dict[str, Any] | None] | None]] = []
    for sid, sv, ckey in to_compute:
        pos_prod = 1.0
        neg_prod = 1.0
        best: dict[str, Any] | None = None
        for a in anchors:
            if a["story_id"] == sid:
                continue  # never self-match a story to its own vote vector
            cos = dot(sv, a["vec"])
            if cos <= floor:
                continue
            sim = min(1.0, (cos - floor) / span)
            sig = sim * a["weight"]
            if sig > 0:
                pos_prod *= 1.0 - min(1.0, sig)
            elif sig < 0:
                neg_prod *= 1.0 - min(1.0, -sig)
            if best is None or abs(sig) > abs(best["signal"]):
                best = {
                    "label": a["label"],
                    "source": a["source"],
                    "sim": round(cos, 2),
                    "signal": round(sig, 2),
                }
        affinity = max(-1.0, min(1.0, (1.0 - pos_prod) - (1.0 - neg_prod)))
        val = (affinity, best) if abs(affinity) >= 0.01 else None
        if val is not None:
            out[sid] = val
        computed.append((ckey, val))

    if computed:
        with _score_lock:
            for ckey, val in computed:
                _score_cache[ckey] = val
                _score_cache.move_to_end(ckey)
            while len(_score_cache) > _SCORE_CACHE_MAX:
                _score_cache.popitem(last=False)
    return out
