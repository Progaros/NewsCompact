"""HTTP helpers shared by app/api.py and the routers in app/routes/."""
from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import HTTPException, Request

from . import config, ingest

NO_CACHE = {"Cache-Control": "no-cache"}


def is_https(request: Request) -> bool:
    """Served over HTTPS: configured public base, or the request scheme (X-Forwarded-Proto via a trusted proxy)."""
    return config.PUBLIC_BASE.startswith("https") or request.url.scheme == "https"


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
        payload.update(read=None, read_at=None, changes=[], vote=None)
    return payload


def _is_trusted_proxy(peer: str) -> bool:
    """True only when ``peer`` is explicitly allowed by ``config.FORWARDED_ALLOW_IPS``."""
    entries = [p.strip() for p in (config.FORWARDED_ALLOW_IPS or "").split(",") if p.strip()]
    if "*" in entries:
        return True
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for item in entries:
        try:
            if "/" in item:
                if peer_ip in ipaddress.ip_network(item, strict=False):
                    return True
            elif peer_ip == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def _valid_ip(candidate: str) -> str | None:
    try:
        return str(ipaddress.ip_address(candidate.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """Right-most valid X-Forwarded-For hop when connected via a configured trusted proxy, else peer IP."""
    peer = request.client.host if request.client else "unknown"
    if _is_trusted_proxy(peer):
        fwd = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if fwd:
            ip = _valid_ip(fwd[-1])
            if ip:
                return ip
        real = request.headers.get("x-real-ip", "").strip()
        if real:
            ip = _valid_ip(real)
            if ip:
                return ip
    return peer


def require_http_url(url: str) -> str:
    url = (url or "").strip()
    if not ingest.is_http_url(url):
        raise HTTPException(422, "URL must start with http:// or https://")
    return url
