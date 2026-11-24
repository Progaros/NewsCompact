"""Experimental media / outlet analysis endpoints (/outlets, /api/outlets/*)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from .. import media_analysis
from ..web import require_uid

router = APIRouter()


@router.get("/api/outlets")
def list_outlets(request: Request, days: int = Query(30, ge=1, le=365)):
    require_uid(request)
    return media_analysis.outlets_overview(days=days)


@router.post("/api/outlets/sample")
def sample_outlets(request: Request, count: int = Query(5, ge=1, le=15)):
    # Admin-only (enforced by _ADMIN_PREFIXES in app/api.py).
    require_uid(request)
    return media_analysis.run(max_articles=count, respect_budget=False)


@router.get("/api/outlets/{outlet:path}")
def get_outlet(outlet: str, request: Request, days: int = Query(30, ge=1, le=365)):
    require_uid(request)
    data = media_analysis.outlet_detail(outlet, days=days)
    if data is None:
        raise HTTPException(404, "outlet not found")
    return data
