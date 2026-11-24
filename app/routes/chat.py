"""Per-story chat: history, version switching, answers as background jobs (streamed as NDJSON), unread state."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import chat_history, chat_jobs, db, scoring, stories
from ..web import NO_CACHE, require_uid

router = APIRouter()

_CHAT_HISTORY_MAX = 20  # prior messages sent to the model


class ChatSend(BaseModel):
    message: str = Field(max_length=4000)
    web_search: bool = True
    edit_of: int | None = None  # user message this one replaces (as a new version)


class ChatSwitch(BaseModel):
    message_id: int


@router.get("/api/stories/{story_id}/chat")
def get_chat(story_id: int, request: Request):
    uid = require_uid(request)
    with db.get_conn() as conn:
        story_id = stories.resolve_id(conn, story_id)
        if not stories.exists(conn, story_id):
            raise HTTPException(404, "story not found")
        return chat_history.payload(conn, story_id, uid)


@router.post("/api/stories/{story_id}/chat/switch")
def switch_chat_version(story_id: int, body: ChatSwitch, request: Request):
    """Show another version of a message (and the latest replies that follow it)."""
    uid = require_uid(request)
    with db.get_conn() as conn:
        story_id = stories.resolve_id(conn, story_id)
        if not chat_history.switch(conn, story_id, uid, body.message_id):
            raise HTTPException(404, "message not found")
        return chat_history.payload(conn, story_id, uid)


@router.get("/api/chats")
def list_chats(request: Request):
    """The user's chats (newest first) with unread / in-progress state, for bubbles and the Chats tab."""
    uid = require_uid(request)
    jobs = chat_jobs.running(uid)
    with db.get_conn() as conn:
        chats = chat_history.overview(conn, uid)
    for c in chats:
        job = jobs.get(c["story_id"])
        c["running"] = job is not None
        c["started"] = job.started if job else None
    return {"chats": chats, "unread": sum(c["unread"] for c in chats), "running": len(jobs)}


@router.post("/api/stories/{story_id}/chat/read")
def mark_chat_read(story_id: int, request: Request):
    uid = require_uid(request)
    with db.get_conn() as conn:
        chat_history.mark_seen(conn, stories.resolve_id(conn, story_id), uid)
    return {"ok": True}


@router.post("/api/stories/{story_id}/chat/stop")
def stop_chat(story_id: int, request: Request):
    """Interrupt the answer in progress (what was generated so far is kept)."""
    uid = require_uid(request)
    with db.get_conn() as conn:
        story_id = stories.resolve_id(conn, story_id)
    job = chat_jobs.get(uid, story_id)
    if job is not None:
        job.stop.set()
    return {"stopping": job is not None}


def _ndjson_stream(job: chat_jobs.Job) -> StreamingResponse:
    async def lines():
        async for record in job.follow():
            yield json.dumps(record, ensure_ascii=False) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={**NO_CACHE, "X-Accel-Buffering": "no"})


@router.get("/api/stories/{story_id}/chat/stream")
def attach_chat(story_id: int, request: Request):
    """Re-attach to the answer in progress (all events so far, then live); 204 when none runs."""
    uid = require_uid(request)
    with db.get_conn() as conn:
        story_id = stories.resolve_id(conn, story_id)
    job = chat_jobs.get(uid, story_id)
    if job is None:
        return Response(status_code=204)
    return _ndjson_stream(job)


@router.post("/api/stories/{story_id}/chat")
def post_chat(story_id: int, body: ChatSend, request: Request):
    """Start an answer as a background job and stream it (NDJSON: steps, thinking, deltas, done/error).

    Disconnecting does not stop the job; POST .../chat/stop does.
    """
    uid = require_uid(request)
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "message must not be empty")
    with db.get_conn() as conn:
        story_id = stories.resolve_id(conn, story_id)
        story = stories.load_story(conn, story_id, uid)
        if story is None:
            raise HTTPException(404, "story not found")
        about = scoring.about_user(scoring.get_user_profile(conn, uid))
        try:
            job = chat_jobs.reserve(uid, story_id, text)
        except chat_jobs.Busy:
            raise HTTPException(409, "an answer for this story is still in progress") from None
        except chat_jobs.TooMany:
            raise HTTPException(429, "too many answers in progress") from None
        try:
            turn = chat_history.start_turn(conn, story_id, uid, text, body.edit_of, _CHAT_HISTORY_MAX)
        except Exception:
            chat_jobs.release(job)
            raise
        if turn is None:
            chat_jobs.release(job)
            raise HTTPException(404, "message to edit not found")
    session_id, question_id, history = turn
    chat_jobs.run(job, story, history, body.web_search, about, session_id, question_id)
    return _ndjson_stream(job)
