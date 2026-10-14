"""Per-user chat history of a story: a message tree (edits are sibling versions) with one visible branch."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db


def session(conn: sqlite3.Connection, story_id: int, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT id, leaf_id FROM ChatSession WHERE story_id = ? AND user_id = ?",
                        (story_id, user_id)).fetchone()


def branch(conn: sqlite3.Connection, sess: sqlite3.Row | None) -> list[dict[str, Any]]:
    """Messages from the first one to the session's leaf, each with its versions (siblings)."""
    if sess is None:
        return []
    rows = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, parent_id, role, content, steps FROM ChatMessage WHERE session_id = ? ORDER BY id",
        (sess["id"],))}
    siblings: dict[int | None, list[int]] = {}
    for r in rows.values():
        siblings.setdefault(r["parent_id"], []).append(r["id"])
    path, cur = [], rows.get(sess["leaf_id"] or max(rows, default=0))
    while cur is not None:
        path.append(cur)
        cur = rows.get(cur["parent_id"]) if cur["parent_id"] is not None else None
    out = []
    for m in reversed(path):
        versions = [i for i in siblings[m["parent_id"]] if rows[i]["role"] == m["role"]]
        out.append({"id": m["id"], "role": m["role"], "content": m["content"],
                    "steps": db.json_list(m["steps"]), "versions": versions,
                    "version": versions.index(m["id"]) + 1})
    return out


def payload(conn: sqlite3.Connection, story_id: int, user_id: int) -> dict[str, Any]:
    return {"story_id": story_id, "messages": branch(conn, session(conn, story_id, user_id))}


def switch(conn: sqlite3.Connection, story_id: int, user_id: int, message_id: int) -> bool:
    """Show another version of a message (and the latest replies that follow it); False if unknown."""
    sess = session(conn, story_id, user_id)
    if sess is None or conn.execute("SELECT 1 FROM ChatMessage WHERE id = ? AND session_id = ?",
                                    (message_id, sess["id"])).fetchone() is None:
        return False
    leaf = message_id
    while (child := conn.execute("SELECT MAX(id) FROM ChatMessage WHERE parent_id = ?",
                                 (leaf,)).fetchone()[0]) is not None:
        leaf = child
    conn.execute("UPDATE ChatSession SET leaf_id = ? WHERE id = ?", (leaf, sess["id"]))
    return True


def start_turn(conn: sqlite3.Connection, story_id: int, user_id: int, text: str, edit_of: int | None,
               max_history: int) -> tuple[int, int, list[dict[str, str]]] | None:
    """Store the question (as a new version of ``edit_of`` if given).

    Returns ``(session_id, question_id, history)`` with the last ``max_history`` prior messages,
    or None when ``edit_of`` is not a user message of the visible branch.
    """
    conn.execute("INSERT OR IGNORE INTO ChatSession (user_id, story_id) VALUES (?, ?)", (user_id, story_id))
    sess = session(conn, story_id, user_id)
    msgs = branch(conn, sess)
    if edit_of is not None:
        idx = next((i for i, m in enumerate(msgs) if m["id"] == edit_of and m["role"] == "user"), None)
        if idx is None:
            return None
        msgs = msgs[:idx]
    history = [{"role": m["role"], "content": m["content"]} for m in msgs[-max_history:]]
    question_id = conn.execute(
        "INSERT INTO ChatMessage (session_id, parent_id, role, content) VALUES (?, ?, 'user', ?)",
        (sess["id"], msgs[-1]["id"] if msgs else None, text),
    ).lastrowid
    conn.execute("UPDATE ChatSession SET leaf_id = ? WHERE id = ?", (question_id, sess["id"]))
    return sess["id"], question_id, history


def save_reply(session_id: int, question_id: int, reply: str, steps: list[dict]) -> None:
    """Store the answer; it only becomes the visible leaf if the user didn't move on meanwhile."""
    if not reply and not steps:
        return
    with db.get_conn() as conn:
        reply_id = conn.execute(
            "INSERT INTO ChatMessage (session_id, parent_id, role, content, steps) VALUES (?, ?, 'assistant', ?, ?)",
            (session_id, question_id, reply, json.dumps(steps, ensure_ascii=False) if steps else None),
        ).lastrowid
        conn.execute("UPDATE ChatSession SET leaf_id = ?, updated_at = datetime('now') "
                     "WHERE id = ? AND leaf_id = ?", (reply_id, session_id, question_id))
