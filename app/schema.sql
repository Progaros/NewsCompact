-- Schema for the AI News Aggregator (applied idempotently by app/db.py).
-- Timestamps are naive UTC strings 'YYYY-MM-DD HH:MM:SS'.
-- Columns added after the first release are also listed in db._MIGRATIONS.

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
    enabled         INTEGER NOT NULL DEFAULT 1,   -- 0 = fetch RSS rows only, no AI
    last_error      TEXT                          -- NULL after a successful fetch
);

CREATE TABLE IF NOT EXISTS Article (
    id                INTEGER PRIMARY KEY,
    feed_id           INTEGER NOT NULL REFERENCES Feed(id) ON DELETE CASCADE,
    story_id          INTEGER REFERENCES Story(id) ON DELETE SET NULL,  -- NULL = pending
    title             TEXT,
    url               TEXT UNIQUE,
    author            TEXT,
    content_raw       TEXT,
    content_class     TEXT,      -- ok | thin | broken | empty (app/content_quality.py)
    hero_image_url    TEXT,
    published_date    TEXT,
    created_at        TEXT DEFAULT (datetime('now')),   -- first seen (ingest)
    quality           REAL,
    pipeline_failures INTEGER,
    processed_at      TEXT,      -- pipeline finished (story written)
    t_embed_s         REAL,      -- per-stage wall times of the successful run
    t_cluster_s       REAL,
    t_harness_s       REAL,
    t_story_s         REAL,
    t_wall_s          REAL,
    t_merge_s         REAL,      -- time of the recall merge that moved it (if any)
    duplicate_of      INTEGER,   -- cross-feed copy of this article (skips the AI pipeline)
    url_key           TEXT,      -- canonical URL (dedup.url_key)
    title_key         TEXT       -- normalized title (dedup.title_key)
);

CREATE INDEX IF NOT EXISTS idx_article_story   ON Article(story_id);
CREATE INDEX IF NOT EXISTS idx_article_feed    ON Article(feed_id);
CREATE INDEX IF NOT EXISTS idx_article_created ON Article(created_at);

CREATE TABLE IF NOT EXISTS Story (
    id            INTEGER PRIMARY KEY,
    headline      TEXT,
    summary       TEXT,
    tags          TEXT DEFAULT '[]',
    metrics       TEXT DEFAULT '{}',   -- urgency_level, impact_scale, status, location, language
    quality_score REAL DEFAULT 5.0,    -- trust-weighted median of member article qualities
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    merge_checked_at TEXT,             -- last recall-merge check (re-checked after updates)
    material_at   TEXT,                -- last update significant enough to resurface a read story
    news_at       TEXT                 -- last update that added facts (ranking recency)
);

CREATE INDEX IF NOT EXISTS idx_story_updated ON Story(updated_at);

-- Accounts. Stories, summaries and classifications are shared; profile, votes, read
-- state, chats and personal briefs are per user. User 1 (admin) owns the single-user era
-- data and logs in with NEWS_AUTH_PASSWORD until it sets a password of its own.
CREATE TABLE IF NOT EXISTS AppUser (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT,                       -- scrypt (app/auth.py); NULL = NEWS_AUTH_PASSWORD (user 1 only)
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO AppUser (id, name, is_admin) VALUES (1, 'admin', 1);

-- One row per user (id = AppUser.id), created on first use.
CREATE TABLE IF NOT EXISTS UserProfile (
    id             INTEGER PRIMARY KEY,
    tag_weights    TEXT DEFAULT '{}',
    source_weights TEXT DEFAULT '{}',
    feed_weights   TEXT DEFAULT '{}',   -- per-feed trust overrides {feed_id: 0..1}
    preferences    TEXT DEFAULT '{}'
);

INSERT OR IGNORE INTO UserProfile (id, tag_weights, source_weights, preferences)
VALUES (1, '{}', '{}', '{"min_quality": 0.0, "summary_length": "medium"}');

-- Raw votes, kept separate from UserProfile so weights can be re-derived later.
-- Headline/tags are snapshotted: votes outlive the story (retention).
CREATE TABLE IF NOT EXISTS FeedbackLog (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL DEFAULT 1,
    story_id   INTEGER,
    headline   TEXT,
    tags       TEXT,
    kind       TEXT    NOT NULL,   -- up | down
    created_at TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_story ON FeedbackLog(story_id);

-- Stories marked read, per user. A story is unread again once
-- COALESCE(Story.material_at, Story.created_at) > read_at.
CREATE TABLE IF NOT EXISTS StoryRead (
    user_id  INTEGER NOT NULL DEFAULT 1 REFERENCES AppUser(id) ON DELETE CASCADE,
    story_id INTEGER NOT NULL REFERENCES Story(id) ON DELETE CASCADE,
    read_at  TEXT NOT NULL DEFAULT (datetime('now')),
    summary  TEXT,                    -- the summary as it was when marked read
    PRIMARY KEY (user_id, story_id)
);

-- URLs of deleted articles, so feeds that still list them don't re-ingest them.
CREATE TABLE IF NOT EXISTS ArticleTombstone (
    url        TEXT PRIMARY KEY,
    deleted_at TEXT DEFAULT (datetime('now'))
);

-- Shared snapshots (/p/<token>): short tokens for default-view content, long unguessable
-- ones for personal content (personal brief, chat answers).
CREATE TABLE IF NOT EXISTS ShareLink (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES AppUser(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,   -- brief | chat
    payload    TEXT    NOT NULL,   -- JSON snapshot
    created_at TEXT    DEFAULT (datetime('now'))
);

-- Per-story chat: one session per user and story. Messages form a tree: editing a
-- message adds a sibling (same parent_id); leaf_id is the end of the branch on screen.
CREATE TABLE IF NOT EXISTS ChatSession (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL DEFAULT 1 REFERENCES AppUser(id) ON DELETE CASCADE,
    story_id   INTEGER NOT NULL REFERENCES Story(id) ON DELETE CASCADE,
    leaf_id    INTEGER,
    created_at TEXT    DEFAULT (datetime('now')),
    updated_at TEXT    DEFAULT (datetime('now')),
    UNIQUE (user_id, story_id)
);

CREATE TABLE IF NOT EXISTS ChatMessage (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES ChatSession(id) ON DELETE CASCADE,
    parent_id  INTEGER,        -- previous message in the branch (NULL = first message)
    role       TEXT    NOT NULL,   -- user | assistant
    content    TEXT    NOT NULL,
    steps      TEXT,           -- assistant: JSON list of agent steps (thinking, searches, reads, errors)
    created_at TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_message_session ON ChatMessage(session_id);

-- Article_Vectors (sqlite-vec vec0, cosine, PK article_id) is created by db.py once the extension is loaded.
