"""Scheduled ntfy reports: preview and send now (admins)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import config, reports
from ..web import require_uid

router = APIRouter()


def _report_kind(kind: str) -> str:
    if kind not in reports.KINDS:
        raise HTTPException(404, "unknown report")
    return kind


@router.get("/api/reports/{kind}")
def preview_report(kind: str, request: Request):
    return {"text": reports.render(_report_kind(kind), fresh=False, user_id=require_uid(request))}


@router.post("/api/reports/{kind}/send")
def send_report(kind: str, request: Request):
    if not config.HEADS_UP_NTFY_URL:
        raise HTTPException(409, "ntfy is not configured (NEWS_HEADS_UP_NTFY_URL)")
    sent, text = reports.send(_report_kind(kind), require_uid(request))
    if not sent:
        raise HTTPException(502, "no report text" if not text else "ntfy push failed")
    return {"ok": True, "text": text}
