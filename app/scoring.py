"""Ranking (v2) and the per-user profile: tag list, 👍/👎, chats.

Every story gets a 0..100 score; all features are 0..1 so the weights are comparable:

    public   = Σ W_PUB_x · x   x ∈ quality, coverage, urgency, impact, velocity, status (weights sum to 1)
    personal = public + W_AFFINITY · tag_affinity (-1..1) + W_GEO · geo (0..1)        ("For you" only)
    score    = 100 · freshness · soft_cap(value + update_boost)   (linear to 0.8, then bends towards 1)

- quality       trust-weighted median of member article qualities / 10 (low-trust reposts
                can't water down a credible source)
- coverage      1 - e^(-outlet_trust / COVERAGE_SATURATION); outlet_trust sums the best feed
                trust of each distinct outlet (several feeds of one site count once)
- urgency       (urgency_level - 1) / 9, capped at RESOLVED_URGENCY_CAP for Resolved stories
- impact        IMPACT_SCORE[impact_scale]; status = STATUS_SCORE[status]
- velocity      saturating sum of recent StoryUpdate significance and of outlets reporting now
- freshness     2^(-hours since news_at / FRESHNESS_HALF_LIFE_HOURS[status]): multiplicative,
                so every story fades unless it gets new facts; an update moves news_at only
                FRESHNESS_REFRESH[significance] of the way to its publication time
- update_boost  strongest recent update: UPDATE_BOOST · BOOST_SIG_WEIGHT[significance], fading like velocity
- tag_affinity  noisy-OR of the positive and of the negative tag signals, tanh(weight / 4) each:
                explicit tag list first, else inferred weights (decayed 👍/👎 and story chats)
                scaled by tag specificity, so generic tags ("germany") count little
- geo           home city 1.0 / country 0.6 / language 0.35 (preferences.home_*, else server
                USER_*), or 0.5 per preferences.locations / interest_regions hit
A tag at or below TAG_MUTE_WEIGHT on the tag list hides its stories from "For you".
"For you" then multiplies by READER_FACTOR (read / updated since read), and the top of every
active list is reranked for variety (app/diversity.py).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import statistics
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from . import config, db, diversity, search, tags as tagmod
from .tags import normalize_tags

SCORE_VERSION = "v2.1"
_DEFAULT_PREFS = {"min_quality": 0.0, "summary_length": "medium"}
# The "default" view (everyone, signed out): no learned interests, preferences or trust overrides.
DEFAULT_PROFILE: dict[str, Any] = {"id": None, "tag_weights": {}, "inferred_weights": {}, "source_weights": {},
                                   "feed_weights": {}, "preferences": {}}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def story_tags(story: sqlite3.Row) -> list[str]:
    return normalize_tags([t for t in db.json_list(story["tags"]) if isinstance(t, str)])


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _hours_since(ts: str | None, now: dt.datetime) -> float | None:
    t = db.parse_ts(ts)
    return None if t is None else max(0.0, (now - t).total_seconds() / 3600)


def _fade(age_h: float, half_life_h: float) -> float:
    return 2.0 ** (-age_h / half_life_h) if half_life_h > 0 else 0.0


def coverage_score(trust: float) -> float:
    """``trust`` = summed trust of the distinct outlets (= outlet count when all are fully trusted)."""
    return 1.0 - math.exp(-max(0.0, trust) / config.COVERAGE_SATURATION)


def freshness(story: sqlite3.Row, status: str | None, now: dt.datetime | None = None) -> tuple[float, float, float]:
    """``(factor, half_life_h, age_h)`` of the story's last news."""
    hl = config.FRESHNESS_HALF_LIFE_HOURS
    half_life = hl.get(status or "", hl.get("Ongoing", 30.0))
    age = _hours_since(news_time(story), now or _now())
    if age is None:
        return 1.0, half_life, 0.0
    return _fade(age, half_life), half_life, age


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


def feed_trust_list(conn: sqlite3.Connection, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Every feed with its effective trust and whether the profile overrides it (⚙ Preferences)."""
    per_feed = feed_trust(conn, profile)
    return [{"id": f["id"], "title": f["title"], "category": f["category"],
             "trust": per_feed[f["id"]], "custom": str(f["id"]) in profile["feed_weights"]}
            for f in conn.execute("SELECT id, title, COALESCE(category, '') AS category FROM Feed "
                                  "ORDER BY category, title COLLATE NOCASE")]


_FEED_HOST_PREFIXES = ("www.", "feeds.", "feed.", "rss.", "nachrichtenfeeds.")


def outlet_key(site_url: str | None, xml_url: str | None) -> str:
    """The publisher behind a feed: site host without feed subdomains, mapped through OUTLET_GROUPS."""
    for url in (site_url, xml_url):
        host = (urlsplit(url or "").hostname or "").lower()
        if host:
            for prefix in _FEED_HOST_PREFIXES:
                host = host.removeprefix(prefix)
            return config.OUTLET_GROUPS.get(host, host)
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


def median_quality(articles: list[tuple[int, int | None, float | None, float]]) -> float | None:
    """Trust-weighted median of ``(article_id, duplicate_of, quality, trust)``; None without scores.

    Cross-feed copies (``duplicate_of``) count once with their original, at the best trust of
    any copy. Plain median if all trust is 0.
    """
    best: dict[int, tuple[float, float]] = {}
    for aid, dup, q, w in articles:
        if q is None:
            continue
        root = dup or aid
        prev = best.get(root)
        best[root] = (prev[0] if prev else float(q), max(w, prev[1]) if prev else w)
    if not best:
        return None
    quality = weighted_median(list(best.values()))
    return quality if quality is not None else float(statistics.median(q for q, _ in best.values()))


def about_user(profile: dict[str, Any]) -> str:
    """The user's self-description (preferences.about) for personal AI texts; "" if unset."""
    about = profile["preferences"].get("about")
    return about.strip()[:1000] if isinstance(about, str) else ""


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
                 story_id: int | None = None, exclude: set[int] = frozenset(),
                 story_ids: list[int] | None = None) -> dict[int, dict[str, Any]]:
    """``{story_id: {feeds, outlets, trust, quality, heat}}`` per story (``story_id`` / ``story_ids`` limit it).

    ``trust`` = ``feed_trust()``; the result's ``trust`` sums the best feed trust of each distinct
    outlet (``outlets`` = their count); ``quality`` is ``median_quality`` of the articles; ``heat``
    sums each outlet's trust faded by the age of its newest original article (outlets reporting
    right now; cross-feed copies don't count as new reporting).
    Articles of ``exclude`` feeds don't count (a story with only those is left out).
    """
    outlet_of = {fid: outlet_key(site, xml) or f"feed:{fid}"
                 for fid, site, xml in conn.execute("SELECT id, site_url, xml_url FROM Feed")}
    sql = ("SELECT a.id, a.duplicate_of, a.story_id, a.feed_id, a.quality, a.created_at "
           "FROM Article a WHERE a.story_id IS NOT NULL")
    if story_id is not None:
        rows = conn.execute(sql + " AND a.story_id = ?", (story_id,))
    elif story_ids is not None:
        rows = conn.execute(sql + " AND a.story_id IN (SELECT value FROM json_each(?))", (json.dumps(story_ids),))
    else:
        rows = conn.execute(sql)
    feeds: dict[int, set[int]] = {}
    outlets: dict[int, dict[str, float]] = {}
    newest: dict[int, dict[str, str]] = {}
    quals: dict[int, list[tuple[int, int | None, float | None, float]]] = {}
    for r in rows:
        if r["feed_id"] in exclude:
            continue
        sid, w = r["story_id"], trust.get(r["feed_id"], config.SOURCE_TRUST_DEFAULT)
        feeds.setdefault(sid, set()).add(r["feed_id"])
        key = outlet_of.get(r["feed_id"]) or f"feed:{r['feed_id']}"
        best = outlets.setdefault(sid, {})
        best[key] = max(best.get(key, 0.0), w)
        latest = newest.setdefault(sid, {})
        if r["duplicate_of"] is None:
            latest[key] = max(latest.get(key, ""), r["created_at"] or "")
        quals.setdefault(sid, []).append((r["id"], r["duplicate_of"], r["quality"], w))
    now = _now()
    out = {}
    for sid, fids in feeds.items():
        quality = median_quality(quals.get(sid, []))
        heat = sum(w * _fade(_hours_since(newest[sid][k], now) or 0.0, config.VELOCITY_OUTLET_HALF_LIFE_HOURS)
                   for k, w in outlets[sid].items() if newest[sid].get(k))
        out[sid] = {"feeds": len(fids), "outlets": len(outlets[sid]), "trust": sum(outlets[sid].values()),
                    "quality": quality, "heat": heat}
    return out


def _mentions(value: str, tag: str) -> bool:
    """``value`` appears in ``tag`` as whole words ("us" matches "us-politics", not "music")."""
    return re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", tag) is not None


def region_hits(tags: list[str], preferences: dict[str, Any]) -> list[str]:
    """preferences.interest_regions / locations entries found (as whole words) in the tags."""
    hits: list[str] = []
    for key in ("interest_regions", "locations"):
        values = preferences.get(key)
        if not isinstance(values, list):
            continue
        for v in values:
            v = str(v).lower().strip()
            if v and v not in hits and any(_mentions(v, t) for t in tags):
                hits.append(v)
    return hits


def distance_score(metrics: dict[str, Any], preferences: dict[str, Any] | None = None) -> float:
    """Story location/language vs the user's home (preferences.home_*; server config if unset)."""
    prefs = preferences or {}

    def home(key: str, default: str) -> str:
        value = prefs.get(key)
        return (value if isinstance(value, str) and value.strip() else default).lower().strip()

    location = str(metrics.get("location") or "").lower()
    language = str(metrics.get("language") or "").lower()
    city, country = home("home_city", config.USER_CITY), home("home_country", config.USER_COUNTRY)
    score = 0.0
    if location:
        if city and city in location:
            score = 1.0
        elif country and country in location:
            score = 0.6
    lang = home("language", config.USER_LANGUAGE)
    if lang and language == lang:
        score = max(score, 0.35)
    return score


def geo_score(tags: list[str], metrics: dict[str, Any], preferences: dict[str, Any]) -> tuple[float, str, list[str]]:
    """``(0..1, reason, region hits)``; reason is city | country | language | region | ""."""
    dist = distance_score(metrics, preferences)
    hits = region_hits(tags, preferences)
    region = min(1.0, 0.5 * len(hits))
    if region > dist:
        return region, "region", hits
    return dist, {1.0: "city", 0.6: "country", 0.35: "language"}.get(dist, ""), hits


# --- tag affinity ------------------------------------------------------------
_DF_TTL_SECONDS = 600
_df_cache: dict[str, tuple[float, int, dict[str, int]]] = {}
_df_lock = threading.Lock()


def tag_frequencies(conn: sqlite3.Connection) -> tuple[int, dict[str, int]]:
    """``(active story count, {tag: stories carrying it})``, cached for a few minutes."""
    key = str(config.DB_PATH)
    with _df_lock:
        hit = _df_cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1], hit[2]
    cutoff = (_now() - dt.timedelta(days=config.STORY_ACTIVE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    n, df = 0, {}
    for (raw,) in conn.execute("SELECT tags FROM Story WHERE headline IS NOT NULL AND news_at >= ?", (cutoff,)):
        n += 1
        for t in normalize_tags([t for t in db.json_list(raw) if isinstance(t, str)]):
            df[t] = df.get(t, 0) + 1
    with _df_lock:
        _df_cache[key] = (time.monotonic() + _DF_TTL_SECONDS, n, df)
    return n, df


def specificity(tag: str, n: int, df: dict[str, int]) -> float:
    """Normalized IDF: a tag on nearly every story → INFERRED_MIN_SPECIFICITY, a rare one → ~1."""
    norm = math.log(n + 1)
    if norm <= 0:
        return 1.0
    idf = math.log((n + 1) / (df.get(tag, 0) + 1)) / norm
    return max(config.INFERRED_MIN_SPECIFICITY, min(1.0, idf))


def tag_signals(tags: list[str], profile: dict[str, Any],
                freq: tuple[int, dict[str, int]] | None = None) -> list[dict[str, Any]]:
    """Per matching tag: tanh-bounded signal -1..1; the tag list wins over inferred weights."""
    explicit, inferred = profile["tag_weights"], profile.get("inferred_weights") or {}
    k = config.TAG_WEIGHT_SCALE
    out = []
    for t in tags:
        if t in explicit:
            s, source = math.tanh(explicit[t] / k), "list"
        elif t in inferred:
            spec = specificity(t, *freq) if freq else 1.0
            s, source = spec * math.tanh(inferred[t] / k), "learned"
        else:
            continue
        if abs(s) >= 0.005:
            out.append({"tag": t, "signal": s, "source": source})
    return out


def tag_affinity(signals: list[dict[str, Any]]) -> float:
    """Noisy-OR per sign: more matches raise it towards ±1, a weak extra match never lowers it."""
    pos = 1.0 - math.prod(1.0 - s["signal"] for s in signals if s["signal"] > 0)
    neg = 1.0 - math.prod(1.0 + s["signal"] for s in signals if s["signal"] < 0)
    return max(-1.0, min(1.0, pos - neg))


def muted_tag(tags: list[str], profile: dict[str, Any]) -> str | None:
    """The first story tag the user muted (tag list weight ≤ TAG_MUTE_WEIGHT)."""
    weights = profile["tag_weights"]
    return next((t for t in tags if weights.get(t, 0.0) <= config.TAG_MUTE_WEIGHT), None)


# --- story dynamics + score ----------------------------------------------------
def story_dynamics(conn: sqlite3.Connection, story_id: int | None = None,
                   now: dt.datetime | None = None) -> dict[int, dict[str, float]]:
    """``{story_id: {updates, updates_24h, boost}}`` from recent StoryUpdate rows, timed by event_at.

    ``updates`` = Σ VELOCITY_SIG_WEIGHT faded by age (one major update outweighs a run of minor
    ones); ``boost`` = the strongest faded UPDATE_BOOST · BOOST_SIG_WEIGHT, so a later minor
    update can't replace a major one.
    """
    now = now or _now()
    half_life = config.VELOCITY_UPDATE_HALF_LIFE_HOURS
    cutoff = (now - dt.timedelta(hours=max(24.0, 6 * half_life))).strftime("%Y-%m-%d %H:%M:%S")
    sql = ("SELECT story_id, significance, COALESCE(event_at, created_at) FROM StoryUpdate "
           "WHERE COALESCE(event_at, created_at) >= ?")
    params: list[Any] = [cutoff]
    if story_id is not None:
        sql += " AND story_id = ?"
        params.append(story_id)
    out: dict[int, dict[str, float]] = {}
    for sid, sig, at in conn.execute(sql + " ORDER BY id", params):
        age = _hours_since(at, now)
        if age is None:
            continue
        d = out.setdefault(sid, {"updates": 0.0, "updates_24h": 0, "boost": 0.0})
        fade = _fade(age, half_life)
        d["updates"] += config.VELOCITY_SIG_WEIGHT.get(str(sig), 0.0) * fade
        d["updates_24h"] += age <= 24
        d["boost"] = max(d["boost"], config.UPDATE_BOOST * config.BOOST_SIG_WEIGHT.get(str(sig), 0.0) * fade)
    return out


def refreshed_news_at(old: str | None, trigger: str, significance: int) -> str:
    """``news_at`` after an update published at ``trigger``: the story's age shrinks by
    FRESHNESS_REFRESH[significance], so a minor detail barely rejuvenates it; never moves back."""
    o, t = db.parse_ts(old), db.parse_ts(trigger)
    if o is None:
        return trigger
    if t is None or t <= o:
        return old
    share = _clamp01(config.FRESHNESS_REFRESH.get(str(significance), 0.0))
    return (o + (t - o) * share).strftime("%Y-%m-%d %H:%M:%S")


def reader_factor(state: dict[str, Any] | None) -> float:
    """"For you" factor by read state: what the reader already knows counts less."""
    state = state or {}
    factors = config.READER_FACTOR
    if state.get("read") == "read":
        return factors.get("read", 1.0)
    if state.get("read") == "updated":
        return factors.get("updated_major" if state.get("since_sig", 0) >= 3 else "updated", 1.0)
    return 1.0


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_KNEE = 0.8


def soft_cap(x: float) -> float:
    """0..1: linear up to the knee, then bends towards 1 so the best stories keep their order."""
    if x <= _KNEE:
        return max(0.0, x)
    return _KNEE + (1 - _KNEE) * math.tanh((x - _KNEE) / (1 - _KNEE))


def score_story(story: sqlite3.Row, stats: dict[str, Any], profile: dict[str, Any], *,
                personal: bool = True, dynamics: dict[str, float] | None = None,
                freq: tuple[int, dict[str, int]] | None = None,
                now: dt.datetime | None = None) -> tuple[dict[str, Any], float]:
    """Return ``(breakdown, score 0..100)`` for one story.

    ``stats`` = the story's ``source_stats`` entry, ``dynamics`` its ``story_dynamics`` entry,
    ``freq`` = ``tag_frequencies`` (specificity of inferred weights). ``personal=False``
    (default view) leaves out tags and location.
    """
    tags = story_tags(story)
    metrics = db.json_dict(story["metrics"])
    quality = stats.get("quality")
    q = _clamp01(_float(quality if quality is not None else story["quality_score"], 5.0) / 10.0)
    trust = float(stats.get("trust") or 0.0)
    coverage = coverage_score(trust)
    level = max(1.0, min(10.0, _float(metrics.get("urgency_level", 5), 5.0)))
    status = metrics.get("status")
    urgency = (level - 1.0) / 9.0
    if status == "Resolved":
        urgency = min(urgency, config.RESOLVED_URGENCY_CAP)
    impact = config.IMPACT_SCORE.get(metrics.get("impact_scale"), config.IMPACT_SCORE.get("National", 0.7))
    status_score = config.STATUS_SCORE.get(status, config.STATUS_SCORE.get("Ongoing", 0.6))
    dyn = dynamics or {}
    velocity = 1.0 - math.exp(-(0.7 * dyn.get("updates", 0.0) + 0.3 * stats.get("heat", 0.0)) / 2.0)
    parts = {
        "quality": config.W_PUB_QUALITY * q,
        "coverage": config.W_PUB_COVERAGE * coverage,
        "urgency": config.W_PUB_URGENCY * urgency,
        "impact": config.W_PUB_IMPACT * impact,
        "velocity": config.W_PUB_VELOCITY * velocity,
        "status": config.W_PUB_STATUS * status_score,
    }
    public = sum(parts.values())
    signals, affinity, geo, geo_reason, regions, muted = [], 0.0, 0.0, "", [], None
    if personal:
        signals = tag_signals(tags, profile, freq)
        affinity = tag_affinity(signals)
        geo, geo_reason, regions = geo_score(tags, metrics, profile["preferences"])
        muted = muted_tag(tags, profile)
        parts["tags"] = config.W_AFFINITY * affinity
        parts["geo"] = config.W_GEO * geo
    parts["news"] = dyn.get("boost", 0.0)
    value = soft_cap(sum(parts.values()))
    fresh, half_life, age = freshness(story, status, now)
    score = 100.0 * fresh * value
    signals.sort(key=lambda s: -abs(s["signal"]))
    breakdown = {
        "version": SCORE_VERSION,
        "quality": round(q * 10, 1),
        "coverage": round(coverage, 2),
        "outlet_trust": round(trust, 2),
        "urgency": int(level),
        "impact": metrics.get("impact_scale"),
        "status": status,
        "velocity": round(velocity, 2),
        "updates_24h": int(dyn.get("updates_24h", 0)),
        "public": round(100 * public, 1),
        "affinity": round(affinity, 2),
        "matches": [{**s, "signal": round(s["signal"], 2)} for s in signals[:4]],
        "geo": round(geo, 2),
        "geo_reason": geo_reason,
        "regions": regions,
        "muted": muted,
        "value": round(100 * value, 1),
        "freshness": round(fresh, 3),
        "half_life_h": half_life,
        "age_h": round(age, 1),
        "parts": {k: round(100 * v, 1) for k, v in parts.items()},
    }
    return breakdown, score


def news_time(story: sqlite3.Row) -> str | None:
    """Last update that added facts; rewrites without news don't make a story recent again."""
    return story["news_at"] or story["updated_at"]


CHANGES_MAX = 3


def read_states(conn: sqlite3.Connection, user_id: int | None) -> dict[int, dict[str, Any]]:
    """``{story_id: {read: "read"|"updated", read_at, since_sig, changes}}`` for the user's read stories.

    ``since_sig`` = the strongest update significance after ``read_at``; ``changes`` (updated
    stories only): up to CHANGES_MAX new facts reported since the user read the story, most
    significant / newest first.
    """
    if user_id is None:
        return {}
    rows = conn.execute(
        "SELECT r.story_id, r.read_at, COALESCE(s.material_at, s.created_at) > r.read_at "
        "FROM StoryRead r JOIN Story s ON s.id = r.story_id WHERE r.user_id = ?", (user_id,)
    )
    out = {sid: {"read": "updated" if newer else "read", "read_at": at, "since_sig": 0, "changes": []}
           for sid, at, newer in rows}
    facts = conn.execute(
        "SELECT u.story_id, u.significance, u.facts FROM StoryUpdate u JOIN StoryRead r "
        "ON r.story_id = u.story_id AND r.user_id = ? AND u.created_at > r.read_at "
        "ORDER BY u.significance DESC, u.id DESC", (user_id,)
    )
    for sid, sig, raw in facts:
        out[sid]["since_sig"] = max(out[sid]["since_sig"], sig)
        changes = out[sid]["changes"]
        if out[sid]["read"] != "updated":
            continue
        for fact in db.json_list(raw):
            if len(changes) < CHANGES_MAX and fact not in changes:
                changes.append(fact)
    return out


def vote_states(conn: sqlite3.Connection, user_id: int | None) -> dict[int, str]:
    """``{story_id: "up"|"down"}`` for the user's active votes."""
    if user_id is None:
        return {}
    rows = conn.execute(
        "SELECT story_id, kind FROM FeedbackLog WHERE user_id = ? AND story_id IS NOT NULL ORDER BY id ASC",
        (user_id,),
    )
    return {sid: kind for sid, kind in rows if kind in ("up", "down")}


def ranked_stories(conn: sqlite3.Connection, profile: dict[str, Any] | None = None, *,
                   archived: bool = False, query: str = "", tag: str = "",
                   personal: bool = True, user_id: int | None = 1,
                   keep: frozenset[int] | set[int] = frozenset(), diverse: bool = True,
                   semantic: bool = False) -> list[dict[str, Any]]:
    """Finished stories as ``[{story, source_count, relevance, display, read}]``.

    Active stories (news within STORY_ACTIVE_DAYS) are ranked by score (``display``,
    ``relevance`` = its breakdown); ``archived`` returns the older ones, newest first.
    Honors ``preferences.min_quality``, muted tags (personal) and optional text / tag filters.
    A ``query`` keeps only search matches (app/search.py; ``semantic`` adds meaning matches),
    ordered by match, and sets each item's ``match``.
    ``read`` is None, "read" or "updated" (significant update since ``user_id`` read it); the
    personal view scales the score by ``reader_factor`` except for ``keep`` ids (read during
    this page visit, so they don't jump). ``diverse`` reranks the top for variety.
    ``personal=False`` ranks the default view (DEFAULT_PROFILE, no tags or location);
    otherwise ``profile`` defaults to the profile of ``user_id``.
    """
    tagmod.load_aliases(conn)
    if not personal:
        profile = DEFAULT_PROFILE
    elif profile is None:
        profile = get_user_profile(conn, user_id or 1)
    reads = read_states(conn, user_id)
    votes = vote_states(conn, user_id)
    now = _now()
    cutoff = (now - dt.timedelta(days=config.STORY_ACTIVE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    # news_at is always set once a story has a headline (Prompt 2 / migration v5 / init_db backfill).
    sql = "SELECT * FROM Story WHERE headline IS NOT NULL AND news_at " + ("< ?" if archived else ">= ?")
    params: list[Any] = [cutoff]
    hits: dict[int, dict[str, Any]] = {}
    if query:
        hits = search.search(conn, query, archived=archived, cutoff=cutoff, semantic=semantic)
        if not hits:
            return []
        sql += f" AND id IN ({','.join('?' * len(hits))})"
        params += list(hits)
    try:
        min_quality = float(profile["preferences"].get("min_quality") or 0)
    except (TypeError, ValueError):
        min_quality = 0.0
    wanted_tag = normalize_tags([tag])[0] if tag and normalize_tags([tag]) else ""
    stories = conn.execute(sql, params).fetchall()
    stats = source_stats(conn, feed_trust(conn, profile), exclude=excluded_feeds(profile),
                         story_ids=[s["id"] for s in stories])
    dynamics = story_dynamics(conn, now=now)
    freq = tag_frequencies(conn) if personal and profile.get("inferred_weights") else None
    out = []
    for s in stories:
        st = stats.get(s["id"])
        if st is None:
            continue
        quality = st["quality"] if st["quality"] is not None else (s["quality_score"] or 0)
        if quality < min_quality:
            continue
        if wanted_tag and wanted_tag not in story_tags(s):
            continue
        relevance, display = score_story(s, st, profile, personal=personal, dynamics=dynamics.get(s["id"]),
                                         freq=freq, now=now)
        if relevance["muted"] and not wanted_tag:
            continue
        state = reads.get(s["id"], {"read": None})
        if personal and user_id is not None and s["id"] not in keep:
            display = apply_reader_state(relevance, display, state)
        out.append({"story": s, "source_count": st["feeds"], "outlet_count": st["outlets"],
                    "quality": round(quality, 2),
                    "relevance": relevance, "display": display, "vote": votes.get(s["id"]), **state})
    if hits:
        boost = config.SEARCH_PRIORITY_BOOST
        for x in out:
            x["match"] = hits[x["story"]["id"]]["match"]
        out.sort(key=lambda x: hits[x["story"]["id"]]["score"] * (1 + boost * max(0.0, x["display"]) / 100),
                 reverse=True)
    elif archived:
        out.sort(key=lambda x: news_time(x["story"]) or "", reverse=True)
    else:
        out.sort(key=lambda x: x["display"], reverse=True)
        if diverse:
            out = diversity.rerank(conn, out)
    return out


def apply_reader_state(relevance: dict[str, Any], display: float, state: dict[str, Any] | None) -> float:
    """Scale a personal score by ``reader_factor`` (recorded in the breakdown)."""
    factor = reader_factor(state)
    relevance["reader"] = factor
    return display * factor


# --- profile + feedback (per user; user 1 = the owner from the single-user era) ---
def inferred_weights(conn: sqlite3.Connection, user_id: int, now: dt.datetime | None = None) -> dict[str, float]:
    """Tag weights learned from 👍/👎 and story chats, faded by age (VOTE_HALF_LIFE_DAYS).

    One event spreads ±VOTE_WEIGHT (chat: +CHAT_WEIGHT) over its story's tags by 1/√n, so a
    story with six tags doesn't count six times. Never written to the tag list.
    """
    now = now or _now()
    half_lives = config.VOTE_HALF_LIFE_DAYS
    events = [(kind, raw, at) for kind, raw, at in conn.execute(
        "SELECT kind, tags, created_at FROM FeedbackLog WHERE user_id = ?", (user_id,)) if kind in ("up", "down")]
    events += [("chat", raw, at) for raw, at in conn.execute(
        "SELECT s.tags, c.created_at FROM ChatSession c JOIN Story s ON s.id = c.story_id "
        "WHERE c.user_id = ? AND c.leaf_id IS NOT NULL", (user_id,))]
    out: dict[str, float] = {}
    for kind, raw, at in events:
        tags = normalize_tags([t for t in db.json_list(raw) if isinstance(t, str)])
        age_h = _hours_since(at, now)
        if not tags or age_h is None:
            continue
        strength = {"up": config.VOTE_WEIGHT, "down": -config.VOTE_WEIGHT, "chat": config.CHAT_WEIGHT}[kind]
        delta = strength / math.sqrt(len(tags)) * _fade(age_h / 24, half_lives.get(kind, 60.0))
        for t in tags:
            out[t] = out.get(t, 0.0) + delta
    return {t: round(max(-10.0, min(10.0, w)), 3) for t, w in out.items() if abs(w) >= 0.01}


def get_user_profile(conn: sqlite3.Connection | None = None, user_id: int = 1) -> dict[str, Any]:
    if conn is None:
        with db.get_conn() as c:
            return get_user_profile(c, user_id)
    row = conn.execute("SELECT * FROM UserProfile WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO UserProfile (id, tag_weights, source_weights, preferences) VALUES (?, '{}', '{}', ?)",
            (user_id, json.dumps(_DEFAULT_PREFS)),
        )
        return {"id": user_id, "tag_weights": {}, "inferred_weights": inferred_weights(conn, user_id),
                "source_weights": {}, "feed_weights": {}, "preferences": dict(_DEFAULT_PREFS)}
    return {
        "id": user_id,
        "tag_weights": tagmod.merge_weights(db.json_dict(row["tag_weights"])),
        "inferred_weights": inferred_weights(conn, user_id),
        "source_weights": db.json_dict(row["source_weights"]),
        "feed_weights": db.json_dict(row["feed_weights"]),
        "preferences": db.json_dict(row["preferences"]),
    }


def update_profile(preferences: dict[str, Any] | None = None,
                   tag_weights: dict[str, float] | None = None,
                   source_weights: dict[str, float] | None = None,
                   feed_weights: dict[int, float] | None = None, *, user_id: int = 1) -> dict[str, Any]:
    """Merge validated preferences; ``tag_weights`` (the explicit tag list) / ``source_weights`` /
    ``feed_weights`` (per-feed trust overrides, if given) replace the stored ones."""
    with db.get_conn() as conn:
        profile = get_user_profile(conn, user_id)
        prefs = {**profile["preferences"], **(preferences or {})}
        conn.execute("UPDATE UserProfile SET preferences = ? WHERE id = ?", (json.dumps(prefs), user_id))
        if source_weights is not None:
            clean_sw = {str(k): _clamp01(float(v)) for k, v in source_weights.items()}
            conn.execute("UPDATE UserProfile SET source_weights = ? WHERE id = ?", (json.dumps(clean_sw), user_id))
        if feed_weights is not None:
            known = {r[0] for r in conn.execute("SELECT id FROM Feed")}
            clean_fw = {str(k): _clamp01(float(v)) for k, v in feed_weights.items() if int(k) in known}
            conn.execute("UPDATE UserProfile SET feed_weights = ? WHERE id = ?", (json.dumps(clean_fw), user_id))
        if tag_weights is not None:
            conn.execute("UPDATE UserProfile SET tag_weights = ? WHERE id = ?",
                         (json.dumps(tagmod.merge_weights(tag_weights)), user_id))
        return get_user_profile(conn, user_id)


def apply_feedback(story_id: int, kind: str | None, user_id: int = 1, age: float = 0.0) -> dict[str, Any] | None:
    """Set, switch or remove (``kind=None``) a user's vote on ``story_id``, cast ``age`` seconds ago
    (a later vote change wins).

    Votes only go to FeedbackLog (with a headline/tags snapshot); ranking turns them into
    decaying inferred weights, the user's tag list stays untouched.
    """
    with db.get_conn() as conn:
        # Serialized: two quick clicks must not both log a vote.
        conn.execute("BEGIN IMMEDIATE")
        try:
            found = _apply_vote(conn, story_id, kind, user_id, age)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return get_user_profile(conn, user_id) if found else None


def _apply_vote(conn: sqlite3.Connection, story_id: int, kind: str | None, user_id: int, age: float = 0.0) -> bool:
    alias = conn.execute("SELECT story_id FROM StoryAlias WHERE old_id = ?", (story_id,)).fetchone()
    if alias is not None:
        story_id = alias[0]
    story = conn.execute("SELECT headline, tags FROM Story WHERE id = ?", (story_id,)).fetchone()
    if story is None:
        return False
    if db.claim_mark(conn, user_id, story_id, "vote", age) is None:
        return True
    conn.execute("DELETE FROM FeedbackLog WHERE user_id = ? AND story_id = ?", (user_id, story_id))
    if kind in ("up", "down"):
        conn.execute("INSERT INTO FeedbackLog (user_id, story_id, headline, tags, kind) VALUES (?, ?, ?, ?, ?)",
                     (user_id, story_id, story["headline"], story["tags"], kind))
    return True


def feedback_log(limit: int = 200, user_id: int = 1) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT story_id, headline, tags, kind, created_at FROM FeedbackLog WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?", (user_id, limit)
        )
        return [dict(r) for r in rows]
