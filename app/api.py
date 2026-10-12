"""FastAPI app: REST endpoints, SSE stream, static frontend."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, bus, chat, clustering, config, db, heads_up, ingest, pipeline, scoring, worker

_FRONTEND = Path(__file__).with_name("frontend")
_NO_CACHE = {"Cache-Control": "no-cache"}


# --- request models ----------------------------------------------------------
class Feedback(BaseModel):
    story_id: int
    kind: str


class Preferences(BaseModel):
    interest_regions: list[Annotated[str, Field(max_length=80)]] | None = Field(None, max_length=50)
    locations: list[Annotated[str, Field(max_length=80)]] | None = Field(None, max_length=50)
    min_quality: float | None = Field(None, ge=0, le=10)


class ProfileUpdate(BaseModel):
    preferences: Preferences = Field(default_factory=Preferences)
    tag_weights: dict[Annotated[str, Field(max_length=80)], float] | None = Field(None, max_length=500)


class FeedCreate(BaseModel):
    title: str
    xml_url: str
    category: str | None = None
    site_url: str | None = None


class FeedUpdate(BaseModel):
    enabled: int | None = None
    title: str | None = None
    category: str | None = None
    xml_url: str | None = None


class CategoryUpdate(BaseModel):
    category: str | None
    enabled: int


class Login(BaseModel):
    password: str = Field(max_length=1024)


class ChatSend(BaseModel):
    message: str = Field(max_length=4000)
    search_turns: int = 0


_CHAT_HISTORY_MAX = 20  # prior messages sent to the model


# --- helpers -----------------------------------------------------------------
def _require_http_url(url: str) -> str:
    url = (url or "").strip()
    if not ingest.is_http_url(url):
        raise HTTPException(422, "URL must start with http:// or https://")
    return url


def _articles_by_story(conn: sqlite3.Connection, story_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {sid: [] for sid in story_ids}
    if not story_ids:
        return out
    marks = ",".join("?" * len(story_ids))
    rows = conn.execute(
        "SELECT a.id, a.story_id, a.title, a.url, a.author, a.published_date, a.hero_image_url, "
        "f.title AS feed FROM Article a JOIN Feed f ON f.id = a.feed_id "
        f"WHERE a.story_id IN ({marks}) ORDER BY a.published_date DESC",
        story_ids,
    )
    for r in rows:
        out[r["story_id"]].append({k: r[k] for k in r.keys() if k != "story_id"})
    return out


def _story_payload(item: dict[str, Any], articles: list[dict]) -> dict[str, Any]:
    s = item["story"]
    return {
        "id": s["id"],
        "headline": s["headline"],
        "summary": s["summary"],
        "tags": db.json_list(s["tags"]),
        "metrics": db.json_dict(s["metrics"]),
        "quality_score": s["quality_score"],
        "source_count": item["source_count"],
        "relevance": item["relevance"],
        "display_score": round(item["display"], 3),
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "articles": articles,
    }


def _load_story(conn: sqlite3.Connection, story_id: int) -> dict[str, Any]:
    s = conn.execute("SELECT * FROM Story WHERE id = ?", (story_id,)).fetchone()
    if s is None:
        raise HTTPException(404, "story not found")
    n = conn.execute(
        "SELECT COUNT(DISTINCT feed_id) FROM Article WHERE story_id = ?", (story_id,)
    ).fetchone()[0]
    relevance, display = scoring.score_story(s, n, scoring.get_user_profile(conn))
    item = {"story": s, "source_count": n, "relevance": relevance, "display": display}
    return _story_payload(item, _articles_by_story(conn, [story_id])[story_id])


def _is_public(request: Request) -> bool:
    """Read-only paths reachable without a session (chat history is not public)."""
    path = request.url.path
    if path in ("/", "/list", "/api/auth/login", "/api/auth/session") or path.startswith("/static/"):
        return True
    if request.method != "GET":
        return False
    if path in ("/api/stories", "/api/articles", "/api/heads-up", "/api/health", "/api/events"):
        return True
    return path.startswith("/api/stories/") and not path.endswith("/chat")


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


def _from_lan(request: Request) -> bool:
    """Client and every proxy-reported hop are private/loopback addresses.

    The last X-Forwarded-For entry is what nginx itself appended, so a client
    can't spoof its way in by sending a private address.
    """
    hops = [request.client.host if request.client else ""]
    fwd = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    hops += fwd[-1:] + [request.headers.get("x-real-ip", "").strip()]
    for hop in filter(None, hops):
        try:
            ip = ipaddress.ip_address(hop)
        except ValueError:
            return False
        if not (ip.is_private or ip.is_loopback):
            return False
    return True


def _same_origin(request: Request) -> bool:
    """Browsers send Origin on cross-site writes; non-browser clients usually send none."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    allowed = {request.headers.get("host", ""), urlsplit(config.PUBLIC_BASE).netloc}
    allowed.update(h.strip() for h in request.headers.get("x-forwarded-host", "").split(",") if h.strip())
    return urlsplit(origin).netloc in allowed


# --- app -----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    if not auth.auth_enabled():
        logging.getLogger("news.api").warning(
            "auth is OFF: management, chat and refresh are limited to LAN clients; "
            "set NEWS_AUTH_PASSWORD for remote access.")
    worker.start()
    heads_up.start_scheduler()
    try:
        yield
    finally:
        bus.close_all()
        heads_up.stop_scheduler()
        await asyncio.to_thread(worker.stop)


def create_app() -> FastAPI:
    app = FastAPI(title="AI News Aggregator", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if not _is_public(request):
            if not auth.auth_enabled():
                # Open mode is a LAN convenience; never hand management/chat to the internet.
                if not _from_lan(request):
                    return JSONResponse(status_code=403, content={
                        "detail": "open mode is LAN-only — set NEWS_AUTH_PASSWORD for remote access"})
            elif not auth.is_authenticated(request.cookies.get(config.AUTH_COOKIE)):
                if request.url.path == "/feeds":
                    return RedirectResponse("/", status_code=303)
                return JSONResponse(status_code=401, content={"detail": "unauthenticated"})
        return await call_next(request)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        # Cross-site writes (CSRF; also covers open mode, where no cookie protects anything).
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
            return JSONResponse(status_code=403, content={"detail": "cross-origin request rejected"})
        resp = await call_next(request)
        resp.headers.update(_SECURITY_HEADERS)
        if config.PUBLIC_BASE.startswith("https"):
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    # --- pages ---
    @app.get("/")
    def index_page():
        return FileResponse(_FRONTEND / "index.html", headers=_NO_CACHE)

    @app.get("/feeds")
    def feeds_page():
        return FileResponse(_FRONTEND / "feeds.html", headers=_NO_CACHE)

    @app.get("/list")
    def list_page():
        return FileResponse(_FRONTEND / "list.html", headers=_NO_CACHE)

    # --- stories ---
    @app.get("/api/stories")
    def list_stories(limit: int = Query(50, ge=1, le=1000), archived: bool = False,
                     q: str = Query("", max_length=100), tag: str = Query("", max_length=80)):
        with db.get_conn() as conn:
            ranked = scoring.ranked_stories(conn, archived=archived, query=q.strip(), tag=tag.strip())
            page = ranked[:limit]
            arts = _articles_by_story(conn, [i["story"]["id"] for i in page])
            pending = pipeline.pending_count(conn)
            last_article = conn.execute("SELECT MAX(created_at) FROM Article").fetchone()[0]
        return {
            "count": len(ranked),
            "pending": pending,
            "last_article": last_article,
            "active_days": config.STORY_ACTIVE_DAYS,
            "stories": [_story_payload(i, arts[i["story"]["id"]]) for i in page],
        }

    @app.get("/api/stories/{story_id}")
    def get_story(story_id: int):
        with db.get_conn() as conn:
            return _load_story(conn, story_id)

    @app.get("/api/heads-up")
    def get_heads_up():
        text, cached = heads_up.brief()
        return {"text": text or heads_up.last_brief() or "No brief available yet.", "cached": cached}

    # --- articles (/list) ---
    @app.get("/api/articles")
    def list_articles(limit: int = Query(100, ge=1, le=5000), order: str = "newest",
                      status: str = Query("", pattern="^(|assigned|pending|skipped|dead|duplicate)$"),
                      q: str = Query("", max_length=100)):
        direction = "ASC" if order == "oldest" else "DESC"
        where, params = [], []
        if status:
            where.append(f"({pipeline.STATUS_SQL}) = ?")
            params.append(status)
        if q.strip():
            where.append("(a.title LIKE ? OR f.title LIKE ?)")
            params += [f"%{q.strip()}%"] * 2
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        base = f"FROM Article a JOIN Feed f ON f.id = a.feed_id {where_sql}"
        with db.get_conn() as conn:
            total = conn.execute(f"SELECT count(*) {base}", params).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT a.id, a.title, a.url, a.author, a.published_date, a.created_at,
                       a.processed_at, a.content_class, a.quality, a.story_id, f.title AS feed,
                       ({pipeline.STATUS_SQL}) AS status,
                       a.t_embed_s, a.t_cluster_s, a.t_harness_s, a.t_story_s, a.t_wall_s, a.t_merge_s,
                       (julianday(a.processed_at) - julianday(a.created_at)) * 86400 AS latency_s
                {base}
                ORDER BY a.created_at {direction}, a.id {direction}
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        articles = []
        for r in rows:
            latency, wall = r["latency_s"], r["t_wall_s"]
            articles.append({
                "id": r["id"],
                "title": r["title"],
                "url": r["url"],
                "feed": r["feed"],
                "author": r["author"],
                "published_date": r["published_date"],
                "created_at": r["created_at"],
                "processed_at": r["processed_at"],
                "class": r["content_class"],
                "quality": r["quality"],
                "story_id": r["story_id"],
                "status": r["status"],
                "timings": {
                    "wait": max(0.0, latency - wall) if latency is not None and wall is not None else None,
                    "embed": r["t_embed_s"],
                    "cluster": r["t_cluster_s"],
                    "harness": r["t_harness_s"],
                    "story": r["t_story_s"],
                    "wall": wall,
                    "latency": latency,
                    "merge": r["t_merge_s"],
                },
            })
        return {"count": total, "order": order, "worker": worker.status, "articles": articles}

    # --- chat ---
    @app.get("/api/stories/{story_id}/chat")
    def get_chat(story_id: int):
        with db.get_conn() as conn:
            if conn.execute("SELECT 1 FROM Story WHERE id = ?", (story_id,)).fetchone() is None:
                raise HTTPException(404, "story not found")
            rows = conn.execute(
                "SELECT m.role, m.content FROM ChatMessage m JOIN ChatSession s ON s.id = m.session_id "
                "WHERE s.story_id = ? ORDER BY m.id",
                (story_id,),
            ).fetchall()
        return {
            "story_id": story_id,
            "messages": [dict(r) for r in rows],
            "search_turns_options": config.CHAT_SEARCH_TURNS,
        }

    @app.post("/api/stories/{story_id}/chat")
    def post_chat(story_id: int, body: ChatSend):
        text = (body.message or "").strip()
        if not text:
            raise HTTPException(400, "message must not be empty")
        with db.get_conn() as conn:
            story = _load_story(conn, story_id)
            conn.execute("INSERT OR IGNORE INTO ChatSession (story_id) VALUES (?)", (story_id,))
            session_id = conn.execute(
                "SELECT id FROM ChatSession WHERE story_id = ?", (story_id,)
            ).fetchone()[0]
            history = [dict(r) for r in conn.execute(
                "SELECT role, content FROM (SELECT id, role, content FROM ChatMessage "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (session_id, _CHAT_HISTORY_MAX),
            )]
            conn.execute(
                "INSERT INTO ChatMessage (session_id, role, content) VALUES (?, 'user', ?)",
                (session_id, text),
            )

        def ndjson(record: dict[str, Any]) -> str:
            return json.dumps(record, ensure_ascii=False) + "\n"

        def events():
            """NDJSON: status lines while researching, deltas while answering, then done/error."""
            parts: list[str] = []
            try:
                for kind, chunk in chat.stream(story, history, text, body.search_turns):
                    if kind == "delta":
                        parts.append(chunk)
                    yield ndjson({"type": kind, "text": chunk})
                yield ndjson({"type": "done"})
            except Exception as e:  # noqa: BLE001
                logging.getLogger("news.chat").exception("chat failed for story %s", story_id)
                yield ndjson({"type": "error", "text": f"chat failed: {type(e).__name__}"})
            finally:
                # Also runs when the client disconnects mid-answer: keep what was generated.
                reply = "".join(parts).strip()
                if reply:
                    with db.get_conn() as conn:
                        conn.execute(
                            "INSERT INTO ChatMessage (session_id, role, content) VALUES (?, 'assistant', ?)",
                            (session_id, reply),
                        )
                        conn.execute("UPDATE ChatSession SET updated_at = datetime('now') WHERE id = ?",
                                     (session_id,))

        return StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={**_NO_CACHE, "X-Accel-Buffering": "no"})

    # --- auth ---
    @app.post("/api/auth/login")
    def login(body: Login, request: Request):
        resp = JSONResponse({"ok": True})
        if not auth.auth_enabled():
            return resp
        ip = request.client.host if request.client else "unknown"
        if auth.login_throttled(ip):
            raise HTTPException(429, "too many failed attempts — try again shortly")
        if not auth.check_password(body.password):
            auth.record_failure(ip)
            raise HTTPException(401, "incorrect password")
        auth.clear_failures(ip)
        resp.set_cookie(
            config.AUTH_COOKIE,
            auth.create_session(),
            max_age=int(config.AUTH_SESSION_HOURS * 3600),
            httponly=True,
            samesite="lax",
            path="/",
            secure=config.PUBLIC_BASE.startswith("https"),
        )
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request):
        auth.destroy_session(request.cookies.get(config.AUTH_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(config.AUTH_COOKIE, path="/")
        return resp

    @app.get("/api/auth/session")
    def session(request: Request):
        return {
            "auth_enabled": auth.auth_enabled(),
            "authenticated": auth.is_authenticated(request.cookies.get(config.AUTH_COOKIE)),
        }

    # --- feedback / profile ---
    @app.post("/api/feedback")
    def post_feedback(body: Feedback):
        if body.kind not in ("up", "down"):
            raise HTTPException(400, "kind must be 'up' or 'down'")
        profile = scoring.apply_feedback(body.story_id, body.kind)
        if profile is None:
            raise HTTPException(404, "story not found")
        return {"ok": True, "profile": profile}

    @app.get("/api/feedback")
    def get_feedback(limit: int = Query(200, ge=1, le=5000)):
        return {"ok": True, "log": scoring.feedback_log(limit)}

    @app.get("/api/profile")
    def get_profile():
        return scoring.get_user_profile()

    @app.put("/api/profile")
    def put_profile(body: ProfileUpdate):
        prefs = body.preferences.model_dump(exclude_none=True)
        for key in ("interest_regions", "locations"):
            if key in prefs:
                prefs[key] = [v.strip() for v in prefs[key] if v.strip()]
        return scoring.update_profile(prefs, body.tag_weights)

    # --- worker ---
    @app.post("/api/refresh")
    def refresh():
        return worker.refresh()

    # --- feeds ---
    @app.get("/api/feeds")
    def list_feeds():
        with db.get_conn() as conn:
            feeds = [dict(r) for r in conn.execute("SELECT * FROM Feed ORDER BY category, title")]
            stats = {r["feed_id"]: r for r in conn.execute(
                "SELECT feed_id, "
                "COUNT(CASE WHEN created_at >= datetime('now', '-1 day') THEN 1 END) AS last_24h, "
                "COUNT(CASE WHEN created_at >= datetime('now', '-7 day') THEN 1 END) AS last_7d, "
                "COUNT(*) AS total FROM Article GROUP BY feed_id"
            )}
        cats: dict[str, dict[str, int]] = {}
        for f in feeds:
            f.pop("etag", None)
            f.pop("last_modified", None)
            s = stats.get(f["id"])
            f["last_24h"] = s["last_24h"] if s else 0
            f["last_7d"] = s["last_7d"] if s else 0
            f["article_total"] = s["total"] if s else 0
            g = cats.setdefault(f["category"] or "(uncategorized)",
                                {"feeds": 0, "enabled": 0, "errors": 0, "last_24h": 0, "last_7d": 0})
            g["feeds"] += 1
            g["errors"] += 1 if f["last_error"] else 0
            if f["enabled"]:  # category totals = active load only
                g["enabled"] += 1
                g["last_24h"] += f["last_24h"]
                g["last_7d"] += f["last_7d"]
        return {"feeds": feeds, "categories": dict(sorted(cats.items()))}

    @app.post("/api/feeds")
    def add_feed(body: FeedCreate):
        url = _require_http_url(body.xml_url)
        if not body.title.strip():
            raise HTTPException(422, "title must not be empty")
        with db.get_conn() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO Feed (title, xml_url, site_url, category, enabled) VALUES (?, ?, ?, ?, 1)",
                    (body.title.strip(), url, body.site_url, (body.category or "").strip() or None),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, f"feed already exists: {url}") from None
        return {"ok": True, "id": cur.lastrowid}

    @app.get("/api/feeds/preview")
    def preview_feed(url: str):
        import feedparser

        url = _require_http_url(url)
        try:
            r = ingest.fetch(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"fetch failed: {e}") from e
        parsed = feedparser.parse(r.content)
        title = (parsed.feed.get("title") or "").strip()
        if not title and parsed.entries:
            title = (parsed.entries[0].get("title") or "").strip()
        if not title:
            raise HTTPException(422, "could not parse a feed title")
        return {"title": title}

    @app.post("/api/feeds/category")
    def set_category_enabled(body: CategoryUpdate):
        enabled = 1 if body.enabled else 0
        with db.get_conn() as conn:
            if body.category:
                cur = conn.execute("UPDATE Feed SET enabled = ? WHERE category = ?", (enabled, body.category))
            else:
                cur = conn.execute(
                    "UPDATE Feed SET enabled = ? WHERE category IS NULL OR category = ''", (enabled,)
                )
        if enabled:
            worker.wake_pipeline()
        return {"affected": cur.rowcount}

    @app.patch("/api/feeds/{feed_id}")
    def update_feed(feed_id: int, body: FeedUpdate):
        sets: dict[str, Any] = {}
        if body.enabled is not None:
            sets["enabled"] = 1 if body.enabled else 0
        if body.title is not None:
            if not body.title.strip():
                raise HTTPException(422, "title must not be empty")
            sets["title"] = body.title.strip()
        if body.category is not None:
            sets["category"] = body.category.strip() or None
        if body.xml_url is not None:
            sets["xml_url"] = _require_http_url(body.xml_url)
        with db.get_conn() as conn:
            if conn.execute("SELECT 1 FROM Feed WHERE id = ?", (feed_id,)).fetchone() is None:
                raise HTTPException(404, "feed not found")
            if sets:
                assignments = ", ".join(f"{col} = ?" for col in sets)  # keys are fixed above
                try:
                    conn.execute(f"UPDATE Feed SET {assignments} WHERE id = ?", (*sets.values(), feed_id))
                except sqlite3.IntegrityError:
                    raise HTTPException(409, f"xml_url already in use: {sets.get('xml_url')}") from None
        if sets.get("enabled"):
            worker.wake_pipeline()
        return {"ok": True}

    @app.delete("/api/feeds/{feed_id}")
    def delete_feed(feed_id: int):
        with db.get_conn() as conn:
            if conn.execute("SELECT 1 FROM Feed WHERE id = ?", (feed_id,)).fetchone() is None:
                raise HTTPException(404, "feed not found")
            ids = [r[0] for r in conn.execute("SELECT id FROM Article WHERE feed_id = ?", (feed_id,))]
            clustering.delete_embeddings(conn, ids)
            conn.execute("DELETE FROM Feed WHERE id = ?", (feed_id,))  # cascades to Article
        pipeline.reap_orphaned_stories()
        return {"ok": True}

    # --- health / events ---
    @app.get("/api/health")
    def health():
        with db.get_conn() as conn:
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM Article), (SELECT count(*) FROM Story), "
                "(SELECT count(*) FROM Article WHERE story_id IS NULL AND COALESCE(pipeline_failures, 0) >= ?)",
                (config.PIPELINE_MAX_FAILURES,),
            ).fetchone()
            pending = pipeline.pending_count(conn)
        return {
            "ok": True,
            "articles": counts[0],
            "stories": counts[1],
            "pending": pending,
            "dead_letter": counts[2],
            "worker": worker.status,
        }

    @app.get("/api/events")
    async def events(request: Request):
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
                                 headers={**_NO_CACHE, "X-Accel-Buffering": "no"})

    app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")
    return app


app = create_app()
