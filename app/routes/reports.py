"""Scheduled ntfy reports: preview and send now (admins)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config, reports
from ..web import require_uid

router = APIRouter()


class SendReportOptions(BaseModel):
    view: Literal["personal", "default"] | None = None
    max_stories: int | None = Field(None, ge=3, le=50)
    duration_minutes: int | None = Field(None, ge=1, le=180)
    language: Literal["auto", "de", "en"] | None = None
    style: Literal["informative", "entertaining", "analytical", "briefing"] | None = None
    format: Literal["solo", "dialogue"] | None = None
    fact_check: bool | None = None
    notify: bool | None = None
    script_only: bool | None = None


def _report_kind(kind: str) -> str:
    if kind not in reports.KINDS:
        raise HTTPException(404, "unknown report")
    return kind


@router.get("/api/reports/{kind}")
def preview_report(kind: str, request: Request):
    return {"text": reports.render(_report_kind(kind), fresh=False, user_id=require_uid(request))}


@router.post("/api/reports/{kind}/send")
def send_report(kind: str, request: Request, body: SendReportOptions | None = Body(default=None)):
    k = _report_kind(kind)
    if k != "podcast" and not config.HEADS_UP_NTFY_URL:
        raise HTTPException(409, "ntfy is not configured (NEWS_HEADS_UP_NTFY_URL)")
    opts = body or SendReportOptions()
    sent, text = reports.send(
        k,
        require_uid(request),
        duration_minutes=opts.duration_minutes,
        language=opts.language,
        style=opts.style,
        fmt=opts.format,
        view=opts.view,
        max_stories=opts.max_stories,
        fact_check=opts.fact_check,
        notify=opts.notify,
        script_only=opts.script_only,
    )
    if not sent:
        raise HTTPException(502, text or "no report text")
    return {"ok": True, "text": text}
