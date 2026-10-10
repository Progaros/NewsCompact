-- Schema for the AI News Aggregator.
-- Applied by app/db.py after the sqlite-vec extension is loaded on the connection.
--
-- Note: Article_Embeddings is a sqlite-vec virtual table; its creation depends on
-- the extension being loaded, so db.py guards it separately.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS Feed (
    id              INTEGER PRIMARY KEY,
    title           TEXT    NOT NULL,
    xml_url         TEXT    NOT NULL UNIQUE,
    site_url        TEXT,
    category        TEXT,
    last_fetched_at TEXT,
    etag            TEXT,
    last_modified   TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS Article (
    id             INTEGER PRIMARY KEY,
    feed_id        INTEGER NOT NULL REFERENCES Feed(id) ON DELETE CASCADE,
    story_id       INTEGER REFERENCES Story(id) ON DELETE SET NULL,
    title          TEXT,
    url            TEXT UNIQUE,
    author         TEXT,
    content_raw    TEXT,
    hero_image_url TEXT,
    published_date TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    quality        REAL,
    content_class  TEXT
);

CREATE INDEX IF NOT EXISTS idx_article_story ON Article(story_id);
CREATE INDEX IF NOT EXISTS idx_article_feed  ON Article(feed_id);

CREATE TABLE IF NOT EXISTS Story (
    id                 INTEGER PRIMARY KEY,
    headline           TEXT,
    summary            TEXT,
    tags               TEXT DEFAULT '[]',
    metrics            TEXT DEFAULT '{}',
    quality_score      REAL DEFAULT 5.0,
    created_at         TEXT DEFAULT (datetime('now')),
    updated_at         TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_story_updated ON Story(updated_at);

CREATE TABLE IF NOT EXISTS UserProfile (
    id            INTEGER PRIMARY KEY,
    tag_weights   TEXT DEFAULT '{}',
    source_weights TEXT DEFAULT '{}',
    preferences   TEXT DEFAULT '{"min_quality": 0.0, "summary_length": "medium"}'
);

-- Single local user.
INSERT OR IGNORE INTO UserProfile (id, tag_weights, source_weights, preferences)
VALUES (1, '{}', '{}', '{"min_quality": 0.0, "summary_length": "medium"}');

-- Raw 👍/👎 feedback, kept SEPARATE from UserProfile so the raw signal can be
-- re-derived / the algorithm adjusted later without touching the live profile.
-- (Click/engagement tracking is intentionally NOT recorded here — votes only.)
CREATE TABLE IF NOT EXISTS FeedbackLog (
    id          INTEGER PRIMARY KEY,
    story_id    INTEGER NOT NULL REFERENCES Story(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,
    created_at  TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_story ON FeedbackLog(story_id);

-- The vec0 virtual table (created by db.py once the extension is loaded):
--   CREATE VIRTUAL TABLE Article_Embeddings USING vec0(
--       article_id INTEGER,
--       embedding  FLOAT[1024]
--   );
