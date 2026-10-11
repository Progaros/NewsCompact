"""FastAPI application: REST endpoints, SSE stream, and static frontend."""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path

from . import auth, bus, chat, config, db, scoring, worker

log = logging.getLogger("news.heads-up")

_FRONTEND_DIR = Path(__file__).with_name("frontend")


class _NoCacheStaticFiles(StaticFiles):
    """Frontend is served in place (no build step): always revalidate so
    edits to app.js/css take effect on a normal reload, not a hard refresh."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

# "Quick Heads Up" brief cache: {"text": str, "ts": epoch}. The LLM call is
# slow, so the brief is only regenerated once it's older than the TTL
# (config.HEADS_UP_TTL_MINUTES); fresh requests get the stored text back.
_heads_up_cache: dict = {}


class Feedback(BaseModel):
    story_id: int
    kind: str


class ProfileUpdate(BaseModel):
    # Merge user-provided data (location, interest_regions, ...) into
    # UserProfile.preferences — feeds the user_impact term of relevance.
    preferences: dict = {}


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
    password: str


class ChatSend(BaseModel):
    message: str
    # Web-search turn budget (searchngx) for this turn; clamped server-side to
    # the configured set (config.CHAT_SEARCH_TURNS) in app/chat.py.
    search_turns: int = 0


def _load_tags(raw: str) -> list[str]:
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def _load_json(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _source_counts(conn) -> dict:
    """story_id -> distinct source (feed) count, for the credibility term."""
    rows = conn.execute(
        "SELECT story_id, COUNT(DISTINCT feed_id) AS c FROM Article "
        "WHERE story_id IS NOT NULL GROUP BY story_id"
    ).fetchall()
    return {r["story_id"]: r["c"] for r in rows}


def _story_payload(conn, story, source_count: int) -> dict:
    arts = conn.execute(
        "SELECT id, title, url, author, published_date, hero_image_url, length(content_raw) AS words_len "
        "FROM Article WHERE story_id = ? ORDER BY published_date DESC",
        (story["id"],),
    ).fetchall()
    profile = scoring.get_user_profile()
    tw, prefs = profile["tag_weights"], profile["preferences"]
    return {
        "id": story["id"],
        "headline": story["headline"],
        "summary": story["summary"],
        "tags": _load_tags(story["tags"]),
        "metrics": _load_json(story["metrics"]),
        "quality_score": story["quality_score"],
        "source_count": source_count,
        "relevance": scoring.relevance_score(story, tw, source_count, prefs),
        "created_at": story["created_at"],
        "updated_at": story["updated_at"],
        "display_score": round(scoring.display_score(story, tw, source_count, prefs), 3),
        "articles": [dict(a) for a in arts],
    }


# --- Heads-up brief: generation, ntfy push, and the scheduled push loop ---

def _build_brief(force: bool = False) -> tuple[str, bool]:
    """Generate the "Quick Heads Up" brief, honoring the TTL cache.

    Returns ``(text, cached)``. With ``force=False`` a stored brief younger
    than the TTL is returned as-is (no LLM call). With ``force=True`` (used
    by the scheduled push) it always regenerates. Failures are never cached
    so an Ollama hiccup doesn't pin the endpoint to an error for the whole
    TTL. Shared by the ``/api/heads-up`` endpoint and the scheduler.
    """
    from . import ollama

    now = time.time()
    ttl = config.HEADS_UP_TTL_MINUTES * 60
    if not force and _heads_up_cache and now - _heads_up_cache["ts"] < ttl:
        return _heads_up_cache["text"], True

    profile = scoring.get_user_profile()
    with db.get_conn() as conn:
        stories = conn.execute("SELECT * FROM Story").fetchall()
        counts = _source_counts(conn)
    tw, prefs = profile["tag_weights"], profile["preferences"]
    ranked = sorted(
        stories,
        key=lambda s: scoring.display_score(s, tw, counts.get(s["id"], 0), prefs),
        reverse=True,
    )[:5]
    if not ranked:
        return "No stories yet. The worker is ingesting.", False

    lines = []
    for s in ranked:
        m = _load_json(s["metrics"])
        lines.append(
            f"- {s['headline']} | urgency={m.get('urgency_level')} "
            f"status={m.get('status')} | {s['summary']}"
        )
    try:
        text = ollama.chat(
            [
                {"role": "system", "content": "Write a concise 2-3 sentence news brief "
                                               "in plain text. Be factual, no preamble."},
                {"role": "user", "content": "Summarize these top stories for a reader:\n" + "\n".join(lines)},
            ],
            temperature=0.2,
        )
    except Exception as e:  # noqa: BLE001
        text = f"(heads-up unavailable: {e})"
    text = text.strip()
    # Don't cache failures — a hiccup with Ollama shouldn't lock the
    # endpoint into an error message for the whole TTL.
    if not text.startswith("(heads-up unavailable"):
        _heads_up_cache["text"] = text
        _heads_up_cache["ts"] = now
    return text, False


def _send_ntfy(text: str) -> None:
    """Push the brief to the phone over ntfy (POST body = message text)."""
    import httpx

    try:
        r = httpx.post(
            config.HEADS_UP_NTFY_URL,
            content=text.encode("utf-8"),
            headers={"Title": config.HEADS_UP_NTFY_TITLE},
            timeout=15.0,
        )
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("heads-up ntfy push to %s failed: %s", config.HEADS_UP_NTFY_URL, e)


_SCHED_POLL_SECONDS = 15      # how often the scheduler wakes to check due times
_SCHED_CATCHUP_MINUTES = 5    # a time still fires if it fell within this window


def _parse_hhmm(t: str) -> int | None:
    """Parse "HH:MM" to minutes-since-midnight, or None if malformed."""
    parts = t.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _fire(time_str: str) -> None:
    """One scheduled firing: regenerate a fresh brief and push it to ntfy."""
    text, _cached = _build_brief(force=True)
    # On an LLM failure or an empty DB the fresh call returns a sentinel;
    # _build_brief never caches those, so fall back to the last good brief.
    if not text or text.startswith("(heads-up unavailable") or text.startswith("No stories"):
        text = _heads_up_cache.get("text") or ""
    if not text:
        log.info("heads-up: skip scheduled push at %s (no brief yet)", time_str)
        return
    _send_ntfy(text)
    log.info("heads-up: pushed scheduled brief (schedule %s)", time_str)


_sched_stop = threading.Event()


def _sched_tick(fired: dict[str, str]) -> None:
    """Fire any configured times that have come due (once per time per day)."""
    times = config.HEADS_UP_NTFY_TIMES
    if not times or not config.HEADS_UP_NTFY_URL:
        return
    now = datetime.now()
    today = now.date().isoformat()
    now_min = now.hour * 60 + now.minute
    for t in times:
        t_min = _parse_hhmm(t)
        if t_min is None:
            log.warning("heads-up: skipping malformed schedule time %r", t)
            continue
        delta = now_min - t_min
        if delta < 0 or delta > _SCHED_CATCHUP_MINUTES:
            continue  # not yet, or already well past (don't fire a stale time)
        if fired.get(t) == today:
            continue  # already fired this time today
        _fire(t)
        fired[t] = today


def _sched_loop() -> None:
    fired: dict[str, str] = {}  # "HH:MM" -> date (ISO) it last fired
    while not _sched_stop.is_set():
        try:
            _sched_tick(fired)
        except Exception:  # noqa: BLE001
            log.exception("heads-up scheduler tick failed")
        _sched_stop.wait(_SCHED_POLL_SECONDS)


_sched_started = False


def start_heads_up_scheduler() -> None:
    """Start the background thread that fires the scheduled ntfy push(es)."""
    global _sched_started
    if _sched_started:
        return
    _sched_started = True
    threading.Thread(target=_sched_loop, daemon=True).start()


# --- Auth middleware (default-secure) ---------------------------------------

def _is_public(request: Request) -> bool:
    """Paths reachable without a session. Reads only — the main page must
    stay fully readable (and live) for a not-signed-in viewer; everything
    else 401s. When auth is disabled (no NEWS_AUTH_PASSWORD) this is moot."""
    path = request.url.path
    if path == "/" or path == "/list" or path.startswith("/static/"):
        return True
    if path in ("/api/auth/login", "/api/auth/session"):
        return True
    if request.method == "GET":
        # story list + detail (the list endpoint also serves the load-more
        # pagination), flat article list, heads-up brief, health, and the SSE
        # stream. NOTE: /api/stories/{id}/chat is deliberately EXCLUDED from
        # the public prefix — chat history is signed-in only.
        if path in ("/api/stories", "/api/articles") or (
            path.startswith("/api/stories/") and not path.endswith("/chat")
        ):
            return True
        if path in ("/api/heads-up", "/api/health", "/api/events"):
            return True
    return False


def create_app() -> FastAPI:
    app = FastAPI(title="AI News Aggregator")

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        if auth.auth_enabled() and not _is_public(request):
            if not auth.is_authenticated(request.cookies.get(config.AUTH_COOKIE)):
                # Browser navigation to the management page → home; API
                # clients get a machine-readable 401 for the toast.
                if request.url.path == "/feeds":
                    return RedirectResponse("/", status_code=303)
                return JSONResponse(status_code=401, content={"detail": "unauthenticated"})
        return await call_next(request)

    @app.on_event("startup")
    def _startup() -> None:
        db.init_db()
        worker.start()
        start_heads_up_scheduler()

    @app.get("/")
    def index():
        # no-cache so JS edits never sit behind a heuristic cache
        return FileResponse(_FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/feeds")
    def feeds_page():
        return FileResponse(_FRONTEND_DIR / "feeds.html", headers={"Cache-Control": "no-cache"})

    @app.get("/list")
    def list_page():
        return FileResponse(_FRONTEND_DIR / "list.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/stories")
    def list_stories(limit: int = 50):
        with db.get_conn() as conn:
            stories = conn.execute("SELECT * FROM Story").fetchall()
            counts = _source_counts(conn)
            payload = [_story_payload(conn, s, counts.get(s["id"], 0)) for s in stories]
            # Pipeline-eligible backlog (same query as /api/health), surfaced next
            # to the story total in the status bar as "N enqueued".
            pending = conn.execute(
                "SELECT count(*) c FROM Article a JOIN Feed f ON f.id = a.feed_id "
                "WHERE a.story_id IS NULL AND f.enabled = 1 "
                "AND COALESCE(a.pipeline_failures, 0) < ?",
                (config.PIPELINE_MAX_FAILURES,),
            ).fetchone()["c"]
            # Newest article's ingest time — the feed-freshness signal, shown in
            # the status bar as "last article Xm ago" (naive-UTC string, formatted
            # client-side).
            last_article = conn.execute("SELECT MAX(created_at) FROM Article").fetchone()[0]
        payload.sort(key=lambda p: p["display_score"], reverse=True)
        return {"count": len(payload), "pending": pending, "last_article": last_article, "stories": payload[:limit]}

    @app.get("/api/articles")
    def list_articles(limit: int = 100, order: str = "newest"):
        """Compact flat list of every ingested article for the /list page.

        Read-only, paginated by cumulative limit (load-more). Default sort is
        newest-first by created_at ("first seen"); created_at is naive-UTC, so
        lexicographic == chronological. Returns a trimmed payload (no bodies).
        """
        if order not in ("newest", "oldest"):
            order = "newest"
        direction = "DESC" if order == "newest" else "ASC"
        max_fail = config.PIPELINE_MAX_FAILURES
        with db.get_conn() as conn:
            total = conn.execute("SELECT count(*) c FROM Article").fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT a.id, a.title, a.url, a.created_at, a.content_class,
                       a.quality, a.story_id, a.pipeline_failures, f.title AS feed,
                       a.t_embed_s, a.t_cluster_s, a.t_harness_s,
                       a.t_story_s, a.t_wall_s, a.author, a.published_date
                FROM Article a JOIN Feed f ON f.id = a.feed_id
                ORDER BY a.created_at {direction}, a.id {direction}
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        articles = []
        for r in rows:
            if r["story_id"] is not None:
                status = "assigned"
            elif (r["pipeline_failures"] or 0) >= max_fail:
                status = "dead"
            else:
                status = "pending"
            articles.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "url": r["url"],
                    "feed": r["feed"],
                    "author": r["author"],
                    "published_date": r["published_date"],
                    "created_at": r["created_at"],
                    "class": r["content_class"],
                    "quality": r["quality"],
                    "story_id": r["story_id"],
                    "status": status,
                    # Per-stage pipeline wall times (s); null if never processed.
                    "timings": {
                        "embed": r["t_embed_s"],
                        "cluster": r["t_cluster_s"],
                        "harness": r["t_harness_s"],
                        "story": r["t_story_s"],
                        "wall": r["t_wall_s"],
                    },
                }
            )
        return {"count": total, "order": order, "articles": articles}

    @app.get("/api/stories/{story_id}")
    def get_story(story_id: int):
        with db.get_conn() as conn:
            s = conn.execute("SELECT * FROM Story WHERE id=?", (story_id,)).fetchone()
            if s is None:
                raise HTTPException(404, "story not found")
            sc = conn.execute(
                "SELECT COUNT(DISTINCT feed_id) AS c FROM Article WHERE story_id=?",
                (story_id,),
            ).fetchone()["c"]
            return _story_payload(conn, s, sc)

    @app.get("/api/stories/{story_id}/chat")
    def get_chat(story_id: int):
        """History for a story's chat session (signed-in only, see _is_public)."""
        with db.get_conn() as conn:
            s = conn.execute("SELECT * FROM Story WHERE id=?", (story_id,)).fetchone()
            if s is None:
                raise HTTPException(404, "story not found")
            session = conn.execute(
                "SELECT id FROM ChatSession WHERE story_id=?", (story_id,)
            ).fetchone()
            if session is None:
                messages = []
            else:
                rows = conn.execute(
                    "SELECT role, content FROM ChatMessage WHERE session_id=? ORDER BY id",
                    (session["id"],),
                ).fetchall()
                messages = [{"role": r["role"], "content": r["content"]} for r in rows]
        return {
            "story_id": story_id,
            "messages": messages,
            "search_turns_options": config.CHAT_SEARCH_TURNS,
        }

    @app.post("/api/stories/{story_id}/chat")
    def post_chat(story_id: int, body: ChatSend):
        """Append a user message, generate the assistant reply, persist both."""
        text = (body.message or "").strip()
        if not text:
            raise HTTPException(400, "message must not be empty")
        with db.get_conn() as conn:
            s = conn.execute("SELECT * FROM Story WHERE id=?", (story_id,)).fetchone()
            if s is None:
                raise HTTPException(404, "story not found")
            sc = conn.execute(
                "SELECT COUNT(DISTINCT feed_id) AS c FROM Article WHERE story_id=?",
                (story_id,),
            ).fetchone()["c"]
            story = _story_payload(conn, s, sc)
            # One session per story (single local user).
            session = conn.execute(
                "SELECT id FROM ChatSession WHERE story_id=?", (story_id,)
            ).fetchone()
            if session is None:
                cur = conn.execute("INSERT INTO ChatSession (story_id) VALUES (?)", (story_id,))
                session_id = cur.lastrowid
            else:
                session_id = session["id"]
            rows = conn.execute(
                "SELECT role, content FROM ChatMessage WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            history = [{"role": r["role"], "content": r["content"]} for r in rows]
            conn.execute(
                "INSERT INTO ChatMessage (session_id, role, content) VALUES (?, 'user', ?)",
                (session_id, text),
            )
        # The LLM call is slow — run it with the connection CLOSED so the WAL
        # lock is not held across it (see db.py autocommit note).
        reply = chat.generate(story, history, text, body.search_turns)
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO ChatMessage (session_id, role, content) VALUES (?, 'assistant', ?)",
                (session_id, reply),
            )
            conn.execute(
                "UPDATE ChatSession SET updated_at=datetime('now') WHERE id=?",
                (session_id,),
            )
        return {"reply": reply, "story_id": story_id}

    @app.get("/api/heads-up")
    def heads_up():
        """AI paragraph summarizing the top critical stories.

        Cached: if the stored brief is younger than the TTL
        (NEWS_HEADS_UP_TTL_MINUTES, default 3 min) it is returned without
        re-running the LLM. The frontend only calls this on page open, so the
        brief refreshes at most once per TTL while the page is open.
        """
        text, cached = _build_brief()
        return {"text": text, "cached": cached}

    @app.post("/api/auth/login")
    def login(body: Login, request: Request):
        ip = request.client.host if request.client else "unknown"
        if auth.auth_enabled():
            if auth.login_throttled(ip):
                raise HTTPException(429, "too many failed attempts — try again shortly")
            if not auth.check_password(body.password):
                auth.record_failure(ip)
                raise HTTPException(401, "incorrect password")
            auth.clear_failures(ip)
        resp = JSONResponse({"ok": True})
        if auth.auth_enabled():
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
        """Boot probe: lets the frontend know whether to show the sign-in UI."""
        return {
            "auth_enabled": auth.auth_enabled(),
            "authenticated": auth.is_authenticated(request.cookies.get(config.AUTH_COOKIE)),
        }

    @app.post("/api/feedback")
    def feedback(body: Feedback):
        if body.kind not in ("up", "down"):
            raise HTTPException(400, "kind must be 'up' or 'down'")
        scoring.apply_feedback(body.story_id, body.kind)
        return {"ok": True, "profile": scoring.get_user_profile()}

    @app.get("/api/profile")
    def profile():
        return scoring.get_user_profile()

    @app.put("/api/profile")
    def set_profile(body: ProfileUpdate):
        # Collect user data (location, interest_regions, ...) -> user_impact term.
        return scoring.update_preferences(body.preferences or {})

    @app.get("/api/feedback")
    def feedback_log(limit: int = 200):
        """Raw 👍/👎 votes — kept separate from the profile for later tuning."""
        return {"ok": True, "log": scoring.feedback_log(limit)}

    @app.post("/api/refresh")
    def refresh():
        # Runs inline; fine for a local single-user app. Long but explicit.
        # Locked: an unlocked refresh would run concurrently with the
        # scheduled worker cycle and double-process the same articles.
        return worker.run_cycle_locked()

    @app.get("/api/feeds")
    def list_feeds():
        """Feeds + live volume stats (24h/7d) per feed and per category.

        Disabled feeds keep ingesting RSS rows (no AI), so their stats stay
        current — this is the "which feed is too chatty" dashboard.
        """
        with db.get_conn() as conn:
            feeds = [dict(r) for r in conn.execute("SELECT * FROM Feed ORDER BY category, title")]
            rows = conn.execute(
                "SELECT feed_id, "
                "COUNT(CASE WHEN created_at >= datetime('now', '-1 day') THEN 1 END) AS last_24h, "
                "COUNT(CASE WHEN created_at >= datetime('now', '-7 day') THEN 1 END) AS last_7d, "
                "COUNT(*) AS total "
                "FROM Article GROUP BY feed_id"
            ).fetchall()
        stats = {r["feed_id"]: dict(r) for r in rows}
        for f in feeds:
            s = stats.get(f["id"], {})
            f["last_24h"] = s.get("last_24h", 0)
            f["last_7d"] = s.get("last_7d", 0)
            f["article_total"] = s.get("total", 0)
        cats: dict[str, dict] = {}
        for f in feeds:
            c = f.get("category") or "(uncategorized)"
            g = cats.setdefault(c, {"feeds": 0, "enabled": 0, "last_24h": 0, "last_7d": 0})
            g["feeds"] += 1
            # Category totals reflect the ACTIVE load only — disabled feeds
            # still show their own per-feed stats, but don't bloat the sum.
            if f["enabled"]:
                g["enabled"] += 1
                g["last_24h"] += f["last_24h"]
                g["last_7d"] += f["last_7d"]
        return {"feeds": feeds, "categories": dict(sorted(cats.items()))}

    @app.post("/api/feeds/category")
    def set_category_enabled(body: CategoryUpdate):
        """Enable/disable every feed in a category in one call.

        `category` null/empty targets the uncategorized bucket.
        """
        with db.get_conn() as conn:
            if body.category:
                cur = conn.execute(
                    "UPDATE Feed SET enabled = ? WHERE category = ?",
                    (body.enabled, body.category),
                )
            else:
                cur = conn.execute(
                    "UPDATE Feed SET enabled = ? WHERE category IS NULL OR category = ''",
                    (body.enabled,),
                )
            conn.commit()
            return {"affected": cur.rowcount}

    @app.get("/api/feeds/preview")
    def preview_feed(url: str):
        """Fetch a feed URL server-side and return its <title>.

        Browser fetches of arbitrary RSS hosts usually fail on CORS, so the
        add-feed form asks us to preview instead.
        """
        import feedparser
        import httpx

        try:
            r = httpx.get(
                url,
                headers={"User-Agent": config.USER_AGENT},
                timeout=15.0,
                follow_redirects=True,
            )
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"fetch failed: {e}") from e
        parsed = feedparser.parse(r.text)
        title = (parsed.feed.get("title") or "").strip()
        if not title and parsed.entries:
            title = (parsed.entries[0].get("title") or "").strip()
        if title:
            return {"title": title}
        raise HTTPException(422, "could not parse a feed title")

    @app.post("/api/feeds")
    def add_feed(body: FeedCreate):
        with db.get_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO Feed (title, xml_url, site_url, category, enabled) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (body.title.strip(), body.xml_url.strip(), body.site_url, body.category),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, f"feed already exists: {body.xml_url}")
        return {"ok": True}

    @app.patch("/api/feeds/{feed_id}")
    def update_feed(feed_id: int, body: FeedUpdate):
        fields = {
            "enabled": body.enabled, "title": body.title,
            "category": body.category, "xml_url": body.xml_url,
        }
        sets = {k: v for k, v in fields.items() if v is not None}
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM Feed WHERE id=?", (feed_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "feed not found")
            if "xml_url" in sets:
                try:
                    conn.execute(
                        "UPDATE Feed SET xml_url = ? WHERE id = ?", (sets["xml_url"], feed_id)
                    )
                except sqlite3.IntegrityError:
                    raise HTTPException(409, f"xml_url already in use: {sets['xml_url']}")
            for col in ("enabled", "title", "category"):
                if col in sets:
                    conn.execute(f"UPDATE Feed SET {col} = ? WHERE id = ?", (sets[col], feed_id))
        return {"ok": True}

    @app.delete("/api/feeds/{feed_id}")
    def delete_feed(feed_id: int):
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM Feed WHERE id=?", (feed_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "feed not found")
            art_ids = [r[0] for r in conn.execute(
                "SELECT id FROM Article WHERE feed_id=?", (feed_id,))]
            for aid in art_ids:  # vec0 rows are not cascaded by FK
                conn.execute("DELETE FROM Article_Embeddings WHERE article_id = ?", (aid,))
            conn.execute("DELETE FROM Feed WHERE id=?", (feed_id,))  # cascades Articles
        return {"ok": True}

    @app.get("/api/health")
    def health():
        with db.get_conn() as conn:
            arts = conn.execute("SELECT count(*) c FROM Article").fetchone()["c"]
            sts = conn.execute("SELECT count(*) c FROM Story").fetchone()["c"]
            pending = conn.execute(
                "SELECT count(*) c FROM Article a JOIN Feed f ON f.id = a.feed_id "
                "WHERE a.story_id IS NULL AND f.enabled = 1 "
                "AND COALESCE(a.pipeline_failures, 0) < ?",
                (config.PIPELINE_MAX_FAILURES,),
            ).fetchone()["c"]
            dead_letter = conn.execute(
                "SELECT count(*) c FROM Article WHERE story_id IS NULL "
                "AND COALESCE(pipeline_failures, 0) >= ?",
                (config.PIPELINE_MAX_FAILURES,),
            ).fetchone()["c"]
        return {
            "ok": True,
            "articles": arts,
            "stories": sts,
            "pending": pending,
            "dead_letter": dead_letter,
        }

    @app.get("/api/events")
    def events():
        def gen():
            q = bus.subscribe()
            try:
                yield bus.emit_json({"type": "hello"})
                while True:
                    try:
                        yield bus.emit_json(q.get(timeout=15))
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    app.mount("/static", _NoCacheStaticFiles(directory=_FRONTEND_DIR), name="static")
    return app


app = create_app()
