"""Stories (ranked list, detail, read marks) and the heads-up brief."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from .. import clustering, config, db, heads_up, pipeline, scoring, stories
from ..web import current_user, private, require_uid

router = APIRouter()


@router.get("/api/stories")
def list_stories(request: Request, limit: int = Query(50, ge=1, le=1000), archived: bool = False,
                 q: str = Query("", max_length=100), tag: str = Query("", max_length=80),
                 hide_read: bool = False, keep: str = Query("", max_length=4000),
                 view: Literal["personal", "default"] = "personal"):
    """``hide_read`` drops stories marked read (not those updated since); ``keep`` = ids exempt from it.

    ``view=personal`` (signed-in only) ranks with the user's interests and settings.
    """
    user = current_user(request)
    uid = user["id"] if user else None
    personal = uid is not None and view == "personal"
    keep_ids = {int(x) for x in keep.split(",") if x.strip().isdigit()}
    with db.get_conn() as conn:
        profile = scoring.get_user_profile(conn, uid) if personal else scoring.DEFAULT_PROFILE
        ranked = scoring.ranked_stories(conn, profile, archived=archived, query=q.strip(), tag=tag.strip(),
                                        personal=personal, user_id=uid)
        hidden = 0
        if hide_read and personal and not archived:
            visible = [i for i in ranked if i["read"] != "read" or i["story"]["id"] in keep_ids]
            hidden, ranked = len(ranked) - len(visible), visible
        page = ranked[:limit]
        exclude = scoring.excluded_feeds(profile)
        ids = [i["story"]["id"] for i in page]
        arts = stories.articles_by_story(conn, ids, exclude)
        related = clustering.related_stories(conn, ids, exclude=exclude)
        totals = pipeline.counts(conn)
    return {
        "count": len(ranked),
        "hidden_read": hidden,
        "pending": totals["pending"],
        "last_article": totals["last_article"],
        "latest": totals["latest_news"],
        "active_days": config.STORY_ACTIVE_DAYS,
        "view": "personal" if personal else "default",
        "stories": [private(stories.story_payload(i, arts[i["story"]["id"]], related[i["story"]["id"]]),
                            uid is not None) for i in page],
    }


@router.get("/api/stories/pulse")
def stories_pulse(since: str = Query("", max_length=32)):
    """Cheap poll target: how many stories got news after ``since`` (a news_at value) and the queue size."""
    with db.get_conn() as conn:
        new, latest = conn.execute("SELECT count(*), MAX(news_at) FROM Story WHERE headline IS NOT NULL "
                                   "AND news_at > ?", (since,)).fetchone() if since else (0, None)
        pending = pipeline.pending_count(conn)
    return {"new": new, "latest": latest, "pending": pending}


@router.get("/api/stories/{story_id}")
def get_story(story_id: int, request: Request, view: Literal["personal", "default"] = "personal"):
    user = current_user(request)
    uid = user["id"] if user else None
    with db.get_conn() as conn:
        payload = stories.load_story(conn, stories.resolve_id(conn, story_id), uid, personal=view == "personal")
    if payload is None:
        raise HTTPException(404, "story not found")
    return private(payload, uid is not None)


@router.post("/api/stories/{story_id}/read")
def mark_read(story_id: int, request: Request):
    uid = require_uid(request)
    with db.get_conn() as conn:
        if not stories.mark_read(conn, uid, story_id):
            raise HTTPException(404, "story not found")
    return {"ok": True, "read": "read"}


@router.delete("/api/stories/{story_id}/read")
def mark_unread(story_id: int, request: Request):
    uid = require_uid(request)
    with db.get_conn() as conn:
        stories.mark_unread(conn, uid, story_id)
    return {"ok": True, "read": None}


@router.get("/api/heads-up")
def get_heads_up(request: Request, view: Literal["personal", "default"] = "default"):
    """Never waits for the LLM: ``generating`` = a newer brief is on its way (poll again)."""
    user = current_user(request)
    key = heads_up.user_key(user["id"]) if view == "personal" and user else heads_up.DEFAULT
    st = heads_up.status(key)
    return {**st, "text": st["text"] or ("Generating the brief…" if st["generating"] else "No brief available yet."),
            "view": "default" if key == heads_up.DEFAULT else "personal"}
