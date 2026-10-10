"""SQLite + sqlite-vec connection management and schema bootstrap.

Each connection loads the vec extension and enables foreign keys. WAL is enabled
so the API and the background worker can share the file. Keep usage patterns
read-only where possible; the vec extension is loaded per-connection (cheap).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from . import config

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _new_conn() -> sqlite3.Connection:
    # autocommit=True: each statement commits immediately. In the default legacy
    # transaction mode, pysqlite opens an implicit transaction on the first write
    # and holds it until an explicit commit() — which, across long-running
    # ingest/pipeline calls (HTTP fetches, LLM calls), made other connections
    # fail with "database is locked". Explicit transactions can still be
    # started with `with conn:` when atomicity is needed.
    db = sqlite3.connect(config.DB_PATH, check_same_thread=False, autocommit=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    db.enable_load_extension(True)
    db.load_extension(sqlite_vec.loadable_path())
    db.enable_load_extension(False)
    return db


@contextmanager
def get_conn():
    """Yield a configured connection; ensures it is closed on exit."""
    db = _new_conn()
    try:
        yield db
    finally:
        db.close()


def pack_floats(vector: list[float]) -> bytes:
    """Serialize a float vector to the raw float32 blob sqlite-vec expects."""
    return sqlite_vec.serialize_float32(vector)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns that postdate a schema version (idempotent).

    ``CREATE TABLE IF NOT EXISTS`` won't touch an existing table, so newly
    added columns (e.g. Article.quality / content_class) are added here for
    databases that predate them.
    """
    for table, column, ddl in (
        ("Article", "quality", "ALTER TABLE Article ADD COLUMN quality REAL"),
        ("Article", "content_class", "ALTER TABLE Article ADD COLUMN content_class TEXT"),
        # Feed management: disabling skips AI ingestion only — RSS fetch + article
        # rows still happen, so volume stats stay live for disabled feeds too.
        ("Feed", "enabled", "ALTER TABLE Feed ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"),
    ):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


def init_db() -> None:
    """Create the schema (idempotent)."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn() as db:
        db.executescript(ddl)
        _ensure_columns(db)
        # vec0 virtual table (guarded — depends on the loaded extension).
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Article_Embeddings'"
        ).fetchone()
        if not exists:
            db.execute(
                f"CREATE VIRTUAL TABLE Article_Embeddings USING vec0("
                f"article_id INTEGER, embedding FLOAT[{config.EMBED_DIM}])"
            )
        db.commit()


def embed_blob(vector: list[float]) -> bytes:
    return pack_floats(vector)
