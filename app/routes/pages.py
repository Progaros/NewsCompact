"""Static pages (HTML shells; data comes from the JSON API)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi import Path as FastPath
from fastapi.responses import FileResponse, RedirectResponse

from ..web import NO_CACHE
from .share import SHARE_TOKEN

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

router = APIRouter()


def _page(name: str) -> FileResponse:
    return FileResponse(FRONTEND / name, headers=NO_CACHE)


@router.get("/")
def index_page():
    return _page("index.html")


# Browsers ask for /favicon.ico on pages without an icon link (raw JSON, images).
@router.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/logo.svg", status_code=301)


@router.get("/feeds")
def feeds_page():
    return _page("feeds.html")


@router.get("/list")
def list_page():
    return _page("list.html")


@router.get("/admin")
def admin_page():
    return _page("admin.html")


@router.get("/s/{story_id}")
def shared_story_page(story_id: int):
    return _page("share.html")


@router.get("/p/{token}")
def shared_snapshot_page(token: str = FastPath(pattern=SHARE_TOKEN)):
    return _page("share.html")
