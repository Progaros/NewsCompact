"""FastAPI app: authorization + security middleware, routers (app/routes/), static frontend."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, bus, chat_jobs, config, db, reports, worker
from .routes import ROUTERS
from .routes.pages import FRONTEND
from .web import is_https

_ADMIN_PREFIXES = ("/feeds", "/api/feeds", "/api/refresh", "/api/reports", "/api/admin", "/admin")
_SIGNED_IN_PAGES = ("/feeds", "/admin", "/list", "/chats")  # browsers are sent to / instead of a JSON 401


def _admin_only(path: str) -> bool:
    """Shared-instance management: feeds, refresh, ntfy reports, user administration."""
    return any(path == p or path.startswith(p + "/") for p in _ADMIN_PREFIXES)


def _is_public(request: Request) -> bool:
    """Read-only paths reachable without a session (chat history is not public)."""
    path = request.url.path
    if path in ("/", "/favicon.ico", "/api/auth/login", "/api/auth/session") or path.startswith("/static/"):
        return True
    if request.method != "GET":
        return False
    if path in ("/api/stories", "/api/heads-up", "/api/health"):
        return True
    if path.startswith(("/s/", "/p/", "/api/share/")):
        return True
    return path.startswith("/api/stories/") and "/chat" not in path


_ANALYTICS_ORIGIN = ("{0.scheme}://{0.netloc}".format(urlsplit(config.ANALYTICS_SCRIPT_URL))
                     if config.ANALYTICS_SCRIPT_URL else "")
_EXTRA_SRC = f" {_ANALYTICS_ORIGIN}" if _ANALYTICS_ORIGIN else ""
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        f"default-src 'self'; script-src 'self'{_EXTRA_SRC}; style-src 'self'; img-src 'self' data: https:; "
        f"connect-src 'self'{_EXTRA_SRC}; object-src 'none'; base-uri 'none'; form-action 'self'; "
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
    if origin == "null":  # sandboxed iframes, data: URLs, privacy-redirected requests
        return False
    allowed = {h for h in (request.headers.get("host", ""), urlsplit(config.PUBLIC_BASE).netloc) if h}
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
        chat_jobs.stop_all()
        reports.stop_scheduler()
        await asyncio.to_thread(worker.stop)


def create_app() -> FastAPI:
    app = FastAPI(title="AI News Aggregator", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        token = request.cookies.get(config.AUTH_COOKIE)
        static = path.startswith("/static/") or path == "/favicon.ico"
        user = request.state.user = (None if static or not token
                                     else await asyncio.to_thread(auth.current_user, token))
        if not _is_public(request):
            if user is None:
                if request.url.path in _SIGNED_IN_PAGES:
                    return RedirectResponse("/", status_code=303)
                return JSONResponse(status_code=401, content={"detail": "unauthenticated"})
            if _admin_only(request.url.path) and not user["is_admin"]:
                if request.url.path in _SIGNED_IN_PAGES:
                    return RedirectResponse("/", status_code=303)
                return JSONResponse(status_code=403, content={"detail": "admins only"})
        return await call_next(request)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        # Cross-site writes (CSRF); same-site covers sibling subdomains, which share the Lax cookie.
        if request.method not in ("GET", "HEAD", "OPTIONS") and (
                not _same_origin(request)
                or request.headers.get("sec-fetch-site") in ("cross-site", "same-site")):
            return JSONResponse(status_code=403, content={"detail": "cross-origin request rejected"})
        # SameSite=Lax still sends the cookie on cross-site top-level GETs (e.g. /api/feeds/preview).
        if (request.url.path.startswith("/api/") and not _is_public(request)
                and request.headers.get("sec-fetch-site") == "cross-site"):
            return JSONResponse(status_code=403, content={"detail": "cross-site request rejected"})
        resp = await call_next(request)
        resp.headers.update(_SECURITY_HEADERS)
        if is_https(request):
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    for router in ROUTERS:
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
    return app


app = create_app()
