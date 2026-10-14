"""Share links for briefs and chat answers (``/p/<token>``)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as FastPath
from pydantic import BaseModel

from .. import share
from ..web import require_uid

SHARE_TOKEN = r"^[A-Za-z0-9_-]{8,64}$"

router = APIRouter()


class ShareCreate(BaseModel):
    kind: Literal["brief", "chat"]
    view: Literal["personal", "default"] = "default"  # brief
    story_id: int | None = None  # chat
    message_id: int | None = None  # chat: the assistant answer


@router.post("/api/share")
def create_share(body: ShareCreate, request: Request):
    uid = require_uid(request)
    if body.kind == "brief":
        personal = body.view == "personal"
        payload = share.brief_snapshot(uid, personal)
        if payload is None:
            raise HTTPException(409, "no brief yet")
    else:
        personal = True
        payload = share.answer_snapshot(uid, body.story_id, body.message_id)
        if payload is None:
            raise HTTPException(404, "answer not found")
    return {"url": share.create(uid, body.kind, payload, personal)}


@router.get("/api/share/{token}")
def get_share(token: str = FastPath(pattern=SHARE_TOKEN)):
    link = share.get(token)
    if link is None:
        raise HTTPException(404, "link not found")
    return link


@router.get("/api/share")
def list_shares(request: Request):
    """The signed-in user's own share links, newest first."""
    return {"links": share.list_links(require_uid(request))}


@router.delete("/api/share/{token}")
def revoke_share(request: Request, token: str = FastPath(pattern=SHARE_TOKEN)):
    if not share.revoke(require_uid(request), token):
        raise HTTPException(404, "link not found")
    return {"ok": True}
