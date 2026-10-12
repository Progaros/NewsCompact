import sqlite3

import sqlite_vec

from app import config, db
from conftest import topic_vector

POST = "Reading time: 2 min | Post permalink " + "Real text. " * 40 + "Top comments a Comment permalink b Comment permalink"


def _legacy_db(path):
    """A database as created before the v1/v2 migrations."""
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.executescript("""
        CREATE TABLE Feed (id INTEGER PRIMARY KEY, title TEXT NOT NULL, xml_url TEXT NOT NULL UNIQUE,
                           site_url TEXT, category TEXT, last_fetched_at TEXT, etag TEXT, last_modified TEXT);
        CREATE TABLE Article (id INTEGER PRIMARY KEY, feed_id INTEGER, story_id INTEGER, title TEXT,
                              url TEXT UNIQUE, author TEXT, content_raw TEXT, hero_image_url TEXT,
                              published_date TEXT, created_at TEXT, quality REAL, content_class TEXT);
        CREATE TABLE Story (id INTEGER PRIMARY KEY, headline TEXT, summary TEXT, tags TEXT, metrics TEXT,
                            quality_score REAL, created_at TEXT, updated_at TEXT);
        INSERT INTO Feed (id, title, xml_url) VALUES (1, 'f', 'https://f/rss');
        CREATE TABLE FeedbackLog (id INTEGER PRIMARY KEY,
                                  story_id INTEGER NOT NULL REFERENCES Story(id) ON DELETE CASCADE,
                                  kind TEXT NOT NULL, created_at TEXT);
        INSERT INTO Story (id, headline, tags) VALUES (5, 'Voted story', '["ai"]');
        INSERT INTO FeedbackLog (story_id, kind) VALUES (5, 'up');
    """)
    conn.execute(f"CREATE VIRTUAL TABLE Experiment_Embeddings USING vec0(article_id INTEGER, embedding FLOAT[{config.EMBED_DIM}])")
    conn.execute(f"CREATE VIRTUAL TABLE Article_Embeddings USING vec0(article_id INTEGER, embedding FLOAT[{config.EMBED_DIM}])")
    conn.execute("INSERT INTO Article (id, feed_id, title, content_raw, content_class) VALUES (1, 1, 'a', ?, 'broken')", (POST,))
    conn.execute("INSERT INTO Article (id, feed_id, title, content_raw, content_class) VALUES (2, 1, 'b', ?, 'broken')",
                 ("".join(chr(c) for c in range(1, 31)) * 10,))
    conn.execute("INSERT INTO Article_Embeddings (article_id, embedding) VALUES (1, ?)",
                 (sqlite_vec.serialize_float32(topic_vector("a")),))
    conn.commit()
    conn.close()


def test_legacy_database_is_migrated(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    db.init_db()
    db.init_db()  # idempotent
    with db.get_conn() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'Experiment_Embeddings'").fetchone() is None
        assert conn.execute("SELECT count(*) FROM pragma_foreign_key_list('FeedbackLog')").fetchone()[0] == 0
        assert tuple(conn.execute("SELECT headline, kind FROM FeedbackLog").fetchone()) == ("Voted story", "up")
        assert conn.execute("SELECT title_key FROM Article WHERE id = 1").fetchone()[0] is None  # 'a' too short
        rows = {r["id"]: r for r in conn.execute("SELECT id, content_raw, content_class FROM Article")}
        assert rows[1]["content_class"] == "ok" and rows[1]["content_raw"].startswith("Real text.")
        assert rows[2]["content_class"] == "empty" and rows[2]["content_raw"] == ""
        assert conn.execute("SELECT count(*) FROM Article_Vectors").fetchone()[0] == 1
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'Article_Embeddings'").fetchone() is None
        cols = {r[1] for r in conn.execute("PRAGMA table_info(Feed)")}
        assert {"enabled", "last_error"} <= cols
