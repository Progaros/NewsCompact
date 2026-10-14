"""Per-user 👍/👎 feedback and ⚙ Preferences (profile)."""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import config, db, heads_up, reports, scoring
from ..web import current_user, require_uid

router = APIRouter()


class Feedback(BaseModel):
    story_id: int
    kind: str | None = None


class ReportSchedule(BaseModel):
    kind: Literal["brief", "pipeline"]
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    days: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=lambda: list(range(7)), max_length=7)
    enabled: bool = True


class Preferences(BaseModel):
    interest_regions: list[Annotated[str, Field(max_length=80)]] | None = Field(None, max_length=50)
    locations: list[Annotated[str, Field(max_length=80)]] | None = Field(None, max_length=50)
    min_quality: float | None = Field(None, ge=0, le=10)
    ntfy_reports: list[ReportSchedule] | None = Field(None, max_length=20)
    excluded_feeds: list[int] | None = Field(None, max_length=2000)
    about: str | None = Field(None, max_length=1000)
    home_city: str | None = Field(None, max_length=80)
    home_country: str | None = Field(None, max_length=80)
    language: str | None = Field(None, pattern=r"^\s*([A-Za-z]{2,3})?\s*$")


class ProfileUpdate(BaseModel):
    preferences: Preferences = Field(default_factory=Preferences)
    tag_weights: dict[Annotated[str, Field(max_length=80)], float] | None = Field(None, max_length=500)
    source_weights: dict[Annotated[str, Field(max_length=200)], Annotated[float, Field(ge=0, le=1)]] | None = (
        Field(None, max_length=100))
    feed_weights: dict[int, Annotated[float, Field(ge=0, le=1)]] | None = Field(None, max_length=2000)


@router.post("/api/feedback")
def post_feedback(body: Feedback, request: Request):
    kind = None if body.kind in (None, "", "none") else body.kind
    if kind not in ("up", "down", None):
        raise HTTPException(400, "kind must be 'up', 'down' or null")
    profile = scoring.apply_feedback(body.story_id, kind, require_uid(request))
    if profile is None:
        raise HTTPException(404, "story not found")
    return {"ok": True, "vote": kind, "profile": profile}


@router.get("/api/feedback")
def get_feedback(request: Request, limit: int = Query(200, ge=1, le=5000)):
    return {"ok": True, "log": scoring.feedback_log(limit, require_uid(request))}


@router.get("/api/profile")
def get_profile(request: Request):
    user = current_user(request)
    uid = require_uid(request)
    with db.get_conn() as conn:
        profile = scoring.get_user_profile(conn, uid)
        out = {
            **profile,
            "user": user,
            "home_defaults": {"home_city": config.USER_CITY, "home_country": config.USER_COUNTRY,
                              "language": config.USER_LANGUAGE},
            "source_trust": scoring.trust_weights(conn, profile),
            "feed_trust": scoring.feed_trust_list(conn, profile),
        }
    if user["is_admin"]:  # ntfy pushes go to the server's one topic
        out["ntfy_reports"] = reports.schedules(profile)
        out["ntfy"] = {"enabled": bool(config.HEADS_UP_NTFY_URL), "kinds": reports.KINDS,
                       "server_time": dt.datetime.now().strftime("%H:%M")}
    return out


@router.put("/api/profile")
def put_profile(body: ProfileUpdate, request: Request):
    user = current_user(request)
    uid = require_uid(request)
    prefs = body.preferences.model_dump(exclude_none=True)
    for key in ("interest_regions", "locations"):
        if key in prefs:
            prefs[key] = [v.strip() for v in prefs[key] if v.strip()]
    if not user["is_admin"]:
        prefs.pop("ntfy_reports", None)
    for s in prefs.get("ntfy_reports", []):
        s["days"] = sorted(set(s["days"]))
    if "excluded_feeds" in prefs:
        prefs["excluded_feeds"] = sorted(set(prefs["excluded_feeds"]))
    if "about" in prefs:
        prefs["about"] = prefs["about"].strip()
    for key in ("home_city", "home_country", "language"):
        if key in prefs:
            prefs[key] = prefs[key].strip().lower() if key == "language" else prefs[key].strip()
    scoring.update_profile(prefs, body.tag_weights, body.source_weights, body.feed_weights, user_id=uid)
    heads_up.invalidate(heads_up.user_key(uid))  # ranking or reader description may have changed
    return get_profile(request)
