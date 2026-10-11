"""Display_Score ranking and the feedback loop.

The personalization core is a **relevance score** built from three 0-10
components (quality, credibility, user_impact), weighted by normalized
weights (sum to 1) so the base stays on a 0-10 scale:

    relevance = quality*WQ + credibility*WC + user_impact*WI     (0..10)
    display   = relevance + urgency*W1 + clamp(user_interest*W3, +/-INTEREST_CAP)
                + distance*W_DISTANCE - time_decay

- quality      : per-story harness quality (1-10).
- credibility  : corroboration — distinct sources backing the story
                 (single-source is low; 5+ maxes out). Honest proxy: a
                 single uncorroborated report scores lower than the same
                 story covered by many feeds.
- user_impact  : how much this affects the user. Driven by collected user
                 data (location/regions) in UserProfile.preferences; a neutral
                 0.5 baseline when nothing is set.
- user_interest: sum of the user's tag weights for the story's tags — the
                 personalization tilt, added on top of relevance (like before).

time_decay is an exponential ramp: 0 at "now", approaching TIME_DECAY_MAX as a
story ages (half-life = TIME_DECAY_HALF_LIFE_HOURS).

Feedback nudges the user's tag weights by a small learning rate (FEEDBACK_ALPHA)
so a single click can't dominate the feed; the raw vote is logged separately in
FeedbackLog.

tag_weights are never decayed, so the *contribution* of the interest term is
clamped to +/- INTEREST_CAP display points in display_score (see
user_interest_points). That bounds personalization against urgency (<= W1*10)
and time decay (<= TIME_DECAY_MAX) while leaving the raw weights intact.
"""
from __future__ import annotations

import datetime
import json
import math
from typing import Any

from . import config, db
from .tags import normalize_tags


def _story_tags(story) -> list[str]:
    try:
        tags = json.loads(story["tags"] or "[]")
    except json.JSONDecodeError:
        tags = []
    # Normalize at read time: pre-fix stories still carry mixed spellings in
    # the DB, and feedback bumps normalized keys.
    return normalize_tags([t for t in tags if isinstance(t, str)])


def _story_metrics(story) -> dict:
    try:
        m = json.loads(story["metrics"] or "{}")
    except json.JSONDecodeError:
        m = {}
    return m


def _hours_since(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0.0
    return max(0.0, (datetime.datetime.utcnow() - dt).total_seconds() / 3600)


def time_decay_penalty(updated_at: str | None) -> float:
    age_hours = _hours_since(updated_at)
    # ramp from 0 -> TIME_DECAY_MAX over many half-lives
    return config.TIME_DECAY_MAX * (1.0 - math.exp(-age_hours / config.TIME_DECAY_HALF_LIFE_HOURS))


def user_interest_score(story, tag_weights: dict[str, float]) -> float:
    return sum(tag_weights.get(t, 0.0) for t in _story_tags(story))


def user_interest_points(story, tag_weights: dict[str, float]) -> float:
    """The clamped display contribution of the interest term.

    The raw tag-weight sum can drift over time (feedback is never decayed),
    so ranking uses the contribution clamped to +/- INTEREST_CAP; the raw
    sum is kept for transparency and for re-deriving ranks later."""
    contrib = user_interest_score(story, tag_weights) * config.W3_INTEREST
    return max(-config.INTEREST_CAP, min(config.INTEREST_CAP, contrib))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def credibility_score(source_count: int) -> float:
    """0..1. Corroboration: a single uncorroborated source is low; more
    distinct sources raise it. 1=0.25, 2=0.45, 3=0.65, 4=0.85, 5+=1.0."""
    return _clamp01(0.25 + 0.20 * max(0, source_count - 1))


def user_impact_score(story, preferences: dict[str, Any]) -> float:
    """0..1. How much the story affects the user, from collected user data.

    Neutral 0.5 baseline. +0.25 for each configured region/location that
    matches a story tag (substring, case-insensitive), capped at 1.0. This is
    the hook for "collect user data like location" — set
    preferences.interest_regions / preferences.locations to raise impact for
    stories that touch them.
    """
    impact = 0.5
    tags = [t.lower() for t in _story_tags(story)]
    for key in ("interest_regions", "locations"):
        vals = preferences.get(key) or []
        if not isinstance(vals, list):
            continue
        for v in vals:
            v = str(v).lower().strip()
            if v and any(v in t for t in tags):
                impact += 0.25
    return _clamp01(impact)


def distance_score(story) -> float:
    """0..1 physical + cultural proximity to the user's location.

    The location/language come from Story.metrics — extracted by the existing
    Prompt2 call, not a separate classifier. Bands:
      city match (USER_CITY in the location string)   -> 1.0
      country match (USER_COUNTRY)                     -> 0.6
      language match (USER_LANGUAGE)                   -> 0.35
      nothing / unknown                                -> 0.0 (neutral)

    Pre-distance stories (no metrics.location) score 0 — neutral, never
    penalized, so existing stories are unaffected.
    """
    m = _story_metrics(story)
    location = str(m.get("location") or "").lower()
    language = str(m.get("language") or "").lower()
    score = 0.0
    if location:
        city = config.USER_CITY.lower().strip()
        country = config.USER_COUNTRY.lower().strip()
        if city and city in location:
            score = 1.0
        elif country and country in location:
            score = 0.6
    lang = config.USER_LANGUAGE.lower().strip()
    if lang and language == lang:
        score = max(score, 0.35)
    return score


def distance_points(story) -> float:
    """Display-point contribution of the distance term (W_DISTANCE * 0..1)."""
    return config.W_DISTANCE * distance_score(story)


def relevance_score(story, tag_weights: dict[str, float], source_count: int = 1,
                    preferences: dict | None = None) -> dict:
    """The relevance breakdown (the personalization base), each component on
    a 0-10 scale plus the weighted 0-10 base. Returned to the frontend so the
    calculation is visible on each card."""
    preferences = preferences or {}
    try:
        quality = _clamp01(float(story["quality_score"] or 5.0) / 10.0)
    except (TypeError, ValueError):
        quality = 0.5
    credibility = credibility_score(source_count)
    impact = user_impact_score(story, preferences)
    interest = user_interest_score(story, tag_weights)
    base = 10.0 * (
        config.W_RELEVANCE_QUALITY * quality
        + config.W_RELEVANCE_CREDIBILITY * credibility
        + config.W_RELEVANCE_IMPACT * impact
    )
    interest = user_interest_score(story, tag_weights)
    distance = distance_score(story)
    return {
        "quality": round(quality * 10, 1),
        "credibility": round(credibility * 10, 1),
        "user_impact": round(impact * 10, 1),
        "user_interest": round(interest, 2),
        "user_interest_points": round(user_interest_points(story, tag_weights), 2),
        "distance": round(distance * 10, 1),
        "distance_points": round(distance_points(story), 2),
        "base": round(base, 2),
    }


def display_score(story, tag_weights: dict[str, float], source_count: int = 1,
                  preferences: dict | None = None) -> float:
    rel = relevance_score(story, tag_weights, source_count, preferences)
    metrics = _story_metrics(story)
    try:
        urgency = float(metrics.get("urgency_level", 5))
    except (TypeError, ValueError):
        urgency = 5.0
    decay = time_decay_penalty(story["updated_at"])
    return (
        rel["base"]
        + urgency * config.W1_URGENCY
        + user_interest_points(story, tag_weights)
        + distance_points(story)
        - decay
    )


def get_user_profile() -> dict:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM UserProfile WHERE id=1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO UserProfile (id, tag_weights, source_weights, preferences) "
                "VALUES (1,'{}','{}','{}')"
            )
            conn.commit()
            row = conn.execute("SELECT * FROM UserProfile WHERE id=1").fetchone()
    return {
        "id": 1,
        "tag_weights": json.loads(row["tag_weights"] or "{}"),
        "source_weights": json.loads(row["source_weights"] or "{}"),
        "preferences": json.loads(row["preferences"] or "{}"),
    }


def update_preferences(new_prefs: dict) -> dict:
    """Merge user-provided preferences (location, interest_regions, ...) into
    the profile. Feeds the user_impact term of the relevance score."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT preferences FROM UserProfile WHERE id=1").fetchone()
        prefs = json.loads(row["preferences"] or "{}") if row else {}
        merged = {**prefs, **new_prefs}
        conn.execute(
            "UPDATE UserProfile SET preferences = ? WHERE id=1",
            (json.dumps(merged),),
        )
        conn.commit()
    return get_user_profile()


def apply_feedback(story_id: int, kind: str) -> dict:
    """kind: 'up' | 'down'.

    Records the raw vote in FeedbackLog (kept separate from the derived profile
    so the signal can be re-derived / the algorithm adjusted later) and nudges
    the tag weights so the feedback takes effect immediately.
    """
    with db.get_conn() as conn:
        story = conn.execute("SELECT * FROM Story WHERE id=?", (story_id,)).fetchone()
        if story is None:
            return {}
        # Raw up/down signal, stored separately from the profile mutation below.
        conn.execute(
            "INSERT INTO FeedbackLog (story_id, kind) VALUES (?, ?)",
            (story_id, kind),
        )
        profile = get_user_profile()
        tag_weights = profile["tag_weights"]
        direction = 1.0 if kind == "up" else -1.0
        for tag in _story_tags(story):
            base = tag_weights.get(tag, 0.0)
            new = base + direction * config.FEEDBACK_ALPHA * 10  # scale to visible effect
            # clamp so weights stay in a sensible band
            tag_weights[tag] = max(-10.0, min(10.0, new))
        conn.execute(
            "UPDATE UserProfile SET tag_weights = ? WHERE id=1",
            (json.dumps(tag_weights),),
        )
        conn.commit()
    return profile


def feedback_log(limit: int = 200) -> list[dict]:
    """Raw 👍/👎 votes, kept separate from the profile for later tuning."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT story_id, kind, created_at FROM FeedbackLog "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
