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
# KNN window for clustering: only cluster against stories updated within N hours.
CLUSTER_WINDOW_HOURS = int(os.getenv("NEWS_CLUSTER_WINDOW_HOURS", "48"))
# Cosine *similarity* threshold (article vector vs story *centroid*) above
# which a new article joins an existing story. sqlite-vec reports cosine
# *distance*; similarity = 1 - distance. Calibrated 2026-10-10 against the
# junk-free centroid: legit members min ~0.57, p10 ~0.63 (see findings.md);
# 0.50 over-merged junk title-only embeddings, 0.55 keeps legit articles
# while rejecting most boilerplate. See scripts/shrink_diluted_stories.py.
CLUSTER_SIM_THRESHOLD = float(os.getenv("NEWS_CLUSTER_SIM_THRESHOLD", "0.55"))
# Stricter threshold for reviving a *stale* story (outside the freshness
# window). Only a strong match re-opens an old story so a year-old topic
# that resurfaces links to the existing context instead of spawning a clone.
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

# --- API -------------------------------------------------------------------
PUBLIC_BASE = os.getenv("NEWS_PUBLIC_BASE", "http://127.0.0.1:8787")
HOST = os.getenv("NEWS_HOST", "0.0.0.0")
PORT = int(os.getenv("NEWS_PORT", "8787"))
