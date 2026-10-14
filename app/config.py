"""Application settings, all overridable via environment variables.

Background for the tuned values (thresholds, weights) lives in docs/findings.md.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _list(name: str, default: str) -> list[str]:
    return [p.strip() for p in os.getenv(name, default).split(",") if p.strip()]


def _weights(name: str, default: str) -> dict[str, float]:
    """``"A=0.3,B=0.5"`` -> ``{"A": 0.3, "B": 0.5}``."""
    out = {}
    for part in _list(name, default):
        key, _, value = part.rpartition("=")
        if key.strip():
            out[key.strip()] = float(value)
    return out


# --- Paths / server --------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(_env("NEWS_DB_PATH", str(BASE_DIR / "news.db")))
PID_FILE = Path(_env("NEWS_PID_FILE", str(BASE_DIR / "news.pid")))
HOST = _env("NEWS_HOST", "0.0.0.0")
PORT = _int("NEWS_PORT", 8787)
PUBLIC_BASE = _env("NEWS_PUBLIC_BASE", "http://127.0.0.1:8787")
# Reverse proxies whose X-Forwarded-For/-Proto are trusted (comma-separated IPs, "*" = any).
FORWARDED_ALLOW_IPS = _env("NEWS_FORWARDED_ALLOW_IPS", "127.0.0.1")
LOG_LEVEL = _env("NEWS_LOG_LEVEL", "INFO")
# Rotating log file (gzip-compressed backups); empty = log to stderr only.
LOG_FILE = _env("NEWS_LOG_FILE", "")
LOG_MAX_MB = _float("NEWS_LOG_MAX_MB", 10)
LOG_BACKUPS = _int("NEWS_LOG_BACKUPS", 10)

# --- Ollama (models are fixed for this deployment) -------------------------
OLLAMA_BASE_URL = _env("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
CHAT_MODEL = _env("NEWS_CHAT_MODEL", "qwen3:27b")
EMBED_MODEL = _env("NEWS_EMBED_MODEL", "qwen3-embedding:0.6b-CPU")
EMBED_DIM = _int("NEWS_EMBED_DIM", 1024)
# Keeps both models loaded between worker cycles (Ollama's default 5 min is
# shorter than a cycle, so the first call of each cycle paid a reload). "" = server default.
OLLAMA_KEEP_ALIVE = _env("NEWS_OLLAMA_KEEP_ALIVE", "15m")
OLLAMA_TIMEOUT = _float("NEWS_OLLAMA_TIMEOUT", 300)
HARNESS_TIMEOUT = _float("NEWS_HARNESS_TIMEOUT", 120)

# --- Worker ----------------------------------------------------------------
# Ingest runs every WORKER_INTERVAL seconds (start-to-start). The pipeline runs
# on its own thread and is woken as soon as a feed commits new articles.
WORKER_INTERVAL_SECONDS = _int("NEWS_WORKER_INTERVAL", 300)
PIPELINE_BATCH = _int("NEWS_PIPELINE_BATCH", 50)
EMBED_BATCH = _int("NEWS_EMBED_BATCH", 16)  # texts per Ollama /api/embed call at batch start
# Pending articles of categories with SOURCE_TRUST below this are processed after the rest.
BACKLOG_LOW_TRUST_BELOW = _float("NEWS_BACKLOG_LOW_TRUST_BELOW", 0.5)
# Articles failing this many pipeline runs are parked (dead letter).
PIPELINE_MAX_FAILURES = _int("NEWS_PIPELINE_MAX_FAILURES", 3)

# --- Ingestion -------------------------------------------------------------
FETCH_CONCURRENCY = _int("NEWS_FETCH_CONCURRENCY", 16)
FETCH_TIMEOUT = _float("NEWS_FETCH_TIMEOUT", 20)
USER_AGENT = _env(
    "NEWS_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 NewsAggregator/1.0",
)
# Full-text extraction only when the RSS description is shorter than this (words).
EXTRACT_MIN_DESC_WORDS = _int("NEWS_EXTRACT_MIN_DESC_WORDS", 100)
MAX_FEEDS_PER_RUN = _int("NEWS_MAX_FEEDS_PER_RUN", 0)  # 0 = all
MAX_ITEMS_PER_FEED = _int("NEWS_MAX_ITEMS_PER_FEED", 10)
# Reddit / Hacker News (upvoterss) comments kept as part of the article body.
SOCIAL_MAX_COMMENTS = _int("NEWS_SOCIAL_MAX_COMMENTS", 10)
# Cross-feed duplicates (same canonical URL, or same title of at least N words,
# within the window) skip the AI pipeline and join the original's story.
DEDUP_WINDOW_HOURS = _int("NEWS_DEDUP_WINDOW_HOURS", 72)
DEDUP_TITLE_MIN_WORDS = _int("NEWS_DEDUP_TITLE_MIN_WORDS", 5)

# --- Retention -------------------------------------------------------------
# Stories without an update for this many days are deleted with their articles;
# unassigned articles (pending/skipped/disabled feeds) are deleted after the same age.
# 0 disables retention.
RETENTION_DAYS = _float("NEWS_RETENTION_DAYS", 7)
RETENTION_INTERVAL_HOURS = _float("NEWS_RETENTION_INTERVAL_HOURS", 1)

# --- Clustering ------------------------------------------------------------
# "two_stage": cosine retrieval + LLM event verifier (locked, F0.5 0.774).
# "cosine": member-anchor cosine only — rollback when Ollama is unavailable.
CLUSTER_MODE = _env("NEWS_CLUSTER_MODE", "two_stage")
CLUSTER_WINDOW_HOURS = _int("NEWS_CLUSTER_WINDOW_HOURS", 48)
CLUSTER_SIM_THRESHOLD = _float("NEWS_CLUSTER_SIM_THRESHOLD", 0.64)  # cosine-mode join
CLUSTER_REVIVE_SIM_THRESHOLD = _float("NEWS_CLUSTER_REVIVE_SIM_THRESHOLD", 0.62)  # stale story
CLUSTER_VERIFY_RETRIEVE_THRESHOLD = _float("NEWS_CLUSTER_VERIFY_RETRIEVE_THRESHOLD", 0.55)
CLUSTER_VERIFY_MAX_CANDIDATES = _int("NEWS_CLUSTER_VERIFY_MAX_CANDIDATES", 3)
CLUSTER_VERIFY_TIMEOUT_SECONDS = _float("NEWS_CLUSTER_VERIFY_TIMEOUT_SECONDS", 60)
KNN_CANDIDATES = _int("NEWS_KNN_CANDIDATES", 200)  # nearest articles fetched from the vec0 index
MAX_CANDIDATE_STORIES = _int("NEWS_MAX_CENTROID_STORIES", 12)

# Recall merge: when the queue is idle, re-check a few recently updated stories
# against similar stories and merge verified duplicates (bounded LLM spend).
MERGE_ENABLED = _env("NEWS_MERGE_ENABLED", "1") == "1"
MERGE_SIM_THRESHOLD = _float("NEWS_MERGE_SIM_THRESHOLD", 0.62)
MERGE_MAX_CHECKS_PER_RUN = _int("NEWS_MERGE_MAX_CHECKS_PER_RUN", 5)
MERGE_ANCHORS_PER_STORY = _int("NEWS_MERGE_ANCHORS_PER_STORY", 3)
# Lexical channel: stories sharing this many headline terms that occur in at most
# MERGE_LEXICAL_MAX_DF window stories are verified too (catches cross-language copies).
MERGE_LEXICAL_MIN_TERMS = _int("NEWS_MERGE_LEXICAL_MIN_TERMS", 3)
MERGE_LEXICAL_MAX_DF = _int("NEWS_MERGE_LEXICAL_MAX_DF", 5)

# --- Story generation ------------------------------------------------------
# Cap for headline-only quality scores: a headline can't show factual density.
TITLE_QUALITY_MAX = _int("NEWS_TITLE_QUALITY_MAX", 6)
# A thin article joining a story at/below this quality skips harness + Prompt2.
JUNK_SKIP_QUALITY_THRESHOLD = _float("NEWS_JUNK_SKIP_QUALITY_THRESHOLD", 2.0)
# Combined article text budget for full Prompt2 runs before older items are compressed.
CONTEXT_BUDGET_CHARS = _int("NEWS_CONTEXT_BUDGET_CHARS", 200_000)
# Update significance (0 none … 3 major, story.py) at which a read story shows up again as updated.
READ_RESURFACE_SIGNIFICANCE = _int("NEWS_READ_RESURFACE_SIGNIFICANCE", 2)

# --- Scoring ---------------------------------------------------------------
# Stories without an update for this many days drop out of the ranked feed into
# the archive (until retention deletes them; a revived story comes back).
STORY_ACTIVE_DAYS = _float("NEWS_STORY_ACTIVE_DAYS", 2)
# display = base + urgency*W1 + clamp(interest*W3, ±INTEREST_CAP) + distance*W_DISTANCE - decay
W1_URGENCY = _float("NEWS_W1", 1.0)
W3_INTEREST = _float("NEWS_W3", 1.5)
INTEREST_CAP = _float("NEWS_INTEREST_CAP", 10.0)
W_DISTANCE = _float("NEWS_W_DISTANCE", 2.0)
# Relevance base weights (sum to 1 so the base stays 0-10).
W_RELEVANCE_QUALITY = _float("NEWS_W_REL_QUALITY", 0.40)
W_RELEVANCE_CREDIBILITY = _float("NEWS_W_REL_CREDIBILITY", 0.35)
W_RELEVANCE_IMPACT = _float("NEWS_W_REL_IMPACT", 0.25)
# Trust (0..1) of a feed category in the credibility term; ⚙ Preferences overrides per category.
SOURCE_TRUST = _weights("NEWS_SOURCE_TRUST", "Reddit=0.3,Hacker News=0.5,Reviews=0.5")
SOURCE_TRUST_DEFAULT = _float("NEWS_SOURCE_TRUST_DEFAULT", 1.0)
TIME_DECAY_HALF_LIFE_HOURS = _float("NEWS_DECAY_HALF_LIFE_HOURS", 18)
TIME_DECAY_MAX = _float("NEWS_DECAY_MAX", 10.0)
FEEDBACK_ALPHA = _float("NEWS_FEEDBACK_ALPHA", 0.1)
USER_CITY = _env("NEWS_USER_CITY", "Munich")
USER_COUNTRY = _env("NEWS_USER_COUNTRY", "Germany")
USER_LANGUAGE = _env("NEWS_USER_LANGUAGE", "de")

# --- Heads-up brief + scheduled ntfy reports ---------------------------------
HEADS_UP_TTL_MINUTES = _int("NEWS_HEADS_UP_TTL_MINUTES", 5)
HEADS_UP_NTFY_URL = _env("NEWS_HEADS_UP_NTFY_URL", "https://ntfy.sh/your-topic")  # empty disables all pushes
HEADS_UP_NTFY_TITLE = _env("NEWS_HEADS_UP_NTFY_TITLE", "News Heads Up")
# Daily brief times (server-local HH:MM) until schedules are saved in ⚙ Preferences.
HEADS_UP_NTFY_TIMES = _list("NEWS_HEADS_UP_NTFY_TIMES", "07:00")

# --- Auth (empty password = open mode) -------------------------------------
AUTH_PASSWORD = _env("NEWS_AUTH_PASSWORD", "")
AUTH_SESSION_HOURS = _float("NEWS_AUTH_SESSION_HOURS", 24)
AUTH_COOKIE = _env("NEWS_AUTH_COOKIE", "news_session")

# --- Per-story chat --------------------------------------------------------
CHAT_TEMPERATURE = _float("NEWS_CHAT_TEMPERATURE", 0.3)
CHAT_MAX_TOKENS = _int("NEWS_CHAT_MAX_TOKENS", 1024)
# Safety cap on autonomous search+scrape turns per message when web search is enabled.
CHAT_SEARCH_MAX_TURNS = _int("NEWS_CHAT_SEARCH_MAX_TURNS", 8)
CHAT_CONTEXT_PER_SOURCE = _int("NEWS_CHAT_CONTEXT_PER_SOURCE", 1500)
CHAT_CONTEXT_MAX = _int("NEWS_CHAT_CONTEXT_MAX", 6000)

# --- searchngx (SearXNG web search for chat) ---------------------------------
SEARCHNGX_BASE_URL = _env("NEWS_SEARCHNGX_BASE_URL", "https://searx.example.org/")
SEARCHNGX_API_KEY = _env("NEWS_SEARCHNGX_API_KEY", "")
SEARCHNGX_ENDPOINT = _env("NEWS_SEARCHNGX_ENDPOINT", "search")
SEARCHNGX_CATEGORIES = _env("NEWS_SEARCHNGX_CATEGORIES", "general,news")
# Retried with these engines when the default ones return nothing (CAPTCHAs, rate limits).
SEARCHNGX_FALLBACK_ENGINES = _env("NEWS_SEARCHNGX_FALLBACK_ENGINES", "bing,bing news")
SEARCHNGX_TIMEOUT = _float("NEWS_SEARCHNGX_TIMEOUT", 30)
SEARCHNGX_MAX_RESULTS = _int("NEWS_SEARCHNGX_MAX_RESULTS", 5)
