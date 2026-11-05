"""SQLite + sqlite-vec connections, schema bootstrap, small shared helpers.

Connections run in autocommit mode: long LLM/HTTP calls between statements
must never hold a write transaction (that caused "database is locked").
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlite_vec

from . import config, tags

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

VECTOR_TABLE = "Article_Vectors"
SEARCH_TABLE = "StorySearch"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.enable_load_extension(False)
    return conn


_local = threading.local()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """This thread's connection, opened once per thread and DB path (nested uses share it)."""
    conn = getattr(_local, "conn", None)
    if conn is not None and _local.path != config.DB_PATH and not _local.depth:
        conn.close()
        conn = None
    if conn is None:
        conn = _local.conn = _connect()
        _local.path, _local.depth = config.DB_PATH, 0
    _local.depth += 1
    try:
        yield conn
    finally:
        _local.depth -= 1
        if not _local.depth and conn.in_transaction:  # an error skipped COMMIT/ROLLBACK
            conn.execute("ROLLBACK")


def init_db() -> None:
    """Create the schema and recover interrupted chat answers on startup (idempotent)."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.execute("VACUUM")
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VECTOR_TABLE} USING vec0("
            f"article_id INTEGER PRIMARY KEY, embedding FLOAT[{config.EMBED_DIM}] distance_metric=cosine)"
        )
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS article_vector_delete AFTER DELETE ON Article "
            f"BEGIN DELETE FROM {VECTOR_TABLE} WHERE article_id = OLD.id; END"
        )
        tags.load_aliases(conn)
        for row in conn.execute("SELECT id, content, steps FROM ChatMessage WHERE steps LIKE '%\"running\"%'").fetchall():
            steps = json_list(row["steps"])
            for s in steps:
                if isinstance(s, dict) and s.get("status") == "running":
                    s["status"] = "ok" if s.get("kind") == "thinking" else "warn"
                    if "(stopped)" not in (s.get("title") or ""):
                        s["title"] = f"{s.get('title') or 'Step'} (stopped)"
            if not (row["content"] or "").strip() and not any(
                isinstance(s, dict) and s.get("kind") in ("stopped", "error") for s in steps
            ):
                steps.append({"id": len(steps), "kind": "stopped",
                              "title": "Stopped — answer interrupted", "status": "warn", "detail": ""})
            conn.execute("UPDATE ChatMessage SET steps = ? WHERE id = ?",
                         (json.dumps(steps, ensure_ascii=False), row["id"]))


def reclaim_space() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA incremental_vacuum").fetchall()  # each step frees a page
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# --- helpers ---------------------------------------------------------------
@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """One write transaction, locked up front (connections autocommit otherwise)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def claim_mark(conn: sqlite3.Connection, user_id: int, story_id: int, kind: str, age: float = 0.0) -> str | None:
    """Record a change of the user's ``kind`` mark (read | vote) made ``age`` seconds ago.

    Returns that time, or None if the mark already changed later (a replayed offline click
    that another device overtook): the caller must then leave the mark alone.
    """
    at = conn.execute("SELECT datetime('now', ?)", (f"-{max(0.0, age):.3f} seconds",)).fetchone()[0]
    won = conn.execute(
        "INSERT INTO MarkChange (user_id, story_id, kind, changed_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (user_id, story_id, kind) DO UPDATE SET changed_at = excluded.changed_at "
        "WHERE excluded.changed_at >= MarkChange.changed_at",
        (user_id, story_id, kind, at),
    ).rowcount
    return at if won else None


def utcnow() -> str:
    """Naive-UTC timestamp in SQLite's datetime('now') format."""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def pack_floats(vector: list[float]) -> bytes:
    return sqlite_vec.serialize_float32(vector)


def unpack_floats(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def json_dict(raw: str | None) -> dict[str, Any]:
    try:
        v = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return v if isinstance(v, dict) else {}


def json_list(raw: str | None) -> list[Any]:
    try:
        v = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return v if isinstance(v, list) else []
