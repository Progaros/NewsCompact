"""Ranking (relevance + display score) and the 👍/👎 feedback loop.

    relevance = 10·(quality·WQ + credibility·WC + user_impact·WI)        (0..10)
    display   = relevance + urgency·W1 + clamp(interest·W3, ±INTEREST_CAP)
                + distance·W_DISTANCE − time_decay

- quality      trust-weighted median of member article qualities, 0..1 (low-trust
               reposts can't water down a credible source)
- credibility  corroboration: summed trust of the distinct outlets (site host; a feed's
               trust = its category weight 0..1, SOURCE_TRUST / profile source_weights;
               several feeds of one outlet count once): 1.0→0.25 … 5.0→1.0
- user_impact  0.5 baseline, +0.25 per preferences.interest_regions/locations hit in tags
- interest     sum of the user's tag weights; clamped because weights never decay
- distance     story location/language vs USER_CITY/COUNTRY/LANGUAGE: 1.0/0.6/0.35/0
- time_decay   TIME_DECAY_MAX·(1 − e^(−age_h/half_life)) on Story.news_at (last update with new facts)
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import statistics
from typing import Any
from urllib.parse import urlsplit

from . import config, db
from .tags import normalize_tags

_DEFAULT_PREFS = {"min_quality": 0.0, "summary_length": "medium"}
# The "default" view (everyone, signed out): no learned interests, preferences or trust overrides.
DEFAULT_PROFILE: dict[str, Any] = {"id": None, "tag_weights": {}, "source_weights": {}, "feed_weights": {},
                                   "preferences": {}}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def story_tags(story: sqlite3.Row) -> list[str]:
    return normalize_tags([t for t in db.json_list(story["tags"]) if isinstance(t, str)])


def time_decay_penalty(updated_at: str | None) -> float:
    ts = db.parse_ts(updated_at)
    if ts is None:
        return 0.0
    age_h = max(0.0, (dt.datetime.now(dt.UTC).replace(tzinfo=None) - ts).total_seconds() / 3600)
    return config.TIME_DECAY_MAX * (1.0 - math.exp(-age_h / config.TIME_DECAY_HALF_LIFE_HOURS))


def credibility_score(trust: float) -> float:
    """``trust`` = summed trust of the distinct feeds (= feed count when all are fully trusted)."""
    return _clamp01(0.05 + 0.20 * max(0.0, trust))


def trust_weights(conn: sqlite3.Connection, profile: dict[str, Any]) -> dict[str, float]:
    """Effective trust per feed category ('' = uncategorized): profile > SOURCE_TRUST > default."""
    out = {}
    for (cat,) in conn.execute("SELECT DISTINCT COALESCE(category, '') FROM Feed"):
        w = profile["source_weights"].get(cat, config.SOURCE_TRUST.get(cat, config.SOURCE_TRUST_DEFAULT))
        try:
            out[cat] = _clamp01(float(w))
        except (TypeError, ValueError):
            out[cat] = config.SOURCE_TRUST_DEFAULT
    return out


def feed_trust(conn: sqlite3.Connection, profile: dict[str, Any]) -> dict[int, float]:
    """Effective trust per feed id: the profile's per-feed override, else its category's trust."""
    cats = trust_weights(conn, profile)
    overrides = profile.get("feed_weights") or {}
    out = {}
    for fid, cat in conn.execute("SELECT id, COALESCE(category, '') FROM Feed"):
        try:
            out[fid] = _clamp01(float(overrides[str(fid)])) if str(fid) in overrides else cats[cat]
        except (TypeError, ValueError):
            out[fid] = cats[cat]
    return out


def outlet_key(site_url: str | None, xml_url: str | None) -> str:
    """The publisher behind a feed: site host without ``www.`` (several feeds can share one)."""
    for url in (site_url, xml_url):
        host = (urlsplit(url or "").hostname or "").lower()
        if host:
            return host.removeprefix("www.")
    return ""


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """Median of ``(value, weight)``; equal weights give the plain median, zero weights are ignored."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in pairs)
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if acc > total / 2 + 1e-9:
            return v
        if abs(acc - total / 2) <= 1e-9:
            return (v + pairs[i + 1][0]) / 2
    return None


def excluded_feeds(profile: dict[str, Any]) -> set[int]:
    """Feed ids the user hid from their personal feed (preferences.excluded_feeds)."""
    out = set()
    for v in profile["preferences"].get("excluded_feeds") or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            pass
    return out


def source_stats(conn: sqlite3.Connection, trust: dict[int, float],
                 story_id: int | None = None, exclude: set[int] = frozenset()) -> dict[int, dict[str, Any]]:
    """``{story_id: {feeds, trust, quality}}`` per story; ``trust`` = ``feed_trust()``.

    ``trust`` sums the best feed trust of each distinct outlet; ``quality`` is the
    trust-weighted median article quality (plain median if all trust is 0, None without scores).
    Articles of ``exclude`` feeds don't count (a story with only those is left out).
    """
    sql = ("SELECT a.story_id, a.feed_id, a.quality, f.site_url, f.xml_url "
           "FROM Article a JOIN Feed f ON f.id = a.feed_id WHERE a.story_id IS NOT NULL")
    rows = conn.execute(sql + " AND a.story_id = ?", (story_id,)) if story_id is not None else conn.execute(sql)
    feeds: dict[int, set[int]] = {}
    outlets: dict[int, dict[str, float]] = {}
    quals: dict[int, list[tuple[float, float]]] = {}
    for r in rows:
        if r["feed_id"] in exclude:
            continue
        sid, w = r["story_id"], trust.get(r["feed_id"], config.SOURCE_TRUST_DEFAULT)
        feeds.setdefault(sid, set()).add(r["feed_id"])
        key = outlet_key(r["site_url"], r["xml_url"]) or f"feed:{r['feed_id']}"
        best = outlets.setdefault(sid, {})
        best[key] = max(best.get(key, 0.0), w)
        if r["quality"] is not None:
            quals.setdefault(sid, []).append((float(r["quality"]), w))
    out = {}
    for sid, fids in feeds.items():
        q = quals.get(sid, [])
        quality = weighted_median(q)
        if quality is None and q:
            quality = float(statistics.median(v for v, _ in q))
        out[sid] = {"feeds": len(fids), "trust": sum(outlets[sid].values()), "quality": quality}
    return out


def _mentions(value: str, tag: str) -> bool:
    """``value`` appears in ``tag`` as whole words ("us" matches "us-politics", not "music")."""
    return re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", tag) is not None


def user_impact_score(tags: list[str], preferences: dict[str, Any]) -> float:
    impact = 0.5
    for key in ("interest_regions", "locations"):
        values = preferences.get(key)
        if not isinstance(values, list):
            continue
        for v in values:
            v = str(v).lower().strip()
            if v and any(_mentions(v, t) for t in tags):
                impact += 0.25
    return _clamp01(impact)


def distance_score(metrics: dict[str, Any]) -> float:
    location = str(metrics.get("location") or "").lower()
    language = str(metrics.get("language") or "").lower()
    city, country = config.USER_CITY.lower().strip(), config.USER_COUNTRY.lower().strip()
    score = 0.0
    if location:
        if city and city in location:
            score = 1.0
        elif country and country in location:
            score = 0.6
    lang = config.USER_LANGUAGE.lower().strip()
    if lang and language == lang:
        score = max(score, 0.35)
    return score


def score_story(story: sqlite3.Row, source_count: int, profile: dict[str, Any],
                trust: float | None = None, quality: float | None = None,
                personal: bool = True) -> tuple[dict[str, float], float]:
    """Return ``(relevance breakdown, display score)`` for one story.

    ``trust`` = summed outlet trust and ``quality`` = trust-weighted article quality
    (see ``source_stats``); they default to ``source_count`` and the stored story quality.
    ``personal=False`` (default view) leaves out the distance to the user's location.
    """
    tags = story_tags(story)
    metrics = db.json_dict(story["metrics"])
    try:
        quality = _clamp01(float(quality if quality is not None else story["quality_score"] or 5.0) / 10.0)
    except (TypeError, ValueError):
        quality = 0.5
    trust = float(source_count) if trust is None else trust
    credibility = credibility_score(trust)
    impact = user_impact_score(tags, profile["preferences"])
    base = 10.0 * (
        config.W_RELEVANCE_QUALITY * quality
        + config.W_RELEVANCE_CREDIBILITY * credibility
        + config.W_RELEVANCE_IMPACT * impact
    )
    interest = sum(profile["tag_weights"].get(t, 0.0) for t in tags)
    interest_pts = max(-config.INTEREST_CAP, min(config.INTEREST_CAP, interest * config.W3_INTEREST))
    distance = distance_score(metrics) if personal else 0.0
    try:
        urgency = float(metrics.get("urgency_level", 5))
    except (TypeError, ValueError):
        urgency = 5.0
    display = (base + urgency * config.W1_URGENCY + interest_pts
               + distance * config.W_DISTANCE - time_decay_penalty(news_time(story)))
    relevance = {
        "quality": round(quality * 10, 1),
        "credibility": round(credibility * 10, 1),
        "source_trust": round(trust, 2),
        "user_impact": round(impact * 10, 1),
        "user_interest": round(interest, 2),
        "user_interest_points": round(interest_pts, 2),
        "distance": round(distance * 10, 1),
        "distance_points": round(distance * config.W_DISTANCE, 2),
        "base": round(base, 2),
    }
    return relevance, display


def news_time(story: sqlite3.Row) -> str | None:
    """Last update that added facts; rewrites without news don't make a story recent again."""
    return story["news_at"] or story["updated_at"]


def read_states(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """``{story_id: {read: "read"|"updated", read_at, read_summary}}`` for stories marked read."""
    rows = conn.execute(
        "SELECT r.story_id, r.read_at, r.summary, COALESCE(s.material_at, s.created_at) > r.read_at "
        "FROM StoryRead r JOIN Story s ON s.id = r.story_id"
    )
    return {sid: {"read": "updated" if newer else "read", "read_at": at, "read_summary": summary}
            for sid, at, summary, newer in rows}


def ranked_stories(conn: sqlite3.Connection, profile: dict[str, Any] | None = None, *,
                   archived: bool = False, query: str = "", tag: str = "",
                   personal: bool = True) -> list[dict[str, Any]]:
    """Finished stories as ``[{story, source_count, relevance, display, read}]``.

    Active stories (news within STORY_ACTIVE_DAYS) are ranked by display
    score; ``archived`` returns the older ones, newest first.
    Honors ``preferences.min_quality`` and optional text / tag filters.
    ``read`` is None, "read" or "updated" (significant update since it was read).
    ``personal=False`` ranks the default view (DEFAULT_PROFILE, no distance).
    """
    profile = (profile or get_user_profile(conn)) if personal else DEFAULT_PROFILE
    stats = source_stats(conn, feed_trust(conn, profile), exclude=excluded_feeds(profile))
    reads = read_states(conn)
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=config.STORY_ACTIVE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    sql = ("SELECT * FROM Story WHERE headline IS NOT NULL AND COALESCE(news_at, updated_at) "
           + ("< ?" if archived else ">= ?"))
    params: list[Any] = [cutoff]
    if query:
        sql += " AND (headline LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [like, like, like]
    try:
        min_quality = float(profile["preferences"].get("min_quality") or 0)
    except (TypeError, ValueError):
        min_quality = 0.0
    wanted_tag = normalize_tags([tag])[0] if tag and normalize_tags([tag]) else ""
    out = []
    for s in conn.execute(sql, params):
        st = stats.get(s["id"])
        if st is None:
            continue
        quality = st["quality"] if st["quality"] is not None else (s["quality_score"] or 0)
        if quality < min_quality:
            continue
        if wanted_tag and wanted_tag not in story_tags(s):
            continue
        relevance, display = score_story(s, st["feeds"], profile, st["trust"], quality, personal)
        out.append({"story": s, "source_count": st["feeds"], "quality": round(quality, 2),
                    "relevance": relevance, "display": display, **reads.get(s["id"], {"read": None})})
    if archived:
        out.sort(key=lambda x: news_time(x["story"]) or "", reverse=True)
    else:
        out.sort(key=lambda x: x["display"], reverse=True)
    return out


# --- profile + feedback ----------------------------------------------------
def get_user_profile(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    if conn is None:
        with db.get_conn() as c:
            return get_user_profile(c)
    row = conn.execute("SELECT * FROM UserProfile WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO UserProfile (id, tag_weights, source_weights, preferences) VALUES (1, '{}', '{}', ?)",
            (json.dumps(_DEFAULT_PREFS),),
        )
        return {"id": 1, "tag_weights": {}, "source_weights": {}, "feed_weights": {},
                "preferences": dict(_DEFAULT_PREFS)}
    return {
        "id": 1,
        "tag_weights": db.json_dict(row["tag_weights"]),
        "source_weights": db.json_dict(row["source_weights"]),
        "feed_weights": db.json_dict(row["feed_weights"]),
        "preferences": db.json_dict(row["preferences"]),
    }


def update_profile(preferences: dict[str, Any] | None = None,
                   tag_weights: dict[str, float] | None = None,
                   source_weights: dict[str, float] | None = None,
                   feed_weights: dict[int, float] | None = None) -> dict[str, Any]:
    """Merge validated preferences; ``tag_weights`` / ``source_weights`` / ``feed_weights``
    (per-feed trust overrides, if given) replace the stored ones."""
    with db.get_conn() as conn:
        profile = get_user_profile(conn)
        prefs = {**profile["preferences"], **(preferences or {})}
        conn.execute("UPDATE UserProfile SET preferences = ? WHERE id = 1", (json.dumps(prefs),))
        if source_weights is not None:
            clean_sw = {str(k): _clamp01(float(v)) for k, v in source_weights.items()}
            conn.execute("UPDATE UserProfile SET source_weights = ? WHERE id = 1", (json.dumps(clean_sw),))
        if feed_weights is not None:
            known = {r[0] for r in conn.execute("SELECT id FROM Feed")}
            clean_fw = {str(k): _clamp01(float(v)) for k, v in feed_weights.items() if int(k) in known}
            conn.execute("UPDATE UserProfile SET feed_weights = ? WHERE id = 1", (json.dumps(clean_fw),))
        if tag_weights is not None:
            clean = {}
            for tag, w in tag_weights.items():
                key = normalize_tags([tag])
                if key:
                    clean[key[0]] = max(-10.0, min(10.0, float(w)))
            conn.execute("UPDATE UserProfile SET tag_weights = ? WHERE id = 1", (json.dumps(clean),))
        return get_user_profile(conn)


def apply_feedback(story_id: int, kind: str) -> dict[str, Any] | None:
    """Log the raw vote and nudge the story's tag weights by ±FEEDBACK_ALPHA·10 (clamped ±10)."""
    with db.get_conn() as conn:
        story = conn.execute("SELECT headline, tags FROM Story WHERE id = ?", (story_id,)).fetchone()
        if story is None:
            return None
        conn.execute("INSERT INTO FeedbackLog (story_id, headline, tags, kind) VALUES (?, ?, ?, ?)",
                     (story_id, story["headline"], story["tags"], kind))
        profile = get_user_profile(conn)
        weights = profile["tag_weights"]
        step = (1.0 if kind == "up" else -1.0) * config.FEEDBACK_ALPHA * 10
        for tag in story_tags(story):
            weights[tag] = max(-10.0, min(10.0, weights.get(tag, 0.0) + step))
        conn.execute("UPDATE UserProfile SET tag_weights = ? WHERE id = 1", (json.dumps(weights),))
        return profile


def feedback_log(limit: int = 200) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT story_id, headline, tags, kind, created_at FROM FeedbackLog ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]
