"""Feed management (admins only, enforced by the middleware in app/api.py)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import feeds
from ..web import require_http_url

router = APIRouter()


class FeedCreate(BaseModel):
    title: str = Field(max_length=300)
    xml_url: str = Field(max_length=2000)
    category: str | None = Field(None, max_length=200)
    site_url: str | None = Field(None, max_length=2000)


class FeedUpdate(BaseModel):
    enabled: int | None = None
    title: str | None = Field(None, max_length=300)
    category: str | None = Field(None, max_length=200)
    xml_url: str | None = Field(None, max_length=2000)
    site_url: str | None = Field(None, max_length=2000)  # "" = none
    trust: float | None = Field(None, ge=0, le=1)  # null = the category's


class CategoryUpdate(BaseModel):
    category: str | None = Field(max_length=200)
    enabled: int | None = None
    trust: float | None = Field(None, ge=0, le=1)  # null = default


@router.get("/api/feeds")
def list_feeds():
    return feeds.list_with_stats()


@router.post("/api/feeds")
def add_feed(body: FeedCreate):
    url = require_http_url(body.xml_url)
    if not body.title.strip():
        raise HTTPException(422, "title must not be empty")
    try:
        feed_id = feeds.add(body.title.strip(), url, body.site_url, (body.category or "").strip() or None)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True, "id": feed_id}


@router.get("/api/feeds/preview")
def preview_feed(url: str):
    try:
        return {"title": feeds.preview_title(require_http_url(url))}
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/api/feeds/category")
def update_category(body: CategoryUpdate):
    if body.enabled is None and "trust" not in body.model_fields_set:
        raise HTTPException(422, "nothing to change")
    out: dict[str, Any] = {}
    if body.enabled is not None:
        out["affected"] = feeds.set_category_enabled(body.category, bool(body.enabled))
    if "trust" in body.model_fields_set:
        feeds.set_category_trust(body.category, body.trust)
        out["trust"] = body.trust
    return out


@router.patch("/api/feeds/{feed_id}")
def update_feed(feed_id: int, body: FeedUpdate):
    changes: dict[str, Any] = {}
    if body.enabled is not None:
        changes["enabled"] = 1 if body.enabled else 0
    if body.title is not None:
        if not body.title.strip():
            raise HTTPException(422, "title must not be empty")
        changes["title"] = body.title.strip()
    if body.category is not None:
        changes["category"] = body.category.strip() or None
    if body.xml_url is not None:
        changes["xml_url"] = require_http_url(body.xml_url)
    if body.site_url is not None:
        changes["site_url"] = require_http_url(body.site_url) if body.site_url.strip() else None
    if "trust" in body.model_fields_set:
        changes["trust"] = body.trust
    try:
        found = feeds.update(feed_id, changes)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    if not found:
        raise HTTPException(404, "feed not found")
    return {"ok": True}


@router.delete("/api/feeds/{feed_id}")
def delete_feed(feed_id: int):
    if not feeds.delete(feed_id):
        raise HTTPException(404, "feed not found")
    return {"ok": True}
