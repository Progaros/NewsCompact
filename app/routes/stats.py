"""Model usage statistics for signed-in users (/stats)."""
from __future__ import annotations

from fastapi import APIRouter, Request, Query

from .. import stats
from ..web import require_uid

router = APIRouter()


@router.get("/api/stats")
def get_stats(request: Request, days: int = Query(30, ge=1, le=365)):
    require_uid(request)
    return stats.summary(days=days)
