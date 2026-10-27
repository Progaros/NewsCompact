"""Story payloads for the API and per-user read marks."""
from __future__ import annotations

import sqlite3
from typing import Any

from . import clustering, db, scoring


def articles_by_story(conn: sqlite3.Connection, story_ids: list[int],
                      exclude: set[int] = frozenset()) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {sid: [] for sid in story_ids}
    if not story_ids:
        return out
    marks = ",".join("?" * len(story_ids))
    rows = conn.execute(
        "SELECT a.id, a.story_id, a.feed_id, a.title, a.url, a.author, a.published_date, a.created_at, "
        "a.hero_image_url, f.title AS feed FROM Article a JOIN Feed f ON f.id = a.feed_id "
        f"WHERE a.story_id IN ({marks}) ORDER BY a.published_date DESC",
        story_ids,
    )
    for r in rows:
        if r["feed_id"] not in exclude:
            out[r["story_id"]].append({k: r[k] for k in r.keys() if k not in ("story_id", "feed_id")})
    return out


def story_payload(item: dict[str, Any], articles: list[dict],
                  related: list[dict] | None = None) -> dict[str, Any]:
    s = item["story"]
    return {
        "id": s["id"],
        "headline": s["headline"],
        "summary": s["summary"],
        "tags": scoring.story_tags(s),
        "metrics": db.json_dict(s["metrics"]),
        "quality_score": item.get("quality", s["quality_score"]),
        "source_count": item["source_count"],
        "outlet_count": item["outlet_count"],
        "relevance": item["relevance"],
        "display_score": round(item["display"], 1),
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "news_at": scoring.news_time(s),
        "read": item.get("read"),
        "read_at": item.get("read_at"),
        "changes": item.get("changes") or [],
        "vote": item.get("vote"),
        "match": item.get("match"),
        "articles": articles,
        "related": related or [],
    }


def load_story(conn: sqlite3.Connection, story_id: int, user_id: int | None = None,
               personal: bool = True) -> dict[str, Any] | None:
    """One story scored for ``user_id`` (personal view) or the default view; read state of ``user_id``."""
    s = conn.execute("SELECT * FROM Story WHERE id = ?", (story_id,)).fetchone()
    if s is None:
        return None
    personal = personal and user_id is not None
    profile = scoring.get_user_profile(conn, user_id) if personal else scoring.DEFAULT_PROFILE
    exclude = scoring.excluded_feeds(profile)
    st = scoring.source_stats(conn, scoring.feed_trust(conn, profile), story_id, exclude).get(
        story_id, {"feeds": 0, "outlets": 0, "trust": 0.0, "quality": None, "heat": 0.0})
    quality = st["quality"] if st["quality"] is not None else s["quality_score"]
    freq = scoring.tag_frequencies(conn) if personal and profile.get("inferred_weights") else None
    relevance, display = scoring.score_story(s, st, profile, personal=personal, freq=freq,
                                             dynamics=scoring.story_dynamics(conn, story_id).get(story_id))
    state = scoring.read_states(conn, user_id).get(story_id, {"read": None})
    if personal:
        display = scoring.apply_reader_state(relevance, display, state)
    item = {"story": s, "source_count": st["feeds"], "outlet_count": st["outlets"], "quality": quality,
            "relevance": relevance, "display": display, "vote": scoring.vote_states(conn, user_id).get(story_id),
            **state}
    return story_payload(item, articles_by_story(conn, [story_id], exclude)[story_id],
                         clustering.related_stories(conn, [story_id], exclude=exclude)[story_id])


def resolve_id(conn: sqlite3.Connection, story_id: int) -> int:
    """The story that absorbed ``story_id`` in a merge, else ``story_id`` itself."""
    row = conn.execute("SELECT story_id FROM StoryAlias WHERE old_id = ?", (story_id,)).fetchone()
    return row[0] if row else story_id


def exists(conn: sqlite3.Connection, story_id: int) -> bool:
    return conn.execute("SELECT 1 FROM Story WHERE id = ?", (story_id,)).fetchone() is not None


def mark_read(conn: sqlite3.Connection, user_id: int, story_id: int) -> bool:
    """Marks the story read (shown again when it changes materially); False if no such story."""
    story_id = resolve_id(conn, story_id)
    if not exists(conn, story_id):
        return False
    conn.execute("INSERT OR REPLACE INTO StoryRead (user_id, story_id, read_at) "
                 "VALUES (?, ?, datetime('now'))", (user_id, story_id))
    return True


def mark_unread(conn: sqlite3.Connection, user_id: int, story_id: int) -> None:
    story_id = resolve_id(conn, story_id)
    conn.execute("DELETE FROM StoryRead WHERE user_id = ? AND story_id = ?", (user_id, story_id))
