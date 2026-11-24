"""Static pages (HTML shells; data comes from the JSON API)."""
from __future__ import annotations

import html
from pathlib import Path

from fastapi import APIRouter
from fastapi import Path as FastPath
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config
from ..web import NO_CACHE
from .share import SHARE_TOKEN

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

router = APIRouter()


def _analytics_tag() -> str:
    if not config.ANALYTICS_SCRIPT_URL:
        return ""
    site = (f' data-website-id="{html.escape(config.ANALYTICS_WEBSITE_ID)}"'
            if config.ANALYTICS_WEBSITE_ID else "")
    return f'  <script async defer src="{html.escape(config.ANALYTICS_SCRIPT_URL)}"{site}></script>\n'


def _page(name: str) -> FileResponse | HTMLResponse:
    tag = _analytics_tag()
    if not tag:
        return FileResponse(FRONTEND / name, headers=NO_CACHE)
    page = (FRONTEND / name).read_text(encoding="utf-8").replace("</head>", tag + "</head>", 1)
    return HTMLResponse(page, headers=NO_CACHE)


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
    return RedirectResponse("/analytics#articles", status_code=303)


@router.get("/chats")
def chats_page():
    return _page("chats.html")


@router.get("/admin")
def admin_page():
    return RedirectResponse("/settings#users", status_code=303)


@router.get("/settings")
def settings_page():
    return _page("settings.html")


@router.get("/stats")
def stats_page():
    return RedirectResponse("/analytics#usage", status_code=303)


@router.get("/podcasts")
def podcasts_page():
    return _page("podcasts.html")


@router.get("/outlets")
def outlets_page(outlet: str = ""):
    suffix = f"?outlet={outlet}#outlets" if outlet else "#outlets"
    return RedirectResponse(f"/analytics{suffix}", status_code=303)


@router.get("/analytics")
def analytics_page():
    return _page("analytics.html")


@router.get("/s/{story_id}")
def shared_story_page(story_id: int):
    return _page("share.html")


@router.get("/p/{token}")
def shared_snapshot_page(token: str = FastPath(pattern=SHARE_TOKEN)):
    return _page("share.html")
