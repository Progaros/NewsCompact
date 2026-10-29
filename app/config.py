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
# Pending articles of feeds whose general trust is below this are processed after the rest.
BACKLOG_LOW_TRUST_BELOW = _float("NEWS_BACKLOG_LOW_TRUST_BELOW", 0.5)
# Articles failing this many pipeline runs are parked (dead letter).
PIPELINE_MAX_FAILURES = _int("NEWS_PIPELINE_MAX_FAILURES", 3)
# Ollama timeouts (slow GPU, not a bad article) retry after a doubling delay instead of
# counting as failures; an article is parked only after this many timeouts in a row.
PIPELINE_MAX_TIMEOUTS = _int("NEWS_PIPELINE_MAX_TIMEOUTS", 6)
PIPELINE_TIMEOUT_BACKOFF_MINUTES = _float("NEWS_PIPELINE_TIMEOUT_BACKOFF_MINUTES", 15)

# --- Ingestion -------------------------------------------------------------
FETCH_CONCURRENCY = _int("NEWS_FETCH_CONCURRENCY", 16)
FETCH_TIMEOUT = _float("NEWS_FETCH_TIMEOUT", 20)
USER_AGENT = _env(
    "NEWS_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 NewsAggregator/1.0",
)
# Retry UA for feeds whose bot wall rejects the browser UA (403, or a redirect without Location).
FEED_USER_AGENT = _env("NEWS_FEED_USER_AGENT", f"NewsAggregator/1.0 (RSS reader; +{PUBLIC_BASE})")
# Full-text extraction only when the RSS description is shorter than this (words).
EXTRACT_MIN_DESC_WORDS = _int("NEWS_EXTRACT_MIN_DESC_WORDS", 100)
MAX_FEEDS_PER_RUN = _int("NEWS_MAX_FEEDS_PER_RUN", 0)  # 0 = all
MAX_ITEMS_PER_FEED = _int("NEWS_MAX_ITEMS_PER_FEED", 10)
# A failing feed is retried after WORKER_INTERVAL doubling per consecutive failure, up to this.
FEED_BACKOFF_MAX_HOURS = _float("NEWS_FEED_BACKOFF_MAX_HOURS", 6)
# Reddit / Hacker News (upvoterss) comments kept as part of the article body.
SOCIAL_MAX_COMMENTS = _int("NEWS_SOCIAL_MAX_COMMENTS", 10)
# Cross-feed duplicates (same canonical URL, or same title of at least N words,
# within the window) skip the AI pipeline and join the original's story.
DEDUP_WINDOW_HOURS = _int("NEWS_DEDUP_WINDOW_HOURS", 72)
DEDUP_TITLE_MIN_WORDS = _int("NEWS_DEDUP_TITLE_MIN_WORDS", 5)
# One feed serving the same body under this many titles = extracted site chrome; re-extracted.
# (Two titles is usually a retitled article.)
BOILERPLATE_MIN_TITLES = _int("NEWS_BOILERPLATE_MIN_TITLES", 3)

# --- Retention -------------------------------------------------------------
# Stories without an update for this many days are deleted with their articles;
# unassigned articles (pending/skipped/disabled feeds) are deleted after the same age.
# 0 disables retention.
RETENTION_DAYS = _float("NEWS_RETENTION_DAYS", 30)
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
# Embedding candidates may be older than the cluster window, so a duplicate isn't lost at 48h.
MERGE_CANDIDATE_WINDOW_HOURS = _int("NEWS_MERGE_CANDIDATE_WINDOW_HOURS", 96)
# Lexical channel: stories sharing this many headline terms that occur in at most
# MERGE_LEXICAL_MAX_DF window stories are verified too (catches cross-language copies).
MERGE_LEXICAL_MIN_TERMS = _int("NEWS_MERGE_LEXICAL_MIN_TERMS", 3)
MERGE_LEXICAL_MAX_DF = _int("NEWS_MERGE_LEXICAL_MAX_DF", 5)

# Split pass (app/split.py): a story with SPLIT_MIN_ARTICLES+ articles of which at least
# SPLIT_FAR_PAIR_SHARE of the pairs are unrelated (cosine < SPLIT_FAR_SIM) is regrouped by
# embedding. Calibrated 2026-10-21: coherent 30+ stories <= 4.5 %, catch-alls 10-36 %.
SPLIT_ENABLED = _env("NEWS_SPLIT_ENABLED", "1") == "1"
SPLIT_MIN_ARTICLES = _int("NEWS_SPLIT_MIN_ARTICLES", 30)
SPLIT_FAR_SIM = _float("NEWS_SPLIT_FAR_SIM", 0.4)
SPLIT_FAR_PAIR_SHARE = _float("NEWS_SPLIT_FAR_PAIR_SHARE", 0.10)
SPLIT_GROUP_SIM = _float("NEWS_SPLIT_GROUP_SIM", 0.6)  # article-to-group-centroid cosine
SPLIT_MIN_GROUP = _int("NEWS_SPLIT_MIN_GROUP", 3)  # smaller groups fold into the nearest one
SPLIT_PART_SIM = _float("NEWS_SPLIT_PART_SIM", 0.8)  # parts this close (centroid cosine) stay together
SPLIT_MAX_PARTS = _int("NEWS_SPLIT_MAX_PARTS", 6)
SPLIT_MAX_PER_RUN = _int("NEWS_SPLIT_MAX_PER_RUN", 1)  # each part costs a full Prompt 2 rewrite

# --- Story generation ------------------------------------------------------
# Cap for headline-only quality scores: a headline can't show factual density.
TITLE_QUALITY_MAX = _int("NEWS_TITLE_QUALITY_MAX", 6)
# A thin article joining a story at/below this quality skips harness + Prompt2.
JUNK_SKIP_QUALITY_THRESHOLD = _float("NEWS_JUNK_SKIP_QUALITY_THRESHOLD", 2.0)
# A joining article this similar to a member whose text has all of its words skips Harness + Prompt 2 (> 1 = off).
COPY_SKIP_SIM = _float("NEWS_COPY_SKIP_SIM", 0.9)
# Combined article text budget for full Prompt2 runs before older items are compressed.
CONTEXT_BUDGET_CHARS = _int("NEWS_CONTEXT_BUDGET_CHARS", 200_000)
# Update significance (0 none … 3 major, story.py) at which a read story shows up again as updated.
READ_RESURFACE_SIGNIFICANCE = _int("NEWS_READ_RESURFACE_SIGNIFICANCE", 2)
# Update significance from which Prompt 2 may rewrite the headline (below it the headline stays).
HEADLINE_REFRESH_SIGNIFICANCE = _int("NEWS_HEADLINE_REFRESH_SIGNIFICANCE", 2)
# Longer Prompt 2 summaries are sent back once for shortening, then cut at a sentence end.
SUMMARY_MAX_CHARS = _int("NEWS_SUMMARY_MAX_CHARS", 800)

# --- Scoring ---------------------------------------------------------------
# Stories without an update for this many days drop out of the ranked feed into
# the archive (until retention deletes them; a revived story comes back).
STORY_ACTIVE_DAYS = _float("NEWS_STORY_ACTIVE_DAYS", 7)
# --- Ranking v2 (app/scoring.py docstring). All features are 0..1; the weights are starting
# hypotheses to calibrate, not validated constants.
# public = Σ W_PUB_* · feature (weights sum to 1); score = 100 · freshness · soft_cap(value + update_boost)
W_PUB_QUALITY = _float("NEWS_W_PUB_QUALITY", 0.30)
W_PUB_COVERAGE = _float("NEWS_W_PUB_COVERAGE", 0.22)
W_PUB_URGENCY = _float("NEWS_W_PUB_URGENCY", 0.16)
W_PUB_IMPACT = _float("NEWS_W_PUB_IMPACT", 0.12)
W_PUB_VELOCITY = _float("NEWS_W_PUB_VELOCITY", 0.12)
W_PUB_STATUS = _float("NEWS_W_PUB_STATUS", 0.08)
IMPACT_SCORE = _weights("NEWS_IMPACT_SCORE", "Local=0.25,Regional=0.45,National=0.7,Global=1.0")
STATUS_SCORE = _weights("NEWS_STATUS_SCORE", "Breaking=1.0,Ongoing=0.6,Resolved=0.15")
# Freshness half-life (hours since the last news) per status; unknown status uses Ongoing.
FRESHNESS_HALF_LIFE_HOURS = _weights("NEWS_FRESHNESS_HALF_LIFE_HOURS", "Breaking=12,Ongoing=30,Resolved=12")
# Coverage = 1 - e^(-outlet_trust / k): the 2nd independent outlet adds more than the 6th.
COVERAGE_SATURATION = _float("NEWS_COVERAGE_SATURATION", 2.0)
RESOLVED_URGENCY_CAP = _float("NEWS_RESOLVED_URGENCY_CAP", 0.65)
VELOCITY_UPDATE_HALF_LIFE_HOURS = _float("NEWS_VELOCITY_UPDATE_HALF_LIFE_HOURS", 8)
VELOCITY_OUTLET_HALF_LIFE_HOURS = _float("NEWS_VELOCITY_OUTLET_HALF_LIFE_HOURS", 6)
UPDATE_BOOST = _float("NEWS_UPDATE_BOOST", 0.08)
# Per update significance (1 minor … 3 major): share of the story's age an update takes back
# (news_at moves that far towards the update's time), its weight in velocity, and in the boost.
FRESHNESS_REFRESH = _weights("NEWS_FRESHNESS_REFRESH", "1=0.1,2=0.5,3=1.0")
VELOCITY_SIG_WEIGHT = _weights("NEWS_VELOCITY_SIG_WEIGHT", "1=0.05,2=0.35,3=1.0")
BOOST_SIG_WEIGHT = _weights("NEWS_BOOST_SIG_WEIGHT", "1=0.1,2=0.45,3=1.0")
# An article's publication time counts from at most this long before it was first seen.
TRIGGER_MAX_LAG_HOURS = _float("NEWS_TRIGGER_MAX_LAG_HOURS", 24)
# "For you" score factor by read state: read, updated since read (significance 2 / 3).
READER_FACTOR = _weights("NEWS_READER_FACTOR", "read=0.2,updated=0.5,updated_major=0.85")
# Read marks / votes clicked offline are replayed at most this long after the click
# (keep in sync with PENDING_MAX_AGE_MS in frontend/app.js).
OFFLINE_MARK_MAX_HOURS = 24
# List-level MMR rerank of the first DIVERSITY_POOL stories: λ·score - (1-λ)·max similarity
# to a story placed above; cosines below DIVERSITY_SIM_FLOOR count as unrelated.
DIVERSITY_POOL = _int("NEWS_DIVERSITY_POOL", 50)
DIVERSITY_LAMBDA = _float("NEWS_DIVERSITY_LAMBDA", 0.82)
DIVERSITY_SIM_FLOOR = _float("NEWS_DIVERSITY_SIM_FLOOR", 0.5)
DIVERSITY_LINK_SIM = _weights("NEWS_DIVERSITY_LINK_SIM",
                              "CONTINUING_DEVELOPMENT=0.95,SAME_TOPIC_DISTINCT_EVENT=0.75")
# Personal: value = public + W_AFFINITY·tag_affinity (-1..1) + W_GEO·geo (0..1)
W_AFFINITY = _float("NEWS_W_AFFINITY", 0.30)
W_GEO = _float("NEWS_W_GEO", 0.08)
# tanh(weight / k): weight 1 ≈ 0.25, 3 ≈ 0.64, 10 ≈ 0.99
TAG_WEIGHT_SCALE = _float("NEWS_TAG_WEIGHT_SCALE", 4.0)
# A tag at or below this explicit weight hides its stories from "For you" (mute).
TAG_MUTE_WEIGHT = _float("NEWS_TAG_MUTE_WEIGHT", -10.0)
# 👍/👎 and story chats no longer edit the tag list; they form decaying inferred weights.
VOTE_WEIGHT = _float("NEWS_VOTE_WEIGHT", 2.0)
CHAT_WEIGHT = _float("NEWS_CHAT_WEIGHT", 1.0)
VOTE_HALF_LIFE_DAYS = _weights("NEWS_VOTE_HALF_LIFE_DAYS", "up=60,down=90,chat=90")
# Inferred weights of tags carried by this share of active stories or more count ~nothing.
INFERRED_MIN_SPECIFICITY = _float("NEWS_INFERRED_MIN_SPECIFICITY", 0.2)
# Tag synonym merging (app/tag_merge.py): tags on ≥ TAG_MERGE_MIN_STORIES stories (or on a tag
# list) are embedded; neighbours ≥ REVIEW_SIM go to the LLM verifier. A SAME_CONCEPT verdict
# merges automatically only at ≥ AUTO_SIM and ≥ AUTO_CONFIDENCE, else it waits for review.
# Thresholds are uncalibrated starting points: check `scripts/merge_tags.py list` first.
TAG_MERGE_ENABLED = _env("NEWS_TAG_MERGE_ENABLED", "1") == "1"
TAG_MERGE_INTERVAL_HOURS = _float("NEWS_TAG_MERGE_INTERVAL_HOURS", 24)
TAG_MERGE_MIN_STORIES = _int("NEWS_TAG_MERGE_MIN_STORIES", 3)
TAG_MERGE_REVIEW_SIM = _float("NEWS_TAG_MERGE_REVIEW_SIM", 0.80)
TAG_MERGE_AUTO_SIM = _float("NEWS_TAG_MERGE_AUTO_SIM", 0.90)
TAG_MERGE_AUTO_CONFIDENCE = _float("NEWS_TAG_MERGE_AUTO_CONFIDENCE", 0.9)
TAG_MERGE_MAX_VERIFY = _int("NEWS_TAG_MERGE_MAX_VERIFY", 30)  # LLM calls per run
# Source trust (0..1) is set on the Feeds page (FeedCategory, Feed.trust); this is the trust of a
# category without a value there. ⚙ Preferences overrides both per user.
SOURCE_TRUST_DEFAULT = 1.0
# Outlets of one organization count as one source for credibility ("host=group", hosts without www./feeds.).
OUTLET_GROUPS = {k.strip(): v.strip() for k, _, v in (p.partition("=") for p in _list(
    "NEWS_OUTLET_GROUPS",
    "deutschlandfunkkultur.de=deutschlandfunk.de,act.eff.org=eff.org,wid.cert-bund.de=bsi.bund.de")) if v.strip()}
USER_CITY = _env("NEWS_USER_CITY", "Munich")
USER_COUNTRY = _env("NEWS_USER_COUNTRY", "Germany")
USER_LANGUAGE = _env("NEWS_USER_LANGUAGE", "de")

# --- Heads-up brief + scheduled ntfy reports ---------------------------------
HEADS_UP_TTL_MINUTES = _int("NEWS_HEADS_UP_TTL_MINUTES", 11)
HEADS_UP_NTFY_URL = _env("NEWS_HEADS_UP_NTFY_URL", "https://ntfy.sh/your-topic")  # empty disables all pushes
HEADS_UP_NTFY_TITLE = _env("NEWS_HEADS_UP_NTFY_TITLE", "News Heads Up")
# Daily brief times (server-local HH:MM) until schedules are saved in ⚙ Preferences.
HEADS_UP_NTFY_TIMES = _list("NEWS_HEADS_UP_NTFY_TIMES", "07:00")

# --- Auth (startup fails unless this is set or an admin has a stored password) ---
AUTH_PASSWORD = _env("NEWS_AUTH_PASSWORD", "")
AUTH_SESSION_HOURS = _float("NEWS_AUTH_SESSION_HOURS", 7200)
AUTH_COOKIE = _env("NEWS_AUTH_COOKIE", "news_session")

# --- Per-story chat --------------------------------------------------------
CHAT_TEMPERATURE = _float("NEWS_CHAT_TEMPERATURE", 0.3)
# Thinking tokens count against this; 1024 often ran out before the answer started.
CHAT_MAX_TOKENS = _int("NEWS_CHAT_MAX_TOKENS", 4096)
# Safety cap on autonomous search+scrape turns per message when web search is enabled.
CHAT_SEARCH_MAX_TURNS = _int("NEWS_CHAT_SEARCH_MAX_TURNS", 8)
CHAT_SEARCH_MAX_SECONDS = _float("NEWS_CHAT_SEARCH_MAX_SECONDS", 120)
# Article lines in the chat system prompt (newest first); big stories had 300+.
CHAT_STORY_MAX_ARTICLES = _int("NEWS_CHAT_STORY_MAX_ARTICLES", 30)
CHAT_CONTEXT_PER_SOURCE = _int("NEWS_CHAT_CONTEXT_PER_SOURCE", 1500)
CHAT_CONTEXT_MAX = _int("NEWS_CHAT_CONTEXT_MAX", 6000)
# Answers run as background jobs (closing the tab doesn't stop them); at most this many per user.
CHAT_MAX_JOBS_PER_USER = _int("NEWS_CHAT_MAX_JOBS_PER_USER", 3)
# Search-result pages fetched at the same time (sequential reads took up to a minute).
CHAT_READ_PARALLEL = _int("NEWS_CHAT_READ_PARALLEL", 4)

# --- Story search (app/search.py) -------------------------------------------
# Word matches (FTS5) for everyone; signed-in users also get meaning matches (query
# embedding vs article vectors), merged by Reciprocal Rank Fusion.
SEARCH_RRF_K = _int("NEWS_SEARCH_RRF_K", 60)
# Word list weighs 1.0. At 0.5 a meaning-only story ranks below the first ~60 word matches:
# probe 2026-10-27 (qwen3-embedding 0.6B) put relevant and off-topic hits both at 0.45–0.57.
SEARCH_SEMANTIC_WEIGHT = _float("NEWS_SEARCH_SEMANTIC_WEIGHT", 0.5)
SEARCH_KNN = _int("NEWS_SEARCH_KNN", 400)  # nearest articles per query, before the scope filter
SEARCH_SEMANTIC_MAX_STORIES = _int("NEWS_SEARCH_SEMANTIC_MAX_STORIES", 30)
# Meaning-only matches need this cosine and must be within SEARCH_SIM_MARGIN of the best one.
# Probe 2026-10-27: unrelated 0.30–0.36, nonsense query max 0.455; tune with scripts/search_probe.py.
SEARCH_MIN_SIM = _float("NEWS_SEARCH_MIN_SIM", 0.5)
SEARCH_SIM_MARGIN = _float("NEWS_SEARCH_SIM_MARGIN", 0.15)
# Wait at most this long for the query embedding, then answer with word matches only.
SEARCH_SEMANTIC_TIMEOUT = _float("NEWS_SEARCH_SEMANTIC_TIMEOUT", 1.5)
SEARCH_EMBED_TIMEOUT = _float("NEWS_SEARCH_EMBED_TIMEOUT", 15)  # HTTP cap of a late embedding
SEARCH_EMBED_MAX_INFLIGHT = _int("NEWS_SEARCH_EMBED_MAX_INFLIGHT", 2)  # busy -> word matches only
SEARCH_EMBED_CACHE = _int("NEWS_SEARCH_EMBED_CACHE", 500)
# Fused score × (1 + boost · priority/100): newer/more important stories win near-ties.
SEARCH_PRIORITY_BOOST = _float("NEWS_SEARCH_PRIORITY_BOOST", 0.15)
# Qwen3-Embedding query instruction (documents are embedded without one); changing it
# changes the query vectors, so re-check SEARCH_MIN_SIM afterwards.
SEARCH_INSTRUCT = _env("NEWS_SEARCH_INSTRUCT",
                       "Given a news search query, retrieve news articles about the topic, event or names it mentions")

# --- searchngx (SearXNG web search for chat) ---------------------------------
SEARCHNGX_BASE_URL = _env("NEWS_SEARCHNGX_BASE_URL", "https://searx.example.org/")
SEARCHNGX_API_KEY = _env("NEWS_SEARCHNGX_API_KEY", "")
SEARCHNGX_ENDPOINT = _env("NEWS_SEARCHNGX_ENDPOINT", "search")
SEARCHNGX_CATEGORIES = _env("NEWS_SEARCHNGX_CATEGORIES", "general,news")
# Retried with these engines when the default ones return nothing (CAPTCHAs, rate limits).
SEARCHNGX_FALLBACK_ENGINES = _env("NEWS_SEARCHNGX_FALLBACK_ENGINES", "bing,bing news")
SEARCHNGX_TIMEOUT = _float("NEWS_SEARCHNGX_TIMEOUT", 30)
SEARCHNGX_MAX_RESULTS = _int("NEWS_SEARCHNGX_MAX_RESULTS", 5)
