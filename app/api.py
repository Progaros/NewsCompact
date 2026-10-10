"""FastAPI application: REST endpoints, SSE stream, and static frontend."""
from __future__ import annotations

import json
import queue
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path

from . import bus, config, db, scoring, worker

_FRONTEND_DIR = Path(__file__).with_name("frontend")


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


def create_app() -> FastAPI:
    app = FastAPI(title="AI News Aggregator")

    @app.on_event("startup")
    def _startup() -> None:
        db.init_db()
        worker.start()

    @app.get("/")
    def index():
        return FileResponse(_FRONTEND_DIR / "index.html")

    @app.get("/feeds")
    def feeds_page():
        return FileResponse(_FRONTEND_DIR / "feeds.html")

    @app.get("/api/stories")
    def list_stories(limit: int = 50):
        with db.get_conn() as conn:
            stories = conn.execute("SELECT * FROM Story").fetchall()
            counts = _source_counts(conn)
            payload = [_story_payload(conn, s, counts.get(s["id"], 0)) for s in stories]
        payload.sort(key=lambda p: p["display_score"], reverse=True)
        return {"count": len(payload), "stories": payload[:limit]}

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

    @app.get("/api/heads-up")
    def heads_up():
        """AI paragraph summarizing the top critical stories."""
        from . import ollama

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
            return {"text": "No stories yet. The worker is ingesting."}
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
        return {"text": text.strip()}

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
        return worker.run_cycle()

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
        return {"ok": True, "articles": arts, "stories": sts}

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

    app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")
    return app


app = create_app()
