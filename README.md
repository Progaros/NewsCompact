# AI News Aggregator

A local-first web application that ingests large volumes of RSS feeds, uses a local
Ollama LLM to cluster related articles into chronological **"stories"**, summarize them,
assess their quality, and rank them against a personalized relevance score.

Everything runs locally: one SQLite file (`news.db`, including the sqlite-vec
embedding table) and HTTP calls to a local Ollama instance. No external services.

**Role of this file:** entry point / usage documentation. For progress tracking see
[`plan.md`](./plan.md), for technical findings [`findings.md`](./findings.md), and for
the original design spec [`project_plan.md`](./project_plan.md).

## Project state (as of 2026-10-10, late evening)

- **Phases 1–5 complete** except final weight/threshold tuning. Phase 5 includes the
  feed management page (add/group/rename/delete feeds, enable-disable AI ingestion
  per feed with live 24h/7d volume stats per feed and per category).
- **Remaining open work:** W1/W2/W3 + relevance-weight tuning once real 👍/👎
  feedback data exists (`FeedbackLog` still empty — personalization terms
  untested in practice).
- **Live at the time of writing:** 240 feeds, ~2500 articles, ~233 stories; the
  background worker is actively processing the clustering backlog
  (~2200 unclustered articles, ~50/cycle). One-off quality rescore finished.

## Quickstart

Prerequisites:

- [uv](https://docs.astral.sh/uv/) (manages Python 3.14 and dependencies)
- A reachable Ollama instance with:
  - chat model `qwen3:27b` (GPU-resident)
  - embedding model `qwen3-embedding:0.6b-CPU` (1024-dim vectors)
  - default host: `http://127.0.0.1:11434` (override with `OLLAMA_BASE_URL`)

```bash
cd news_aggregator_ai
uv sync                  # install dependencies
uv run python main.py    # → http://127.0.0.1:8787
```

On startup the app:

1. initializes `news.db` (schema + sqlite-vec virtual table; idempotent),
2. imports/updates feeds from `subscriptions-2026-09-27.opml` (upsert by `xml_url`),
3. starts the background worker (ingest + AI pipeline every `NEWS_WORKER_INTERVAL`=900 s).

The first full ingest of a bulk pull takes hours (see load figures in
[`findings.md`](./findings.md) → "Load & utilisation"). Subsequent cycles only process
new articles (~30/hour arrival measured).

## How it works

Per worker cycle (`app/worker.py`):

1. **Ingestion** (`app/ingest.py`): concurrent RSS fetch (`feedparser` + `httpx`,
   browser User-Agent, ETag/Last-Modified). Full-text extraction fallback chain when
   the RSS description is short: **trafilatura → newspaper3k → BeautifulSoup**.
   Articles are deduped by URL.
2. **Pipeline** (`app/pipeline.py`), serial per article:
   - **Embed** (title-first text: title + first 100 body words) → `qwen3-embedding:0.6b-CPU`
     → `Article_Embeddings` (sqlite-vec).
   - **Clustering**: KNN over recent stories (48 h window). A new article joins a story
     only if its cosine similarity to the story's *centroid* ≥ 0.50 (star mode; avoids
     junk chains of chain mode). A second, stricter tier (≥ 0.62, no window) *revives*
     a stale story, so a year-later update links back to the old context.
   - **Harness** (`app/harness.py`, Jev-style Choice/Score/Boolean over the chat model
     with Ollama `format` JSON-schema constraints): per-article quality (1–10, with
     calibration anchors; junk/thin bodies are scored from the headline) + breaking
     flag.
   - **Prompt 2**: story-level LLM call that generates/updates the headline, summary,
     tags, quality and metrics (urgency/impact/status). Older articles are compressed
     to bullets past a character budget. Tags are normalized (`app/tags.py`).
   - Story quality is the **median** of member article qualities (one bot-blocked
     fetch can't drag a story down). `app/content_quality.py` classifies bodies as
     `ok`/`thin`/`junk` so junk pages are scored from the headline instead.
   - Breaking stories (urgency ≥ 8) are pushed to the UI via SSE (`app/bus.py`).
3. **Scoring** (`app/scoring.py`):

   ```
   relevance base (0–10) = quality·0.40 + credibility·0.35 + user_impact·0.25
   display_score         = relevance base + urgency·W1 + user_interest·W3 − time_decay
   ```

   - `credibility` = corroboration: distinct sources backing the story (1 → low, 5+ → max).
   - `user_impact` = location/region data from `UserProfile.preferences`
     (`PUT /api/profile`), neutral 0.5 baseline when unset.
   - `user_interest` = sum of the user's tag weights for the story's tags.
   - `time_decay` = exponential ramp (half-life 18 h, max penalty 10) so stale stories sink.
   - 👍/👎 feedback: raw votes are logged in `FeedbackLog` **separately** from the
     profile, then tag weights are nudged by a small learning rate (0.1 × 10 per click,
     clamped ±10) — one click can't dominate the feed.

## Using the UI

Open <http://127.0.0.1:8787>:

- **Quick Heads Up** — AI paragraph summarizing the top 5 stories by display score
  (recomputed per request).
- **Story feed** — cards sorted by display score, each showing: AI headline, summary,
  quality badge (color-coded), **relevance breakdown** (base + q/cred/impact/int
  components, hover for formula), breaking pill, **`N sources` badge** (so you don't
  have to expand the card to see corroboration), tag pills, and 👍/👎 buttons.
- Clicking a card expands an accordion with the per-source article timeline and links.
- **Load more** paginates in pages of 50; the `showing X/Y` status uses the server-side
  total. The UI refreshes live via SSE.
- **Feeds** in the top bar opens the feed management page (`/feeds`): add feeds, rename,
  regroup (category edit per row), delete, and toggle AI ingestion per feed — or
  **turn an entire category on/off with one button** (each category header has a
  "turn all on/off" toggle; category 24h/7d totals count **enabled feeds only**).
  The add-feed form takes the **URL first**; the title is auto-fetched server-side
  from the feed's `<title>` when left empty, and the category field offers existing
  categories as a dropdown while still accepting a free-form new one. Each feed keeps
  its own live 24h/7d stats — disabled feeds keep fetching RSS rows, so their volume
  stays visible even while they skip the AI pipeline. This is the tool for spotting
  too-chatty feeds/categories.
- **Refresh** in the top bar runs a full worker cycle inline (blocks while it runs).
- The status bar in the top right shows `showing X/Y stories`; health figures are at
  `GET /api/health`.

## API

| Method & path          | Purpose                                            |
| ---------------------- | -------------------------------------------------- |
| `GET /api/stories`     | Ranked stories (`limit` slice + total `count`)    |
| `GET /api/stories/{id}`| One story incl. its articles                       |
| `GET /api/heads-up`    | AI brief over the top 5 stories                    |
| `POST /api/feedback`   | `{"story_id": N, "kind": "up" \| "down"}`          |
| `GET /api/feedback`    | Raw 👍/👎 vote log (separate from the profile)     |
| `GET /api/feeds`       | Feeds + 24h/7d volume stats, per feed and per category (category sums = enabled feeds only) |
| `POST /api/feeds`      | Add a feed (`title`, `xml_url`, optional `category`)  |
| `POST /api/feeds/category` | Enable/disable every feed in a category at once (`{"category", "enabled"}`) |
| `GET /api/feeds/preview?url=` | Fetch a feed URL server-side and return its `<title>` (auto-title for the add form) |
| `PATCH /api/feeds/{id}`| Update `enabled` (1/0), `title`, `category`, `xml_url` |
| `DELETE /api/feeds/{id}`| Delete feed (+ its articles and embeddings)          |
| `GET /api/profile`     | UserProfile (tag weights, source weights, prefs)  |
| `PUT /api/profile`     | Merge user data (e.g. `interest_regions`, `locations`) into preferences — feeds the `user_impact` term |
| `POST /api/refresh`    | Run one full worker cycle inline                   |
| `GET /api/health`      | Counts (feeds/articles/stories)                    |
| `GET /api/events`      | SSE stream (cycle done, breaking story, …)         |

## Configuration

All via environment variables (see `app/config.py` for defaults):

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama host |
| `NEWS_CHAT_MODEL` | `qwen3:27b` | chat model |
| `NEWS_EMBED_MODEL` | `qwen3-embedding:0.6b-CPU` | embedding model (1024-dim) |
| `NEWS_CLUSTER_SIM_THRESHOLD` | `0.50` | join-existing-story centroid similarity |
| `NEWS_CLUSTER_REVIVE_SIM_THRESHOLD` | `0.62` | revive a stale story (no time window) |
| `NEWS_CLUSTER_WINDOW_HOURS` | `48` | clustering freshness window |
| `NEWS_WORKER_INTERVAL` | `900` (s) | worker cycle period |
| `NEWS_MAX_ITEMS_PER_FEED` | `10` | items per feed per cycle |
| `NEWS_W1` / `NEWS_W3` | `1.0` / `1.5` | display-score urgency / user-interest weights |
| `NEWS_W_REL_QUALITY` / `NEWS_W_REL_CREDIBILITY` / `NEWS_W_REL_IMPACT` | `0.40` / `0.35` / `0.25` | relevance base weights (sum 1) |
| `NEWS_DECAY_HALF_LIFE_HOURS` / `NEWS_DECAY_MAX` | `18` / `10.0` | time decay |
| `NEWS_FEEDBACK_ALPHA` | `0.1` | feedback learning rate |
| `NEWS_PORT` / `NEWS_HOST` | `8787` / `0.0.0.0` | server bind |
| `NEWS_DB_PATH` | `news.db` | database file |

## Database

Single file `news.db` (WAL mode, foreign keys on, sqlite-vec loaded per connection):

- `Feed` — feeds from OPML (title, xml_url, category, ETag/Last-Modified cache,
  `enabled` = AI-ingestion toggle, see Feed management)
- `Article` — one row per RSS item; `content_raw` (may be a junk page — see
  `content_class`), `quality` (per-article), `story_id` (NULL = pending clustering)
- `Story` — AI headline/summary/tags/metrics, `quality_score` = median of member
  article qualities
- `Article_Embeddings` — sqlite-vec virtual table (`FLOAT[1024]`, title-first vectors)
- `UserProfile` — single local user: `tag_weights`, `source_weights`, `preferences`
- `FeedbackLog` — raw 👍/👎 votes (kept separate from the profile for later tuning)

## Ops & helper scripts

- Run: `cd news_aggregator_ai && uv run python main.py`. The old server ignores SIGTERM
  (uvicorn waits for in-flight Ollama calls) — `kill -9` the `.venv/bin/python` child if
  you must restart.
- One-shot DB stats: `PYTHONPATH=. uv run python scripts/dbstats.py`
- Live reprocessing monitor (15-min snapshots):
  `setsid nohup env PYTHONPATH=. uv run python scripts/monitor.py > monitor.out 2>&1 < /dev/null &`
- Other one-off/maintenance scripts live in `scripts/` (recluster, rescore, cluster
  experiment/check, score check, perf stats, tag normalization, …). Each is documented
  in the `plan.md` changelog entry that created it.
- Server log: `server.log` (pipeline/harness INFO lines, incl. per-article perf).

## Layout

```
news_aggregator_ai/
  main.py            # entrypoint (uvicorn)
  README.md          # this file — usage
  plan.md            # progress tracker / changelog (living)
  findings.md        # technical findings (living, dated sections)
  project_plan.md    # original design spec
  news.db            # SQLite (+vec0), gitignored
  app/
    config.py        # env-driven settings
    db.py            # connections + schema bootstrap (sqlite-vec)
    schema.sql       # DDL
    opml.py          # OPML → Feed rows (auto on startup)
    ingest.py        # RSS fetch + extraction fallback chain
    pipeline.py      # embed → cluster → harness → Prompt2 → story update
    harness.py       # Jev-style Choice/Score/Boolean (schema-constrained)
    ollama.py        # Ollama HTTP client (chat /v1 + embeddings)
    content_quality.py # ok/thin/junk body classifier
    tags.py          # tag normalization (aliases, case/punctuation)
    scoring.py       # relevance/display score + feedback loop
    worker.py        # background scheduler
    bus.py           # SSE pub/sub
    api.py           # FastAPI routes
    frontend/        # index.html, app.js, feeds.html, feeds.js, styles.css
  scripts/           # one-off & monitoring scripts
```
