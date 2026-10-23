"""SQLite + sqlite-vec connections, schema bootstrap, small shared helpers.

Connections run in autocommit mode: long LLM/HTTP calls between statements
must never hold a write transaction (that caused "database is locked").
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import struct
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import sqlite_vec

from . import config, dedup, tags
from .content_quality import classify as classify_content
from .content_quality import clean_body

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Columns added after the first release: (table, column, type).
_MIGRATIONS = (
    ("Article", "quality", "REAL"),
    ("Article", "content_class", "TEXT"),
    ("Article", "pipeline_failures", "INTEGER"),
    ("Article", "pipeline_timeouts", "INTEGER"),
    ("Article", "retry_after", "TEXT"),
    ("Article", "processed_at", "TEXT"),
    ("Article", "t_embed_s", "REAL"),
    ("Article", "t_cluster_s", "REAL"),
    ("Article", "t_harness_s", "REAL"),
    ("Article", "t_story_s", "REAL"),
    ("Article", "t_wall_s", "REAL"),
    ("Article", "t_merge_s", "REAL"),
    ("Article", "duplicate_of", "INTEGER"),
    ("Article", "url_key", "TEXT"),
    ("Article", "title_key", "TEXT"),
    ("Story", "merge_checked_at", "TEXT"),
    ("Story", "split_checked_at", "TEXT"),
    ("Story", "material_at", "TEXT"),
    ("Story", "news_at", "TEXT"),
    ("Feed", "enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("Feed", "last_error", "TEXT"),
    ("Feed", "fetch_failures", "INTEGER NOT NULL DEFAULT 0"),
    ("Feed", "next_fetch_at", "TEXT"),
    ("ChatMessage", "steps", "TEXT"),
    ("ChatMessage", "parent_id", "INTEGER"),
    ("ChatSession", "leaf_id", "INTEGER"),
    ("UserProfile", "feed_weights", "TEXT DEFAULT '{}'"),
    ("FeedbackLog", "user_id", "INTEGER NOT NULL DEFAULT 1"),
)
# Indexes on migrated columns (schema.sql runs before the columns exist on old DBs).
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_article_url_key ON Article(url_key)",
    "CREATE INDEX IF NOT EXISTS idx_article_title_key ON Article(title_key)",
    "CREATE INDEX IF NOT EXISTS idx_article_duplicate ON Article(duplicate_of)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_user ON FeedbackLog(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_story_news ON Story(news_at)",
    # Ranking filters on news_at alone: any story that has a headline must have one.
    "CREATE TRIGGER IF NOT EXISTS story_news_at_insert AFTER INSERT ON Story "
    "WHEN NEW.news_at IS NULL AND NEW.headline IS NOT NULL BEGIN "
    "UPDATE Story SET news_at = COALESCE(NEW.updated_at, datetime('now')) WHERE id = NEW.id; END",
    "CREATE TRIGGER IF NOT EXISTS story_news_at_update AFTER UPDATE OF headline ON Story "
    "WHEN NEW.news_at IS NULL AND NEW.headline IS NOT NULL BEGIN "
    "UPDATE Story SET news_at = COALESCE(NEW.updated_at, datetime('now')) WHERE id = NEW.id; END",
)

VECTOR_TABLE = "Article_Vectors"


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


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _clean_bodies(conn: sqlite3.Connection) -> None:
    """v1: strip upvoterss chrome / binary payloads and reclassify (re-pends wrongly 'broken' rows)."""
    rows = conn.execute(
        "SELECT id, content_raw FROM Article "
        "WHERE content_class = 'broken' OR content_raw LIKE '%Post permalink%'"
    ).fetchall()
    for r in rows:
        body = clean_body(r["content_raw"])
        conn.execute("UPDATE Article SET content_raw = ?, content_class = ? WHERE id = ?",
                     (body, classify_content(body), r["id"]))


def _create_vector_table(conn: sqlite3.Connection) -> None:
    """v2: primary-keyed cosine vec0 table (fast KNN + point lookups); copies the old table."""
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {VECTOR_TABLE} USING vec0("
        f"article_id INTEGER PRIMARY KEY, embedding FLOAT[{config.EMBED_DIM}] distance_metric=cosine)"
    )
    if _table_exists(conn, "Article_Embeddings"):
        conn.execute(
            f"INSERT INTO {VECTOR_TABLE} (article_id, embedding) "
            "SELECT article_id, embedding FROM Article_Embeddings "
            "WHERE article_id IN (SELECT id FROM Article) GROUP BY article_id"
        )
        conn.execute("DROP TABLE Article_Embeddings")


def _dedup_and_retention(conn: sqlite3.Connection) -> None:
    """v3: dedupe keys for existing rows; votes survive story deletion; drop the experiment table."""
    for r in conn.execute("SELECT id, url, title FROM Article").fetchall():
        conn.execute("UPDATE Article SET url_key = ?, title_key = ? WHERE id = ?",
                     (dedup.url_key(r["url"]), dedup.title_key(r["title"]), r["id"]))
    if conn.execute("SELECT 1 FROM pragma_foreign_key_list('FeedbackLog')").fetchone():
        conn.execute(
            "CREATE TABLE FeedbackLog_new (id INTEGER PRIMARY KEY, story_id INTEGER, headline TEXT, "
            "tags TEXT, kind TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT INTO FeedbackLog_new (id, story_id, headline, tags, kind, created_at) "
            "SELECT f.id, f.story_id, s.headline, s.tags, f.kind, f.created_at "
            "FROM FeedbackLog f LEFT JOIN Story s ON s.id = f.story_id"
        )
        conn.execute("DROP TABLE FeedbackLog")
        conn.execute("ALTER TABLE FeedbackLog_new RENAME TO FeedbackLog")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_story ON FeedbackLog(story_id)")
    if _table_exists(conn, "Experiment_Embeddings"):
        conn.execute("DROP TABLE Experiment_Embeddings")


def _chat_tree(conn: sqlite3.Connection) -> None:
    """v4: link the linear chat histories into a message tree (each message's parent = previous one)."""
    conn.execute(
        "UPDATE ChatMessage SET parent_id = (SELECT MAX(p.id) FROM ChatMessage p "
        "WHERE p.session_id = ChatMessage.session_id AND p.id < ChatMessage.id)"
    )
    conn.execute("UPDATE ChatSession SET leaf_id = (SELECT MAX(id) FROM ChatMessage m WHERE m.session_id = ChatSession.id)")


def _news_at(conn: sqlite3.Connection) -> None:
    """v5: existing stories start with their last update as the news time."""
    conn.execute("UPDATE Story SET news_at = updated_at WHERE news_at IS NULL")


def _per_user(conn: sqlite3.Connection) -> None:
    """v6: read state and chats become per user; the single-user data belongs to user 1."""
    if "user_id" not in {r[1] for r in conn.execute("PRAGMA table_info(FeedbackLog)")}:  # v3 rebuilt it
        conn.execute("ALTER TABLE FeedbackLog ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user ON FeedbackLog(user_id)")
    if "user_id" not in {r[1] for r in conn.execute("PRAGMA table_info(StoryRead)")}:
        conn.execute(
            "CREATE TABLE StoryRead_new (user_id INTEGER NOT NULL DEFAULT 1 REFERENCES AppUser(id) ON DELETE CASCADE, "
            "story_id INTEGER NOT NULL REFERENCES Story(id) ON DELETE CASCADE, "
            "read_at TEXT NOT NULL DEFAULT (datetime('now')), summary TEXT, PRIMARY KEY (user_id, story_id))")
        conn.execute("INSERT INTO StoryRead_new (user_id, story_id, read_at, summary) "
                     "SELECT 1, story_id, read_at, summary FROM StoryRead")
        conn.execute("DROP TABLE StoryRead")
        conn.execute("ALTER TABLE StoryRead_new RENAME TO StoryRead")
    if "user_id" not in {r[1] for r in conn.execute("PRAGMA table_info(ChatSession)")}:
        conn.execute(
            "CREATE TABLE ChatSession_new (id INTEGER PRIMARY KEY, "
            "user_id INTEGER NOT NULL DEFAULT 1 REFERENCES AppUser(id) ON DELETE CASCADE, "
            "story_id INTEGER NOT NULL REFERENCES Story(id) ON DELETE CASCADE, leaf_id INTEGER, "
            "created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')), "
            "UNIQUE (user_id, story_id))")
        conn.execute("INSERT INTO ChatSession_new (id, user_id, story_id, leaf_id, created_at, updated_at) "
                     "SELECT id, 1, story_id, leaf_id, created_at, updated_at FROM ChatSession")
        conn.execute("DROP TABLE ChatSession")
        conn.execute("ALTER TABLE ChatSession_new RENAME TO ChatSession")


def _drop_read_summary(conn: sqlite3.Connection) -> None:
    """v7: "what changed" comes from StoryUpdate; the summary snapshot at read time is unused."""
    if "summary" in {r[1] for r in conn.execute("PRAGMA table_info(StoryRead)")}:
        conn.execute("ALTER TABLE StoryRead DROP COLUMN summary")


def _explicit_tag_weights(conn: sqlite3.Connection) -> None:
    """v8: 👍/👎 stop writing the tag list (ranking derives inferred weights from FeedbackLog).

    A stored weight that equals exactly what the user's votes added (±1 per tag and vote) was
    made by votes alone and is dropped; anything the user set or changed by hand stays.
    """
    for uid, raw in conn.execute("SELECT id, tag_weights FROM UserProfile").fetchall():
        weights = json_dict(raw)
        votes: dict[str, float] = {}
        for kind, story_tags in conn.execute("SELECT kind, tags FROM FeedbackLog WHERE user_id = ?", (uid,)):
            for tag in tags.normalize_tags([t for t in json_list(story_tags) if isinstance(t, str)]):
                votes[tag] = votes.get(tag, 0.0) + (1.0 if kind == "up" else -1.0 if kind == "down" else 0.0)
        keep = {}
        for tag, w in weights.items():
            vote_sum = votes.get(tags.normalize_tag(tag), 0.0)
            if not (vote_sum and isinstance(w, (int, float)) and abs(w - vote_sum) < 1e-6):
                keep[tag] = w
        if keep != weights:
            conn.execute("UPDATE UserProfile SET tag_weights = ? WHERE id = ?", (json.dumps(keep), uid))


# (user_version, migration) — each runs once, inside a transaction (foreign keys off,
# so table rebuilds don't cascade).
_DATA_MIGRATIONS = ((1, _clean_bodies), (2, _create_vector_table), (3, _dedup_and_retention), (4, _chat_tree),
                    (5, _news_at), (6, _per_user), (7, _drop_read_summary), (8, _explicit_tag_weights))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def init_db() -> None:
    """Create/upgrade the schema (idempotent)."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        for table, column, ddl in _MIGRATIONS:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        for ddl in _INDEXES:
            conn.execute(ddl)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("PRAGMA foreign_keys=OFF")
        for target, migrate in _DATA_MIGRATIONS:
            if version < target:
                conn.execute("BEGIN")
                try:
                    migrate(conn)
                    conn.execute(f"PRAGMA user_version = {target}")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        conn.execute("PRAGMA foreign_keys=ON")
        tags.load_aliases(conn)
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS article_vector_delete AFTER DELETE ON Article "
            f"BEGIN DELETE FROM {VECTOR_TABLE} WHERE article_id = OLD.id; END"
        )
        conn.execute(f"DELETE FROM {VECTOR_TABLE} WHERE article_id NOT IN (SELECT id FROM Article)")
        # One vote per user and story (older code could log a vote twice).
        conn.execute("DELETE FROM FeedbackLog WHERE story_id IS NOT NULL AND id NOT IN "
                     "(SELECT MAX(id) FROM FeedbackLog WHERE story_id IS NOT NULL GROUP BY user_id, story_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_user_story "
                     "ON FeedbackLog(user_id, story_id) WHERE story_id IS NOT NULL")
        conn.execute("UPDATE Story SET news_at = COALESCE(updated_at, created_at) "
                     "WHERE news_at IS NULL AND headline IS NOT NULL")
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
        for row in conn.execute(
            "SELECT id, content_raw FROM Article WHERE content_class IS NULL"
        ).fetchall():
            conn.execute(
                "UPDATE Article SET content_class = ? WHERE id = ?",
                (classify_content(row["content_raw"]), row["id"]),
            )
        # One-time rebuild so retention can hand freed pages back with incremental_vacuum.
        if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.execute("VACUUM")


def reclaim_space() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA incremental_vacuum").fetchall()  # each step frees a page
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# --- helpers ---------------------------------------------------------------
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
