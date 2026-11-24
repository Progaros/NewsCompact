"""Podcast endpoints: public 'For everyone' podcasts + signed-in personal podcasts & RSS feeds."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path as FastPath, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .. import config, podcast, tts
from ..web import current_user, require_uid

router = APIRouter()

FEED_TOKEN = r"^[A-Za-z0-9_\-]{16,64}$"


class GeneratePodcastRequest(BaseModel):
    duration_minutes: int = Field(15, ge=1, le=180)
    view: Literal["personal", "default"] = "personal"
    language: Literal["auto", "de", "en"] = "auto"
    style: Literal["informative", "entertaining", "analytical", "briefing"] = "informative"
    format: Literal["solo", "dialogue"] = "solo"
    max_stories: int | None = Field(None, ge=3, le=50)
    notify: bool = True
    script_only: bool = False
    fact_check: bool = True


# --- Public podcasts (no auth; strictly view='default' & status='ready') -----
@router.get("/api/podcasts/public")
def list_public_podcasts():
    return {
        "episodes": podcast.list_public_episodes(),
        "feed_path": "/api/podcasts/public/feed.xml",
    }


@router.get("/api/podcasts/public/feed.xml")
def get_public_podcast_feed(request: Request):
    base = config.PUBLIC_BASE.rstrip("/") if config.PUBLIC_BASE else str(request.base_url).rstrip("/")
    xml = podcast.public_rss_xml(base)
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@router.get("/api/podcasts/public/{episode_id}")
def get_public_podcast(episode_id: int):
    ep = podcast.get_public_episode(episode_id, include_script=True)
    if not ep:
        raise HTTPException(404, "episode not found")
    return {"episode": ep}


@router.get("/api/podcasts/public/{episode_id}/audio")
def get_public_podcast_audio(episode_id: int):
    found = podcast.public_audio_file_for(episode_id)
    if not found:
        raise HTTPException(404, "audio not available")
    path, mime = found
    return FileResponse(path, media_type=mime, filename=path.name)


# --- Signed-in user podcasts -------------------------------------------------
@router.get("/api/podcasts")
def list_podcasts(request: Request):
    uid = require_uid(request)
    user = current_user(request)
    is_admin = bool(user and user.get("is_admin"))
    token = podcast.get_or_create_feed_token(uid)
    return {
        "episodes": podcast.list_episodes(uid),
        "tts": tts.status(),
        "ntfy_enabled": bool(config.HEADS_UP_NTFY_URL) and is_admin,
        "can_publish_public": is_admin,
        "feed_path": f"/api/podcasts/feed/{token}.xml",
        "public_feed_path": "/api/podcasts/public/feed.xml",
    }


@router.post("/api/podcasts/generate", status_code=202)
def generate_podcast(body: GeneratePodcastRequest, request: Request):
    uid = require_uid(request)
    user = current_user(request)
    is_admin = bool(user and user.get("is_admin"))
    if body.view == "default" and not is_admin:
        raise HTTPException(403, "only admins can publish public 'For everyone' podcasts")
    try:
        ep = podcast.start_generation(
            uid,
            duration_minutes=body.duration_minutes,
            view=body.view,
            language=body.language,
            style=body.style,
            fmt=body.format,
            max_stories=body.max_stories,
            notify=body.notify and is_admin,
            script_only=body.script_only,
            fact_check=body.fact_check,
            background=True,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"episode": ep}


@router.post("/api/podcasts/feed/rotate")
def rotate_podcast_feed(request: Request):
    token = podcast.rotate_feed_token(require_uid(request))
    return {"feed_path": f"/api/podcasts/feed/{token}.xml"}


@router.get("/api/podcasts/feed/{token}.xml")
def get_podcast_feed(request: Request, token: str = FastPath(pattern=FEED_TOKEN)):
    uid = podcast.user_for_feed_token(token)
    if uid is None:
        raise HTTPException(404, "feed not found")
    base = config.PUBLIC_BASE.rstrip("/") if config.PUBLIC_BASE else str(request.base_url).rstrip("/")
    xml = podcast.rss_xml(uid, token, base)
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@router.get("/api/podcasts/feed/{token}/audio/{episode_id}")
def get_podcast_feed_audio(episode_id: int, token: str = FastPath(pattern=FEED_TOKEN)):
    uid = podcast.user_for_feed_token(token)
    if uid is None:
        raise HTTPException(404, "feed not found")
    found = podcast.audio_file_for(episode_id, uid)
    if not found:
        raise HTTPException(404, "audio not available")
    path, mime = found
    return FileResponse(path, media_type=mime, filename=path.name)


@router.post("/api/podcasts/{episode_id}/stop")
def stop_podcast(episode_id: int, request: Request):
    if not podcast.stop_generation(episode_id, require_uid(request)):
        raise HTTPException(404, "no active episode to stop")
    return {"ok": True}


@router.get("/api/podcasts/{episode_id}")
def get_podcast(episode_id: int, request: Request):
    ep = podcast.get_episode(episode_id, require_uid(request), include_script=True)
    if not ep:
        raise HTTPException(404, "episode not found")
    return {"episode": ep}


@router.get("/api/podcasts/{episode_id}/audio")
def get_podcast_audio(episode_id: int, request: Request):
    found = podcast.audio_file_for(episode_id, require_uid(request))
    if not found:
        raise HTTPException(404, "audio not available")
    path, mime = found
    return FileResponse(path, media_type=mime, filename=path.name)


@router.delete("/api/podcasts/{episode_id}")
def delete_podcast(episode_id: int, request: Request):
    if not podcast.delete_episode(episode_id, require_uid(request)):
        raise HTTPException(404, "episode not found")
    return {"ok": True}
