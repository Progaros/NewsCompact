"""Application configuration, driven by environment variables.

Everything has a sensible local-first default so the app runs out of the box.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("NEWS_DB_PATH", BASE_DIR / "news.db"))
OPML_PATH = Path(os.getenv("NEWS_OPML_PATH", BASE_DIR / "subscriptions-2026-09-27.opml"))

# --- Ollama ----------------------------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
CHAT_MODEL = os.getenv("NEWS_CHAT_MODEL", "qwen3:27b")
EMBED_MODEL = os.getenv("NEWS_EMBED_MODEL", "qwen3-embedding:0.6b-CPU")

# --- Pipeline --------------------------------------------------------------
EMBED_DIM = int(os.getenv("NEWS_EMBED_DIM", "1024"))
# Number of words of article text sent to the embedder (keeps CPU embed fast).
EMBED_MAX_WORDS = int(os.getenv("NEWS_EMBED_MAX_WORDS", "1000"))
# A pipeline article that fails Prompt2 N consecutive cycles (dead-letter cap)
# stops being re-picked — a permanently-broken article no longer burns a
# harness + Prompt2 call every 15 min. The count lives on Article.pipeline_failures
# and resets to 0 on the next successful process. See /api/health (dead_letter).
PIPELINE_MAX_FAILURES = int(os.getenv("NEWS_PIPELINE_MAX_FAILURES", "3"))
# KNN window for clustering: only cluster against stories updated within N hours.
CLUSTER_WINDOW_HOURS = int(os.getenv("NEWS_CLUSTER_WINDOW_HOURS", "48"))
# Cosine *similarity* threshold (article vector vs the story's *anchor*
# members: max sim to any non-broken member) above which a new article joins
# an existing story. sqlite-vec reports cosine *distance*; similarity =
# 1 - distance. 2026-10-11: switched from centroid mode to member-anchor
# mode (app/pipeline.py) — a mean centroid sits in the middle of scattered
# vectors and pulled generic blurbs into story 7 (23-article mush). 0.64
# calibrated against member-anchor sims: known-good multi-feed clusters
# (Russian-diesel story) sit at 0.67-0.86 and are preserved, story-7 mush
# members drop at 0.45-0.63. See findings.md + scripts/shrink_member_gate.py.
CLUSTER_SIM_THRESHOLD = float(os.getenv("NEWS_CLUSTER_SIM_THRESHOLD", "0.64"))

# Two-stage clustering mode (2026-10-11, todo §7): cosine alone conflates
# "same topic" with "same event", so over the static 0.64 threshold it either
# over-merges generic news or fragments related reporting. In "two_stage" mode
# decide_story (a) retrieves candidates with a LOOSER threshold (high recall)
# and (b) asks the local LLM a boolean "same specific event?" on the top few
# (high precision). "cosine" reproduces the pre-existing member-anchor behaviour.
# See scripts/benchmark_clustering.py for the A/B benchmark. Set this to
# "cosine" to roll back to member-anchor-only routing if Ollama is unavailable.
CLUSTER_MODE = os.getenv("NEWS_CLUSTER_MODE", "two_stage")  # "cosine" | "two_stage"
# Retrieve threshold for stage 1 (lower than CLUSTER_SIM_THRESHOLD on purpose:
# widen the candidate pool so the true match is never missed; the LLM prunes).
CLUSTER_VERIFY_RETRIEVE_THRESHOLD = float(
    os.getenv("NEWS_CLUSTER_VERIFY_RETRIEVE_THRESHOLD", "0.55")
)
# How many stage-1 candidates to LLM-verify (best-first) before giving up on a
# join and creating a new story. Bounds the extra latency to a few short calls.
CLUSTER_VERIFY_MAX_CANDIDATES = int(os.getenv("NEWS_CLUSTER_VERIFY_MAX_CANDIDATES", "3"))
# A verifier call must fail fast enough that one slow comparison cannot stall
# the serial worker indefinitely. The normal chat client remains at 120 s.
CLUSTER_VERIFY_TIMEOUT_SECONDS = float(
    os.getenv("NEWS_CLUSTER_VERIFY_TIMEOUT_SECONDS", "60")
)
# Anchor-sim threshold for reviving a *stale* story (outside the freshness
# window). Slightly looser than the fresh-join threshold on purpose: when
# nothing matches a recent story, linking to the old one beats creating a
# duplicate clone. Same member-anchor units as CLUSTER_SIM_THRESHOLD.
CLUSTER_REVIVE_SIM_THRESHOLD = float(
    os.getenv("NEWS_CLUSTER_REVIVE_SIM_THRESHOLD", "0.62")
)

# --- Ingestion -------------------------------------------------------------
FETCH_CONCURRENCY = int(os.getenv("NEWS_FETCH_CONCURRENCY", "8"))
FETCH_TIMEOUT = float(os.getenv("NEWS_FETCH_TIMEOUT", "20"))
USER_AGENT = os.getenv(
    "NEWS_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 NewsAggregator/1.0",
)
# Only re-extract full text if the RSS description is shorter than this (words).
EXTRACT_MIN_DESC_WORDS = int(os.getenv("NEWS_EXTRACT_MIN_DESC_WORDS", "100"))
# How often the background worker runs a full ingestion cycle (seconds).
WORKER_INTERVAL_SECONDS = int(os.getenv("NEWS_WORKER_INTERVAL", "900"))
# Cap on feeds per run (0 = all). Handy for local testing.
MAX_FEEDS_PER_RUN = int(os.getenv("NEWS_MAX_FEEDS_PER_RUN", "0"))
# Cap on items ingested per feed per cycle.
MAX_ITEMS_PER_FEED = int(os.getenv("NEWS_MAX_ITEMS_PER_FEED", "10"))

# --- Scoring ---------------------------------------------------------------
W1_URGENCY = float(os.getenv("NEWS_W1", "1.0"))
W2_QUALITY = float(os.getenv("NEWS_W2", "1.0"))  # legacy; superseded by the relevance model
W3_INTEREST = float(os.getenv("NEWS_W3", "1.5"))
# Relevance base (the personalization core): each component is 0-10, weighted
# by these normalized weights (sum to 1) so the base stays on a 0-10 scale.
#   quality      = harness quality
#   credibility  = corroboration (distinct sources backing the story)
#   user_impact  = how much it affects the user (location/regions, see
#                  UserProfile.preferences) — neutral 0.5 baseline when unset.
# On top: + urgency*W1 + user_interest*W3 - time_decay. See scoring.py.
W_RELEVANCE_QUALITY = float(os.getenv("NEWS_W_REL_QUALITY", "0.40"))
W_RELEVANCE_CREDIBILITY = float(os.getenv("NEWS_W_REL_CREDIBILITY", "0.35"))
W_RELEVANCE_IMPACT = float(os.getenv("NEWS_W_REL_IMPACT", "0.25"))
# Half-life (hours) of the exponential time-decay penalty.
TIME_DECAY_HALF_LIFE_HOURS = float(os.getenv("NEWS_DECAY_HALF_LIFE_HOURS", "18"))
TIME_DECAY_MAX = float(os.getenv("NEWS_DECAY_MAX", "10.0"))
# Learning rate for the feedback loop (small so one click can't dominate).
FEEDBACK_ALPHA = float(os.getenv("NEWS_FEEDBACK_ALPHA", "0.1"))
# Display-point cap on the user_interest term. tag_weights are not decayed,
# so raw sums accumulate permanently (5 upvotes x 6 tags x W3 = +45 would
# outrank max urgency W1*10 and max decay -10 for days). Clamping the
# contribution to +/- INTEREST_CAP keeps personalization in the same band as
# urgency and decay; raw weights stay intact for later re-derivation.
INTEREST_CAP = float(os.getenv("NEWS_INTEREST_CAP", "10.0"))

# --- Heads-up brief ------------------------------------------------------
# The AI "Quick Heads Up" paragraph is regenerated at most once per TTL
# (minutes). The frontend only fetches it on page open, so while a page is
# open the brief refreshes at most once per TTL; otherwise the stored text is
# returned cached as-is ("if not older than set value, show old stored summary").
HEADS_UP_TTL_MINUTES = int(os.getenv("NEWS_HEADS_UP_TTL_MINUTES", "3"))
# Scheduled push: at each configured time (24h "HH:MM", server local time) the
# brief is regenerated and pushed to the phone over ntfy. Each time fires once
# per calendar day (within a short catch-up window) and does not fire for a
# time that passed while the app was down. Empty list/URL disables the push.
HEADS_UP_NTFY_URL = os.getenv("NEWS_HEADS_UP_NTFY_URL", "https://ntfy.sh/your-topic")
HEADS_UP_NTFY_TITLE = os.getenv("NEWS_HEADS_UP_NTFY_TITLE", "News Heads Up")
HEADS_UP_NTFY_TIMES = [
    t.strip() for t in os.getenv("NEWS_HEADS_UP_NTFY_TIMES", "07:00").split(",") if t.strip()
]

# --- Auth ------------------------------------------------------------------
# Single-user login. When AUTH_PASSWORD is non-empty, the write/management
# endpoints require a logged-in session (see app/auth.py); the main feed view
# stays public so the first page is readable without signing in. When it is
# empty, auth is disabled (open mode) and every check passes.
AUTH_PASSWORD = os.getenv("NEWS_AUTH_PASSWORD", "")
# Session lifetime (hours). Sessions are in-memory (a server restart requires
# re-login) and carried by an HttpOnly SameSite=Lax cookie.
AUTH_SESSION_HOURS = float(os.getenv("NEWS_AUTH_SESSION_HOURS", "24"))
AUTH_COOKIE = os.getenv("NEWS_AUTH_COOKIE", "news_session")

# --- API -------------------------------------------------------------------
PUBLIC_BASE = os.getenv("NEWS_PUBLIC_BASE", "http://127.0.0.1:8787")
HOST = os.getenv("NEWS_HOST", "0.0.0.0")
PORT = int(os.getenv("NEWS_PORT", "8787"))
