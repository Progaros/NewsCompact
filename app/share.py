"""Share links: snapshots of a heads-up brief or a chat answer behind ``/p/<token>``."""
from __future__ import annotations

import json
import secrets
from typing import Any

from . import db, heads_up


def brief_snapshot(user_id: int, personal: bool) -> dict[str, Any] | None:
    st = heads_up.status(heads_up.user_key(user_id) if personal else heads_up.DEFAULT)
    if not st["text"]:
        return None
    return {"text": st["text"], "view": "personal" if personal else "default", "generated_at": st["generated_at"]}


def answer_snapshot(user_id: int, story_id: int | None, message_id: int | None) -> dict[str, Any] | None:
    """One of the user's own chat answers with its question; None if not found."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT a.content, q.content AS question, s.headline, s.id AS story_id FROM ChatMessage a "
            "JOIN ChatSession c ON c.id = a.session_id JOIN Story s ON s.id = c.story_id "
            "LEFT JOIN ChatMessage q ON q.id = a.parent_id "
            "WHERE a.id = ? AND a.role = 'assistant' AND c.user_id = ? AND c.story_id = ?",
            (message_id, user_id, story_id)).fetchone()
    if row is None or not row["content"]:
        return None
    return {"story_id": row["story_id"], "headline": row["headline"],
            "question": row["question"] or "", "answer": row["content"]}


def create(user_id: int, kind: str, payload: dict[str, Any], personal: bool) -> str:
    """Store the snapshot; personal content gets a long, unguessable token. Returns the link path."""
    token = secrets.token_urlsafe(24 if personal else 6)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO ShareLink (token, user_id, kind, payload) VALUES (?, ?, ?, ?)",
                     (token, user_id, kind, json.dumps(payload, ensure_ascii=False)))
    return f"/p/{token}"


def get(token: str) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT s.kind, s.payload, s.created_at, u.name FROM ShareLink s "
                           "JOIN AppUser u ON u.id = s.user_id WHERE s.token = ?", (token,)).fetchone()
    if row is None:
        return None
    return {"kind": row["kind"], "shared_by": row["name"], "created_at": row["created_at"],
            **db.json_dict(row["payload"])}


def list_links(user_id: int) -> list[dict[str, Any]]:
    """The user's own share links, newest first."""
    with db.get_conn() as conn:
        rows = conn.execute("SELECT token, kind, payload, created_at FROM ShareLink WHERE user_id = ? "
                            "ORDER BY created_at DESC, rowid DESC", (user_id,)).fetchall()
    links = []
    for r in rows:
        p = db.json_dict(r["payload"])
        if r["kind"] == "brief":
            title = f"Heads-up ({'personal' if p.get('view') == 'personal' else 'default view'})"
        else:
            title = f"{p.get('headline') or 'Story'} — {(p.get('question') or '')[:80]}"
        links.append({"token": r["token"], "url": f"/p/{r['token']}", "kind": r["kind"],
                      "title": title, "created_at": r["created_at"]})
    return links


def revoke(user_id: int, token: str) -> bool:
    with db.get_conn() as conn:
        return conn.execute("DELETE FROM ShareLink WHERE token = ? AND user_id = ?", (token, user_id)).rowcount > 0
