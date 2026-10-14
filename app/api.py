"""FastAPI app: authorization + security middleware, routers (app/routes/), static frontend."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, bus, config, db, reports, worker
from .routes import ROUTERS
from .routes.pages import FRONTEND

_ADMIN_PREFIXES = ("/feeds", "/api/feeds", "/api/refresh", "/api/reports", "/api/admin", "/admin")


def _admin_only(path: str) -> bool:
    """Shared-instance management: feeds, refresh, ntfy reports, user administration."""
    return any(path == p or path.startswith(p + "/") for p in _ADMIN_PREFIXES)


def _is_public(request: Request) -> bool:
    """Read-only paths reachable without a session (chat history is not public)."""
    path = request.url.path
    if path in ("/", "/list", "/api/auth/login", "/api/auth/session") or path.startswith("/static/"):
        return True
    if request.method != "GET":
        return False
    if path in ("/api/stories", "/api/articles", "/api/heads-up", "/api/health", "/api/events"):
        return True
    if path.startswith(("/s/", "/p/", "/api/share/")):
        return True
    return path.startswith("/api/stories/") and "/chat" not in path


_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _same_origin(request: Request) -> bool:
    """Browsers send Origin on cross-site writes; non-browser clients usually send none."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    allowed = {request.headers.get("host", ""), urlsplit(config.PUBLIC_BASE).netloc}
    return urlsplit(origin).netloc in allowed


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    if not auth.login_configured():
        raise RuntimeError("no login configured: set NEWS_AUTH_PASSWORD (or give an admin a password)")
    worker.start()
    reports.start_scheduler()
    try:
        yield
    finally:
        bus.close_all()
        reports.stop_scheduler()
        await asyncio.to_thread(worker.stop)


def create_app() -> FastAPI:
    app = FastAPI(title="AI News Aggregator", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        user = request.state.user = await asyncio.to_thread(
            auth.current_user, request.cookies.get(config.AUTH_COOKIE))
        if not _is_public(request):
            if user is None:
                if request.url.path in ("/feeds", "/admin"):
                    return RedirectResponse("/", status_code=303)
                return JSONResponse(status_code=401, content={"detail": "unauthenticated"})
            if _admin_only(request.url.path) and not user["is_admin"]:
                if request.url.path in ("/feeds", "/admin"):
                    return RedirectResponse("/", status_code=303)
                return JSONResponse(status_code=403, content={"detail": "admins only"})
        return await call_next(request)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        # Cross-site writes (CSRF).
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
            return JSONResponse(status_code=403, content={"detail": "cross-origin request rejected"})
        # SameSite=Lax still sends the cookie on cross-site top-level GETs (e.g. /api/feeds/preview).
        if (request.url.path.startswith("/api/") and not _is_public(request)
                and request.headers.get("sec-fetch-site") == "cross-site"):
            return JSONResponse(status_code=403, content={"detail": "cross-site request rejected"})
        resp = await call_next(request)
        resp.headers.update(_SECURITY_HEADERS)
        if config.PUBLIC_BASE.startswith("https"):
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    for router in ROUTERS:
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
    return app


app = create_app()
