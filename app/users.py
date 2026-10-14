"""User administration (admins only; authorization happens in the API middleware).

Rule violations raise ValueError; unknown users return None.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import auth, db


def _list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT u.id, u.name, u.is_admin = 1 AS is_admin, u.created_at, "
        "(SELECT count(*) FROM FeedbackLog f WHERE f.user_id = u.id) AS votes, "
        "(SELECT count(*) FROM StoryRead r WHERE r.user_id = u.id) AS reads, "
        "(SELECT count(*) FROM ChatSession c WHERE c.user_id = u.id) AS chats "
        "FROM AppUser u ORDER BY u.id")]


def list_users() -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        return _list(conn)


def create(name: str, password: str, is_admin: bool) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        try:
            conn.execute("INSERT INTO AppUser (name, password_hash, is_admin) VALUES (?, ?, ?)",
                         (name, auth.hash_password(password), int(is_admin)))
        except sqlite3.IntegrityError:
            raise ValueError(f"user name already taken: {name}") from None
        return _list(conn)


def update(user_id: int, actor_id: int, *, is_admin: bool | None = None,
           password: str | None = None) -> list[dict[str, Any]] | None:
    with db.get_conn() as conn:
        target = conn.execute("SELECT id, is_admin FROM AppUser WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            return None
        if is_admin is False and target["is_admin"]:
            if user_id == auth.OWNER_ID:
                raise ValueError("the owner (user 1) stays an admin")
            if user_id == actor_id:
                raise ValueError("you can't remove your own admin rights")
        if is_admin is not None:
            conn.execute("UPDATE AppUser SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
        if password is not None:
            conn.execute("UPDATE AppUser SET password_hash = ? WHERE id = ?", (auth.hash_password(password), user_id))
        users = _list(conn)
    if password is not None and user_id != actor_id:
        auth.end_user_sessions(user_id)
    return users


def delete(user_id: int, actor_id: int) -> list[dict[str, Any]] | None:
    if user_id in (auth.OWNER_ID, actor_id):
        raise ValueError("the owner and your own account can't be deleted")
    with db.get_conn() as conn:
        if conn.execute("SELECT 1 FROM AppUser WHERE id = ?", (user_id,)).fetchone() is None:
            return None
        conn.execute("DELETE FROM FeedbackLog WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM UserProfile WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM AppUser WHERE id = ?", (user_id,))  # cascades to reads + chats
        users = _list(conn)
    auth.end_user_sessions(user_id)
    return users
