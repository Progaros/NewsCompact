# AI News Aggregator

A local-first web application that ingests large volumes of RSS feeds, uses a local
Ollama LLM to cluster related articles into chronological **"stories"**, summarize them,
assess their quality, and rank them against a personalized relevance score.

Everything runs locally: one SQLite file (`news.db`, including the sqlite-vec
embedding table) and HTTP calls to a local Ollama instance. No external services.

**Role of this file:** entry point / usage documentation. For progress tracking see
[`plan.md`](./plan.md), for technical findings [`findings.md`](./findings.md), and for
the original design spec [`project_plan.md`](./project_plan.md).

## Project state (as of 2026-10-11)

- **Phases 1–5 complete** except final weight/threshold tuning. Phase 5 includes the
  feed management page (add/group/rename/delete feeds, enable-disable AI ingestion
  per feed with live 24h/7d volume stats per feed and per category).
- **Remaining open work:** W1/W2/W3 + relevance-weight tuning once real 👍/👎
  feedback data exists (`FeedbackLog` still empty — personalization terms
  untested in practice).
- **Database snapshot:** 3,351 articles, 1,011 processed articles, and 817
  stories. The server is currently stopped; start it with a newly supplied
  `NEWS_AUTH_PASSWORD` if authenticated access is required.
- **Clustering status:** two-stage routing is active by default: embeddings
  retrieve candidates at 0.55 and the local verifier accepts only exact-event
  matches. Set `NEWS_CLUSTER_MODE=cosine` for the former member-anchor path.
  The first 40-pair benchmark improved precision at 0.64 from 0.50 to 0.667,
  with recall moving from 0.636 to 0.545.
  The full corrected 200-pair run at 0.55 completed without timeout: verifier
  precision `0.826`, recall `0.691`, F1 `0.753` versus cosine precision
  `0.712`, recall `0.855`, F1 `0.777`.

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
   - **Clustering**: member-anchor KNN retrieves candidates from recent stories, then
     one schema-constrained verifier call classifies up to three candidates as
     `EXACT_MATCH`, `CONTINUING_DEVELOPMENT`, `SAME_TOPIC_DISTINCT_EVENT`, or
     `UNRELATED`. Only the first two attach to a story. A second tier (≥ 0.62, no
     freshness window) revives a stale story; `NEWS_CLUSTER_MODE=cosine` disables
     verification for rollback.
   - **Harness** (`app/harness.py`, Jev-style Choice/Score/Boolean over the chat model
     with Ollama `format` JSON-schema constraints): per-article quality (1–10, with
     calibration anchors; broken/thin bodies are scored from the headline) + breaking
     flag.
   - **Prompt 2**: story-level LLM call that generates/updates the headline, summary,
     tags, quality and metrics (urgency/impact/status). New stories use full context;
     multi-member stories use the current summary plus the new article capped at 1,500
     words. Tags are normalized (`app/tags.py`).
   - Story quality is the **median** of member article qualities (one bot-blocked
     fetch can't drag a story down). `app/content_quality.py` classifies bodies as
     `ok`/`thin`/`broken` so broken pages are scored from the headline instead.
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

- **Sign-in** — if `NEWS_AUTH_PASSWORD` is set, full access (feeds page,
  👍/👎, Refresh, profile) needs a login: the **Sign in** button in the top bar
  opens a small password popup; the session lasts 24 h (`NEWS_AUTH_SESSION_HOURS`)
  in an HttpOnly cookie. Without signing in the main page stays fully readable
  and live (read-only: no Feeds link, no feedback buttons). A not-signed-in
  viewer who hits a protected endpoint sees a toast with a **Sign in** button
  that disappears after a few seconds.

- **Quick Heads Up** — AI paragraph summarizing the top 5 stories by display score.
  Cached server-side and regenerated at most once per TTL (`NEWS_HEADS_UP_TTL_MINUTES`,
  default 3 min); the page fetches it on open only. At each scheduled time
  (`NEWS_HEADS_UP_NTFY_TIMES`, default `07:00`) the brief is also pushed to the phone
  over ntfy (`NEWS_HEADS_UP_NTFY_URL`, default `https://ntfy.sh/your-topic`).
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
| `GET /api/heads-up`    | AI brief over the top 5 stories (TTL-cached)       |
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
| `GET /api/auth/session`| `{authenticated, auth_enabled}` (boot probe)       |
| `POST /api/auth/login` | `{"password": "…"}` → sets the session cookie       |
| `POST /api/auth/logout`| Clears the session cookie                          |

**Auth:** when `NEWS_AUTH_PASSWORD` is set, every endpoint **except** the
read-only set — `GET /api/stories`, `GET /api/stories/{id}`, `GET /api/heads-up`,
`GET /api/health`, `GET /api/events`, `/`, `/static/*` and the two auth probes
above — returns **401** without a session (the `/feeds` page redirects to `/`
for browser navigation). With the password unset, auth is off and everything
is open.

## Configuration

All via environment variables (see `app/config.py` for defaults):

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama host |
| `NEWS_CHAT_MODEL` | `qwen3:27b` | chat model |
| `NEWS_EMBED_MODEL` | `qwen3-embedding:0.6b-CPU` | embedding model (1024-dim) |
| `NEWS_CLUSTER_SIM_THRESHOLD` | `0.64` | join-existing-story member-anchor similarity |
| `NEWS_CLUSTER_MODE` | `two_stage` | `two_stage` verifies candidates with the local LLM; `cosine` is rollback mode |
| `NEWS_CLUSTER_VERIFY_RETRIEVE_THRESHOLD` | `0.55` | minimum candidate similarity before verification |
| `NEWS_CLUSTER_VERIFY_MAX_CANDIDATES` | `3` | maximum verifier calls per article |
| `NEWS_CLUSTER_VERIFY_TIMEOUT_SECONDS` | `60` | deadline for one batched verifier request |
| `NEWS_CLUSTER_REVIVE_SIM_THRESHOLD` | `0.62` | revive a stale story (no time window) |
| `NEWS_PIPELINE_MAX_FAILURES` | `3` | failures before an article enters dead-letter state |
| `NEWS_CLUSTER_WINDOW_HOURS` | `48` | clustering freshness window |
| `NEWS_WORKER_INTERVAL` | `900` (s) | worker cycle period |
| `NEWS_MAX_ITEMS_PER_FEED` | `10` | items per feed per cycle |
| `NEWS_W1` / `NEWS_W3` | `1.0` / `1.5` | display-score urgency / user-interest weights |
| `NEWS_W_REL_QUALITY` / `NEWS_W_REL_CREDIBILITY` / `NEWS_W_REL_IMPACT` | `0.40` / `0.35` / `0.25` | relevance base weights (sum 1) |
| `NEWS_DECAY_HALF_LIFE_HOURS` / `NEWS_DECAY_MAX` | `18` / `10.0` | time decay |
| `NEWS_FEEDBACK_ALPHA` | `0.1` | feedback learning rate |
| `NEWS_PORT` / `NEWS_HOST` | `8787` / `0.0.0.0` | server bind |
| `NEWS_DB_PATH` | `news.db` | database file |
| `NEWS_HEADS_UP_TTL_MINUTES` | `3` | heads-up brief cache TTL (min); page fetches it on open only |
| `NEWS_HEADS_UP_NTFY_URL` | `https://ntfy.sh/your-topic` | phone push target (ntfy) |
| `NEWS_HEADS_UP_NTFY_TIMES` | `07:00` | comma-separated `HH:MM` (server-local) for the scheduled push; empty disables |
| `NEWS_AUTH_PASSWORD` | _(empty = off)_ | app login password; empty disables auth entirely |
| `NEWS_AUTH_SESSION_HOURS` | `24` | session lifetime (h); in-memory, restart = re-login |
| `NEWS_AUTH_COOKIE` | `news_session` | session cookie name (HttpOnly, SameSite=Lax) |

## Database

Single file `news.db` (WAL mode, foreign keys on, sqlite-vec loaded per connection):

- `Feed` — feeds from OPML (title, xml_url, category, ETag/Last-Modified cache,
  `enabled` = AI-ingestion toggle, see Feed management)
- `Article` — one row per RSS item; `content_raw` (may be a broken page — see
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
- Clustering benchmark: export review pairs with
  `PYTHONPATH=. uv run python scripts/benchmark_clustering.py --export /tmp/clustering-pairs.json`.
  Set each exported `label` to `1` for the same specific event or `0` for a
  different event, then run it with `--labels`; add `--llm` only when Ollama is
  reachable and the verifier comparison is desired.
- Expand the seed from the database with
  `PYTHONPATH=. uv run python scripts/build_golden_dataset.py --input benchmark_pairs.json --output benchmark_pairs_200.json --target 200`.
  Mined classifications are candidates and must be human-reviewed before
  being treated as ground truth.
- Export the 160 mined candidates for external review with
  `PYTHONPATH=. uv run python scripts/export_for_review.py`.
  This writes `review_candidates.md` with article and story snippets capped at
  50 words each.
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
    content_quality.py # ok/thin/broken/empty body classifier
    tags.py          # tag normalization (aliases, case/punctuation)
    scoring.py       # relevance/display score + feedback loop
    worker.py        # background scheduler
    bus.py           # SSE pub/sub
    api.py           # FastAPI routes
    frontend/        # index.html, app.js, feeds.html, feeds.js, styles.css
  scripts/           # one-off & monitoring scripts
```
