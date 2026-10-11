# AI News Aggregator

A local-first web application that ingests large volumes of RSS feeds, uses a local
Ollama LLM to cluster related articles into chronological **"stories"**, summarize them,
assess their quality, and rank them against a personalized relevance score.

Everything runs locally: one SQLite file (`news.db`, including the sqlite-vec
embedding table) and HTTP calls to a local Ollama instance. No external services.

**Role of this file:** entry point / usage documentation. For progress tracking see
[`plan.md`](./plan.md), for technical findings [`findings.md`](./findings.md), and for
the original design spec [`project_plan.md`](./project_plan.md).

## Project state (as of 2026-10-12)

- **Phases 1–5 complete** except final weight/threshold tuning. Phase 5 includes the
  feed management page (add/group/rename/delete feeds, enable-disable AI ingestion
  per feed with live 24h/7d volume stats per feed and per category).
- **New since 2026-10-11:** per-story chat (💬, signed-in, optional searchngx
  web-search turns), the `/list` flat article page with per-stage pipeline timings,
  distance sorting (`NEWS_W_DISTANCE` + `NEWS_USER_*`), worker interval default
  lowered 900 → 300 s.
- **Remaining open work:** W1/W2/W3 + relevance-weight tuning once real 👍/👎
  feedback data exists (`FeedbackLog` still empty — personalization terms
  untested in practice).
- **Database snapshot (2026-10-11, evening):** ~4,043 articles, ~1,133
  stories, 367 pending (mostly disabled-feed backlog by design), 1
  dead_letter (article 3546, transient failure — parked). The server is
  **running in open mode** (no `NEWS_AUTH_PASSWORD`) on port 8787; the
  enabled-feed backlog has drained to steady-state arrivals and a per-cycle
  cleanup reaps zero-article ("ghost") stories, so the story count is
  self-healing.
- **Clustering status:** `two_stage` is active: member-anchor retrieval at
  **0.55**, then the local zero-reasoning verifier (only EXACT_MATCH /
  CONTINUING_DEVELOPMENT attach). `NEWS_CLUSTER_MODE=cosine` is the rollback
  path (member-anchor join at 0.64, no verifier). Locked 2026-10-11: final
  200-pair benchmark two_stage P/R/F1 `0.804/0.673/0.733`, F0.5 `0.774`
  versus cosine @0.64 `0.750/0.436/0.552`, F0.5 `0.656`. The F0.5 metric
  weights precision, which matters for news (a wrong merge corrupts the
  summary; a missed merge just leaves two clean stories).
  (findings.md → "Assessment response" + "Final mode decision")

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
2. starts the background worker (ingest + AI pipeline every `NEWS_WORKER_INTERVAL`=300 s).

Feeds are managed through the web UI (`/feeds` → `POST`/`PATCH`/`DELETE /api/feeds`), not imported from a file.

The first full ingest of a bulk pull takes hours (see load figures in
[`findings.md`](./findings.md) → "Load & utilisation"). Subsequent cycles only process
new articles (~30/hour arrival measured).

## How it works

Per worker cycle (`app/worker.py`), a two-step self-heal runs first
(`repair_stale_placeholder_stories()` re-pends articles of NULL-headline
placeholder stories; `cleanup_orphaned_stories()` deletes stories that lost
all their articles), then:

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
     tags, quality and metrics (urgency/impact/status), plus `location` ("City,
     Country" or "Global") and `language` for distance sorting. New stories use
     full context; multi-member stories use the current summary plus the new
     article capped at 1,500 words. Tags are normalized (`app/tags.py`).
   - Story quality is the **median** of member article qualities (one bot-blocked
     fetch can't drag a story down). `app/content_quality.py` classifies bodies as
     `ok`/`thin`/`broken` so broken pages are scored from the headline instead.
   - Breaking stories (urgency ≥ 8) are pushed to the UI via SSE (`app/bus.py`).
3. **Scoring** (`app/scoring.py`):

   ```
   relevance base (0–10) = quality·0.40 + credibility·0.35 + user_impact·0.25
   display_score         = relevance base + urgency·W1 + user_interest·W3
                            + distance·W_DISTANCE − time_decay
   ```

   - `distance` (0–1) = physical + cultural proximity of the story to the user
     (`NEWS_USER_CITY`/`_COUNTRY`/`_LANGUAGE`): city match → 1.0, country → 0.6,
     same language → 0.35, unknown → 0 (neutral). The location/language come
     from the Prompt 2 call (stored in the story's metrics), not a separate
     classifier, so existing stories stay neutral at 0.

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

- **Sign-in** — if `NEWS_AUTH_PASSWORD` is set, full access (feeds page, `/list`,
  👍/👎, chat, profile) needs a login: the **Sign in** button in the top bar
  opens a small password popup; the session lasts 24 h (`NEWS_AUTH_SESSION_HOURS`)
  in an HttpOnly cookie. Without signing in the main page stays fully readable
  and live (read-only: no Feeds link, no feedback buttons). A not-signed-in
  viewer who hits a protected endpoint sees a toast with a **Sign in** button
  that disappears after a few seconds. When signed in, the top bar shows a
  **User** button instead — it opens a read-only profile view (preferences +
  tag weights) with a sign-out action.

- **Quick Heads Up** — AI paragraph summarizing the top 5 stories by display score.
  Cached server-side and regenerated at most once per TTL (`NEWS_HEADS_UP_TTL_MINUTES`,
  default 3 min); the page fetches it on open only. At each scheduled time
  (`NEWS_HEADS_UP_NTFY_TIMES`, default `07:00`) the brief is also pushed to the phone
  over ntfy (`NEWS_HEADS_UP_NTFY_URL`, default `https://ntfy.sh/your-topic`).
- **Story feed** — cards sorted by display score, each showing: AI headline, summary,
  quality badge (color-coded), **relevance breakdown** (base + q/cred/impact/int/dist
  components, hover for formula), breaking pill, **`N sources` badge** (so you don't
  have to expand the card to see corroboration), tag pills, a **📍 location pill**
  (when the story's location isn't "Global" — explains any distance boost) and
  👍/👎 buttons.
- **Chat** — the **💬** button on a card (signed-in only) opens a per-story chat
  popup. The story's full context (headline, summary, metrics, articles) is
  injected into the model's SYSTEM prompt, so it answers in that story's frame
  of reference. Before answering, the agent may run a chosen number of
  search+scrape web turns against the local searchngx instance (selector:
  0/1/2/3/5/7/10, `NEWS_CHAT_SEARCH_TURNS`) to gather fresh context. History is
  persisted per story (`ChatSession`/`ChatMessage`) and survives restarts.
- Clicking a card expands an accordion with the per-source article timeline and links.
- **Load more** paginates in pages of 50; the `showing X/Y` status uses the server-side
  total. The UI refreshes live via SSE.
- **List** in the top bar opens `/list` — a flat, read-only table of every ingested
  article (title, feed · author · published date, body class, quality, status,
  per-stage pipeline timings: embed/cluster/harness/story/total) with newest/oldest
  sort and Load-more pagination. It's the tool for diagnosing slow or stuck
  articles. (`POST /api/refresh` still runs a full worker cycle on demand.)
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
- The status bar in the top right shows `showing X/Y stories · N enqueued ·
  last article Xm ago · live`; health figures are at `GET /api/health`.

## API

| Method & path          | Purpose                                            |
| ---------------------- | -------------------------------------------------- |
| `GET /api/stories`     | Ranked stories (`limit` slice + total `count` + pipeline `pending` + newest-article time) |
| `GET /api/stories/{id}`| One story incl. its articles                       |
| `GET /api/stories/{id}/chat` | Chat history + `search_turns_options` (signed-in only) |
| `POST /api/stories/{id}/chat` | Send a message: `{"message", "search_turns"}` → `{"reply"}` (signed-in only) |
| `GET /api/articles`    | Flat list of all ingested articles for `/list` (`limit`/`order`, per-stage timings) |
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
read-only set — `GET /api/stories`, `GET /api/stories/{id}`, `GET /api/articles`,
`GET /api/heads-up`, `GET /api/health`, `GET /api/events`, `/`, `/list`, `/static/*`
and the two auth probes above — returns **401** without a session (the `/feeds`
page redirects to `/` for browser navigation). `GET /api/stories/{id}/chat` is
deliberately **excluded** from the public story prefix: chat history is
signed-in only. With the password unset, auth is off and everything is open.

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
| `NEWS_WORKER_INTERVAL` | `300` (s) | worker cycle period |
| `NEWS_MAX_ITEMS_PER_FEED` | `10` | items per feed per cycle |
| `NEWS_W1` / `NEWS_W3` | `1.0` / `1.5` | display-score urgency / user-interest weights |
| `NEWS_W_DISTANCE` | `2.0` | distance-sorting weight (see below) |
| `NEWS_USER_CITY` / `NEWS_USER_COUNTRY` / `NEWS_USER_LANGUAGE` | `Munich` / `Germany` / `de` | user location for distance sorting |
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
| `NEWS_CHAT_TEMPERATURE` / `NEWS_CHAT_MAX_TOKENS` | `0.3` / `1024` | per-story chat generation params |
| `NEWS_CHAT_SEARCH_TURNS` | `0,1,2,3,5,7,10` | allowed searchngx web-search turn budgets for chat (shown verbatim in the UI) |
| `NEWS_CHAT_CONTEXT_PER_SOURCE` / `NEWS_CHAT_CONTEXT_MAX` | `1500` / `6000` | chars per scraped page / total web context folded into a chat prompt |
| `NEWS_SEARCHNGX_BASE_URL` | `https://searx.example.org/` | local searchapi.io-style web search instance |
| `NEWS_SEARCHNGX_API_KEY` | _(empty)_ | Bearer token (never logged); empty = unauthenticated |
| `NEWS_SEARCHNGX_ENDPOINT` | `api/v2/web_search` | search endpoint path |
| `NEWS_SEARCHNGX_TIMEOUT` / `NEWS_SEARCHNGX_MAX_RESULTS` | `30` / `5` | HTTP timeout (s) / results per search |

## Database

Single file `news.db` (WAL mode, foreign keys on, sqlite-vec loaded per connection):

- `Feed` — feeds added via the web UI (title, xml_url, category, ETag/Last-Modified cache,
  `enabled` = AI-ingestion toggle, see Feed management)
- `Article` — one row per RSS item; `content_raw` (may be a broken page — see
  `content_class`), `quality` (per-article), `story_id` (NULL = pending clustering),
  per-stage pipeline wall times (`t_embed_s`, `t_cluster_s`, `t_harness_s`, `t_story_s`,
  `t_wall_s` — NULL until the article's first fully successful pipeline run, shown in `/list`)
- `Story` — AI headline/summary/tags/metrics (incl. `location`/`language` for distance
  sorting), `quality_score` = median of member article qualities
- `Article_Embeddings` — sqlite-vec virtual table (`FLOAT[1024]`, title-first vectors)
- `UserProfile` — single local user: `tag_weights`, `source_weights`, `preferences`
- `FeedbackLog` — raw 👍/👎 votes (kept separate from the profile for later tuning)
- `ChatSession` / `ChatMessage` — per-story chat history (one session per story,
  messages cascade from the session, session cascades from the story)

## Ops & helper scripts

- Run: `cd news_aggregator_ai && uv run python main.py`. The old server ignores SIGTERM
  (uvicorn waits for in-flight Ollama calls) — `kill -9` the `.venv/bin/python` child if
  you must restart. Detached launch helpers in `scripts/`: `start.sh` (fully detached
  via `setsid -f`, refuses if the port is taken), `start_server.sh`, `launch_and_check.sh`
  (launch + port/log report). All append/overwrite `server.log`.
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
    ingest.py        # RSS fetch + extraction fallback chain
    pipeline.py      # embed → cluster → harness → Prompt2 → story update
    harness.py       # Jev-style Choice/Score/Boolean (schema-constrained)
    ollama.py        # Ollama HTTP client (chat /v1 + embeddings)
    chat.py          # per-story chat (system-prompt story context + search turns)
    searchngx.py     # best-effort web search/scrape client for the chat agent
    content_quality.py # ok/thin/broken/empty body classifier
    tags.py          # tag normalization (aliases, case/punctuation)
    scoring.py       # relevance/display score + feedback loop
    worker.py        # background scheduler
    bus.py           # SSE pub/sub
    api.py           # FastAPI routes
    frontend/        # index.html, app.js, feeds.html, feeds.js, chat.js,
                     # list.html, list.js, styles.css
  scripts/           # one-off & monitoring scripts
```
