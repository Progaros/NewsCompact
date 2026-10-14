"""Pipeline monitoring: the /list article table, health, live events (SSE), manual refresh."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import bus, db, pipeline, worker
from ..web import NO_CACHE

router = APIRouter()

_SSE_MAX_CLIENTS = 50  # /api/events is public; each stream holds a connection


@router.get("/api/articles")
def list_articles(limit: int = Query(100, ge=1, le=5000), order: str = "newest",
                  status: str = Query("", pattern="^(|assigned|pending|skipped|dead|duplicate)$"),
                  q: str = Query("", max_length=100)):
    result = pipeline.article_list(limit=limit, order=order, status=status, query=q.strip())
    return {"count": result["count"], "order": order, "worker": worker.status, "articles": result["articles"]}


@router.get("/api/health")
def health():
    with db.get_conn() as conn:
        totals = pipeline.counts(conn)
    return {"ok": True, "articles": totals["articles"], "stories": totals["stories"], "pending": totals["pending"],
            "dead_letter": totals["dead_letter"], "worker": worker.status}


@router.post("/api/refresh")
def refresh():
    return worker.refresh()


@router.get("/api/events")
async def events(request: Request):
    if bus.subscriber_count() >= _SSE_MAX_CLIENTS:
        return JSONResponse(status_code=503, content={"detail": "too many live connections"})

    async def stream():
        q = bus.subscribe()
        try:
            yield bus.sse({"type": "hello"})
            while not await request.is_disconnected():
                try:
                    record = await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if record is None:  # server shutting down
                    break
                yield bus.sse(record)
        finally:
            bus.unsubscribe(q)

    # X-Accel-Buffering: nginx would otherwise buffer the stream.
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={**NO_CACHE, "X-Accel-Buffering": "no"})
