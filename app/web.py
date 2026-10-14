"""HTTP helpers shared by app/api.py and the routers in app/routes/."""
from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import HTTPException, Request

from . import ingest

NO_CACHE = {"Cache-Control": "no-cache"}


def current_user(request: Request) -> dict[str, Any] | None:
    """Signed-in user (set by the auth middleware); None for anonymous visitors."""
    return getattr(request.state, "user", None)


def require_uid(request: Request) -> int:
    """Id of the signed-in user on routes the middleware already restricted to users."""
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "unauthenticated")
    return user["id"]


def private(payload: dict[str, Any], owner: bool) -> dict[str, Any]:
    """A user's read/vote state is not part of the public read-only view."""
    if not owner:
        payload.update(read=None, read_at=None, read_summary=None, vote=None)
    return payload


def client_ip(request: Request) -> str:
    """Right-most X-Forwarded-For hop when connected via a local/private proxy, else peer IP."""
    peer = request.client.host if request.client else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
        trusted_proxy = peer_ip.is_private or peer_ip.is_loopback
    except ValueError:
        trusted_proxy = False
    if trusted_proxy:
        fwd = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if fwd:
            return fwd[-1]
        real = request.headers.get("x-real-ip", "").strip()
        if real:
            return real
    return peer


def require_http_url(url: str) -> str:
    url = (url or "").strip()
    if not ingest.is_http_url(url):
        raise HTTPException(422, "URL must start with http:// or https://")
    return url
