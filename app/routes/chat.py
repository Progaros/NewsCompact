"""Per-story chat: history, version switching, streamed answers (NDJSON)."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import chat, chat_history, db, scoring, stories
from ..web import NO_CACHE, require_uid

log = logging.getLogger("news.chat")

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


@router.post("/api/stories/{story_id}/chat")
def post_chat(story_id: int, body: ChatSend, request: Request):
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
        turn = chat_history.start_turn(conn, story_id, uid, text, body.edit_of, _CHAT_HISTORY_MAX)
        if turn is None:
            raise HTTPException(404, "message to edit not found")
    session_id, question_id, history = turn

    def ndjson(record: dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False) + "\n"

    def events():
        """NDJSON: agent steps, thinking and answer deltas, then done/error."""
        parts: list[str] = []
        steps: list[dict] = []
        saved = False

        def save(stopped: bool = False) -> None:
            nonlocal saved
            if not saved:
                saved = True
                reply = "".join(parts).strip()
                for s in steps:
                    if s.get("status") == "running":
                        if s.get("kind") == "thinking":
                            s.update(status="ok", title="Thought (interrupted)")
                        else:
                            s.update(status="warn", title=f"{s['title']} (stopped)")
                if stopped:
                    chat.add_step(steps, "stopped",
                                  f"Stopped — answer interrupted{' (partial)' if reply else ''}",
                                  status="warn")
                chat_history.save_reply(session_id, question_id, reply, steps)

        try:
            for kind, payload in chat.stream(story, history, text, body.web_search, steps, about=about):
                if kind == "delta":
                    parts.append(payload)
                yield ndjson({"type": kind, "step": payload} if kind == "step" else {"type": kind, "text": payload})
            save()
            yield ndjson({"type": "done"})
        except Exception as e:  # noqa: BLE001
            log.exception("chat failed for story %s", story_id)
            step = chat.add_step(steps, "error", "Answer failed", status="error", detail=chat.error_text(e))
            save()
            yield ndjson({"type": "step", "step": step})
            yield ndjson({"type": "error", "text": f"chat failed: {type(e).__name__}"})
        finally:
            # Also runs when the client disconnects mid-answer: keep what was generated.
            save(stopped=True)

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={**NO_CACHE, "X-Accel-Buffering": "no"})
