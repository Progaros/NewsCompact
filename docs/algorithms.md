# News Feed Algorithm — Handoff Doc

Purpose: everything a (human or AI) developer needs to understand and improve the two feed lists — **"For You"** (`view=personal`) and **"For Everyone"** (`view=default`) — in this news aggregator.
Last verified against the code: 2026-10-26 (ranking v2.1).

**Owner decision (2026-10-26): no viewing-behaviour tracking.** No impressions, dwell/visible time, scroll depth or "how was it opened" logging. Personal ranking uses only the explicit tag list, 👍/👎 (and story chats, see below), the preferences, and read state (only to hide / lower stories already read).

## Stack at a glance

- FastAPI + SQLite (WAL, ~200 MB `news.db`), `sqlite-vec` for embeddings (cosine, 1024-dim `qwen3-embedding:0.6b-CPU`)
- LLM via Ollama: `qwen3:27b` at `http://127.0.0.1:11434` (env `CHAT_MODEL`)
- Two LLM prompts per article: **Prompt 1** (`app/harness.py`) = quality assessment; **Prompt 2** (`app/story.py`) = story build/update
- Config: `app/config.py`, all overridable via `NEWS_*` env vars

## Data flow (ingest → feed)

1. **Fetch**: RSS feeds (`Feed` table, cron-like `next_fetch_at`, `app/feeds.py` + worker every `WORKER_INTERVAL=300`s, batch `PIPELINE_BATCH=50`). `app/ingest.py` extracts full text when the RSS description is < `EXTRACT_MIN_DESC_WORDS=100` words; pulls ≤10 comments for Reddit/HN feeds.
2. **Dedup** (`app/dedup.py`): URL exact, then title-key (≥ `DEDUP_TITLE_MIN_WORDS=5` words, window `DEDUP_WINDOW_HOURS=72`).
3. **Cluster** (`app/clustering.py`): `CLUSTER_MODE=two_stage` — embed → cosine retrieve `KNN_CANDIDATES=200` → LLM verifier (same-story? threshold `CLUSTER_SIM_THRESHOLD=0.64`, window `CLUSTER_WINDOW_HOURS=48`). Articles join a `Story` (cluster) with `StoryAlias` links; new stories seeded. `app/merge.py` does background recall-merge; `app/split.py` splits oversized stories (`SPLIT_MIN_ARTICLES=30`, `SPLIT_FAR_SIM=0.4`).
4. **Score/Build** (`app/pipeline.py` + `app/story.py`): skips — thin article with story quality ≤ 2.0 (skip LLM), copy-dump (cosine ≥ `COPY_SKIP_SIM=0.9` to a member whose text covers all its words). Prompt 2 writes headline, summary (≤ `SUMMARY_MAX_CHARS=800`, shorten-retry), tags (≤6, normalized, `app/tags.py`), quality 1–10, location, language, `metrics {urgency_level 1–10, impact_scale Local|Regional|National|Global, status Breaking|Ongoing|Resolved}`. Incremental updates produce `new_facts` + `update_significance 0–3` → `StoryUpdate`; `news_at` advances only if significance ≥ 1 (a story is "fresh" when it got *new facts*, not when rewritten). Escalation: urgency jumping ≥2 to ≥7, or status → Breaking, forces significance 3. Breaking badge = status Breaking AND urgency ≥ 8.
5. **Retention** (`app/retention.py`): `RETENTION_DAYS=30`.

## The ranking formula v2.1 (app/scoring.py)

All features are normalized to 0..1 so weights are comparable; personalization is bounded;
freshness multiplies instead of subtracting, so every story fades unless it gets new facts.

```python
public   = 0.30·quality + 0.22·coverage + 0.16·urgency + 0.12·impact + 0.12·velocity + 0.08·status
personal = public + 0.30·tag_affinity (−1..1) + 0.08·geo (0..1)        # "For you" only
score    = 100 · freshness · soft_cap(value + update_boost)            # 0..100, shown as "Priority"
score   *= READER_FACTOR[read state]                                   # "For you" only
list     = MMR rerank of the top 50 (app/diversity.py)                 # both views, active only
```

| Feature | Definition |
|---|---|
| quality | trust-weighted median article quality / 10 (`source_stats`) |
| coverage | `1 − e^(−outlet_trust / 2)`: distinct outlets × their trust; saturating (2nd outlet adds more than the 6th) |
| urgency | `(urgency_level − 1) / 9`, capped at 0.65 for `Resolved` stories |
| impact | Local 0.25 · Regional 0.45 · National 0.7 · Global 1.0 |
| status | Breaking 1.0 · Ongoing 0.6 · Resolved 0.15 |
| velocity | `1 − e^(−(0.7·Σ VELOCITY_SIG_WEIGHT[sig]·fade(8 h) over StoryUpdate + 0.3·Σ outlet trust·fade(6 h) of each outlet's newest *original* article)/2)`; sig weights 1: 0.05 · 2: 0.35 · 3: 1.0, so one major update outweighs a run of minor ones; cross-feed copies don't count as reporting |
| update_boost | strongest recent update: `max(0.08 · BOOST_SIG_WEIGHT[sig] · fade(8 h))`, weights 1: 0.1 · 2: 0.45 · 3: 1.0 |
| freshness | `2^(−h since news_at / half-life)`, half-life Breaking 12 h · Ongoing 30 h · Resolved 12 h. An update moves `news_at` only `FRESHNESS_REFRESH[sig]` (1: 10 % · 2: 50 % · 3: 100 %) of the way to its *publication* time (`story.trigger_time`: article `published_date`, at most 24 h before first seen, never in the future), stored as `StoryUpdate.event_at` |
| reader factor | "For you": read × 0.2 · updated since read (sig 2) × 0.5 · updated with a major (sig 3) update × 0.85 · unread × 1; stories read during the current page visit (`keep`) are exempt so they don't jump |
| diversity | MMR: `0.82·score/top − 0.18·max similarity to a story above`; similarity = max(story-vector cosine rescaled from 0.5, StoryLink `CONTINUING_DEVELOPMENT` 0.95 / `SAME_TOPIC_DISTINCT_EVENT` 0.75); story vector = content-class-weighted mean of the newest 8 original article vectors. Moved stories get `relevance.similar_to` |
| tag_affinity | per matching tag `tanh(weight/4)` (1→0.25, 3→0.64, 10→0.99); tag list wins over learned weights; learned ones × specificity (normalized IDF over active stories, floor 0.2); combined by noisy-OR per sign: `pos − neg` |
| geo | city 1.0 · country 0.6 · language 0.35, or 0.5 per `interest_regions`/`locations` tag hit |
| soft_cap | linear to 0.8, then `0.8 + 0.2·tanh((x−0.8)/0.2)` — no ties among the best stories |

- **Mute**: a tag at weight ≤ −10 hides its stories from "For you" (hard filter, not a penalty); an explicit tag filter still shows them.
- **Tag list vs learned weights**: `UserProfile.tag_weights` is only what the user set. 👍/👎 (±2/√n per tag, half-life up 60 d / down 90 d) and story chats (+1/√n, 90 d) are derived at ranking time from `FeedbackLog`/`ChatSession` (`inferred_weights`, never stored). Migration v8 removed weights that votes alone had written (179 → 71 tags on the owner profile).
- **For Everyone** = `public` only (no tags, no location, category trust from `SOURCE_TRUST`).
- **Archive** = older than `STORY_ACTIVE_DAYS`, newest first, not scored.
- Every knob is `NEWS_*` env-overridable (`app/config.py`, "Ranking v2"). Weights are starting hypotheses, not validated constants.
- API: `story.relevance` is the breakdown (`version`, `parts` in points, `matches`, `geo_reason`, `freshness`, `half_life_h`, `age_h`, …), `display_score` the priority.

### Request flow (`GET /api/stories` → `scoring.ranked_stories`)

1. Candidates: stories with a headline and `news_at` within `STORY_ACTIVE_DAYS=7` (archive: older, sorted by `news_at`).
2. Filters: search `q` (LIKE on headline/summary/tags), `tag` filter, `preferences.min_quality`, `excluded_feeds` (their articles don't count; a story with only those disappears), muted tags (For you only, not with a tag filter).
3. Score every candidate (no caching, ~0.4 s), apply the reader factor (For you), sort by `display`.
4. For you + Active + "Show read" off → `hide_read=true`: drop stories in state `read` (not `updated`); `keep` = ids read during this page visit, so they don't vanish while scrolling.
5. Diversity rerank of the top 50 (active lists only).
6. Paginate (`limit`, "more" button re-requests the larger window).

Other consumers: heads-up brief = top 5 of the same ranking (personal: unread or updated since read only); scheduled ntfy reports = top 3 of the default ranking.

### Measured on the 2026-10-23 DB copy (5,310 active stories, 1 user)

| | v1 | v2 |
|---|---|---|
| interest share | p90 hit the +10 cap; interest sd 3.9 vs relevance sd 1.2 → tags dominated | tags ≤ 30 of 100 points |
| age | decay saturated (median 9.6 of max 10) → old stories ranked by interest/urgency alone | freshness × value; top 20 median age 1.6 h |
| For Everyone top 20 | — | median 8 outlets; 2 Breaking |
| ranking time | — | 0.4 s for 5,310 stories |

## Tag synonyms (app/tags.py, app/tag_merge.py)

- Static aliases: unambiguous EN/DE pairs (`ki→ai`, `munchen→munich`, `politik→politics`, `wirtschaft→economy`, …; not `bayern`, which is also the football club).
- `TagAlias` table, applied at read time by `normalize_tag`, so stored stories and tag lists follow without rewrites; merged tag weights keep the strongest value, never the sum.
- Daily worker pass (`TAG_MERGE_*`): tags on ≥ 3 stories or on a tag list → `qwen3-embedding` (cached in `TagVector`) → cosine kNN ≥ 0.80 → LLM verifier (`SAME_CONCEPT | BROADER | NARROWER | RELATED | AMBIGUOUS | UNRELATED`). Auto-approve only `SAME_CONCEPT` with cosine ≥ 0.90 and confidence ≥ 0.9; other `SAME_CONCEPT` → pending; everything else → rejected (not asked again). Review: `scripts/merge_tags.py list|approve|reject|add`.
- Thresholds are **uncalibrated** (Ollama was unreachable from the dev machine). Calibrate with `scripts/merge_tags.py neighbours --min-sim 0.7` and `propose --dry-run` before trusting auto-approval.

## Available data (raw material for expanding the algorithm)

Everything below is already stored per article/story — unused or under-used inputs a new algorithm could consume:

- **`Article`**: `content_raw` (full text!), `author`, `content_class` (ok/thin/broken/empty), per-article `quality`, **stage timings** `t_embed_s`…`t_merge_s` (latency profiling, SQLite-internal columns), `pipeline_failures`/`pipeline_timeouts` (internal, exposed only via `pipeline_counts()` status breakdown), `duplicate_of`, `retry_after`, `processed_at`.
- **`Article_Vectors`**: 1024-dim embeddings — enables genuine semantic similarity/serialization instead of tag-match or cosine heuristics.
- **`StoryLink`**: relations already computed (`SAME_TOPIC_DISTINCT_EVENT`, `CONTINUING_DEVELOPMENT`, merge links) — unused in scoring; could feed "developing story" weighting or dedup of coverage.
- **`StoryUpdate`**: `significance`, `facts` — drives `news_at` and, since v2, velocity and the update boost.
- **Per-user**: `UserProfile` `tag_weights`, `source_weights`, `feed_weights`, `preferences` (interest_regions, locations, min_quality, excluded_feeds, about, home_city/country/language), `FeedbackLog` (one up/down per user×story, with headline/tags snapshots!), `StoryRead` (latest read time only — used to hide/lower read stories, never as an interest signal), per-story **chat** (`ChatSession`/`ChatMessage`, tree-structured) — user asks questions about stories; a strong interest signal.
- **`metrics`** JSON already contains fields scoring mostly ignores: `impact_scale` (Local→Global), `status` (Breaking/Ongoing/Resolved), `language`, `location` — impact_scale and status could modulate urgency/distance directly.
- **Config knobs**: nearly every constant above is an env-overridable knob, so experiments need no code changes for weight tuning.

## Interaction (feedback & UI) surface

- **Tag popup on story cards** (`app/frontend/app.js`, `tagMenuRender`): one click sets a weight — `Mute (−10) · −3 · +1 · +3 · +10` — plus an exact input, Remove, and "Filter feed by this tag". A toast offers Undo. Tag pills show their weight (`#ai +9`); negative ones are red, muted ones struck through.
- **👍/👎** (`POST /api/feedback`): one vote per user × story, logged in `FeedbackLog` with a headline/tags snapshot. Feeds learned weights only; the tag list is never edited. Hidable via ⚙ Preferences → "Show 👍/👎 on stories" (`preferences.show_votes`).
- **Preferences** (`app/frontend/auth.js`): editable tag list; read-only "Learned interests" (click to pin one to the tag list); source trust sliders, `⊘` hide feed (`excluded_feeds`); home city/country/language, `interest_regions`, `locations`, `min_quality`, `about` (LLM texts only).
- **Read / unread & resurfacing**: expanding an unread card or clicking its headline marks it read (signed in); the ✓ button toggles read/unread (unread deletes the row). `StoryRead` keeps only the latest `read_at` per user × story — no history, no dwell, no record of *how* it was marked (by design). A story becomes `updated` again when `material_at > read_at` (an update with significance ≥ `READ_RESURFACE_SIGNIFICANCE=2`) and shows up to 3 "New since you read it" facts.
- **"Show read" toggle** (For you + Active only, `localStorage`): see request flow step 4.
- **Expanded card**: plain facts (`High urgency · Global impact · Quality 7.0 · 7 outlets · 2 updates today`), "Why it is in your feed" (built only from the breakdown: tag-list matches, learned matches, location reason, new facts), and a collapsed "Priority 71/100 · how it is computed" table with every part in points, the freshness factor, the "Already read" factor and a "Variety" row when the rerank moved it.
- **Story chat** (`app/chat.py`): a session with ≥ 1 message = one `+CHAT_WEIGHT` event for the story's tags, regardless of message count. Editing a message (✎, `edit_of`) adds a sibling branch (`/chat/switch` changes the shown one) — not a signal.
- **Not signals**: sharing (`ShareLink`, story links/briefs/chats), search queries, tag filter clicks, source-list expansion, the "↑ n new updates" pulse (counts stories whose `news_at` moved).
- **Feeds page** (`/feeds`): add/remove/enable feeds, set categories → indirectly changes trust via `SOURCE_TRUST`/category.

## Review of the external proposal (2026-10-23)

Adopted: staged eligibility → scoring → personalization; normalized features; multiplicative,
status-dependent freshness; saturating coverage; impact/status/velocity; resolved-urgency cap;
tanh-bounded tag affinity; explicit vs inferred preferences with decay; specificity (IDF) for
learned weights; 👍/👎 no longer touching the tag list; one-click weight presets with Undo;
deterministic explanations + inspector; canonical tag aliases via embedding retrieval + LLM
verification + review queue; strongest-not-sum when merging weights.

Changed on purpose:
- **Specificity applies to learned weights only.** Explicit tag weights are deliberate choices; scaling the user's "ai = 9" down to 0.27 because "ai" is common would override them.
- **Noisy-OR instead of `0.7·max + 0.3·mean`.** The mean lowers affinity when a weak extra tag matches; noisy-OR never does and still saturates at ±1.
- **No profile-confidence factor.** Explicit weights should act fully from the first click; learned ones are already small (±2/√n with decay).
- **No content-sufficiency factor yet.** 9,682 of 33,036 articles are `thin` (mostly RSS-only feeds); a 0.7 factor would demote a third of the corpus. Title-only quality is already capped at 6.
- **No StoryLink `arc` term.** Only 400 `CONTINUING_DEVELOPMENT` links; velocity covers momentum.
- **Soft cap instead of `clamp01`.** On real data the personal value exceeded 1.0 for top stories, which tied them.
- **Three statuses, not five half-lives.** Prompt 2 emits only Breaking/Ongoing/Resolved.

Not done yet (next steps, in order):
1. **Calibrate** freshness half-lives, refresh shares and weights by inspecting rankings by hand (no impression data by design): `scripts/compare_rankings.py` prints the "For you" top under the variants `before` (v2), `gentle`, `current`, `strict` with the number of minor-only stories, median age and near-duplicate pairs; calibrate tag-merge thresholds on a labeled EN/DE pair set.
2. **Explicit-only semantic profile** (optional): story vectors of 👍/👎 stories and the tag list → semantic affinity next to tag affinity. Never from reads, opens or view time.

Dropped (owner decision 2026-10-26): impression/dwell logging, implicit signals from opening or reading, learned rankers trained on viewing behaviour.

## Review 2026-10-26 — weak spots found in the code

Fixed in v2.1: 1 (partial refresh), 2 (publication time), 3 (reader factor, brief), 4 (strongest boost), 5 (velocity weights, copies), 6 (headline refresh), 7 (diversity). Dropped: 8, 9 (no implicit signals).

1. **Minor updates fully reset freshness.** `story.py` sets `news_at = now` for any significance ≥ 1, and 1 means "minor detail, reaction or opinion". So a reaction quote gives freshness 1.0 *plus* velocity *plus* update boost → minor updates float to the top.
2. **`news_at` is processing time, not publication time.** Pipeline backlog and Ollama-timeout retries (`retry_after`) make old news look brand new. Use the triggering article's `published_date` (clamped to `[created_at − 1 d, now]`).
3. **Read state doesn't affect ranking.** An `updated` story ranks by the whole story's score, not by what is new *to this reader* (max significance and age of the updates after `read_at`). With "Show read" on, read stories rank like unread ones (could be × ~0.3). The heads-up brief can repeat stories the user already read.
4. **`update_boost` uses the last update, not the strongest.** `story_dynamics` overwrites `boost` per row, so a significance-1 update right after a 3 lowers it. Use the max of the faded boosts.
5. **Velocity rewards chatter.** `Σ sig/3` is linear, so five minor updates count more than one major one; outlet `heat` also counts copies/reposts that added no facts. Consider `sig²` weighting, counting only ≥ 2, and heat only from articles that changed the story.
6. **Headline is frozen on incremental updates** (Prompt 2: "Preserve the story headline"), so a story re-ranked for new facts still shows its first angle; only the expanded card reveals what changed. Now: Prompt 2 writes a neutral headline for the story as a whole and its current state; an incremental update may rewrite it only at significance ≥ `HEADLINE_REFRESH_SIGNIFICANCE=2` (enforced in `story.pick_headline`). New facts must be about the story's core event (off-topic facts → not new).
7. **No diversity.** Same-topic stories and `CONTINUING_DEVELOPMENT` / `SAME_TOPIC_DISTINCT_EVENT` chains (`StoryLink`) can fill the top; they are only shown as "related".
8. **Cheap implicit signals unused.** Opening a card / clicking a headline is an explicit click (unlike impressions) → safe weak positive. ✓-without-opening (≈ "seen, not interested") is stored identically; add a `StoryRead.how` column (`open | headline | button`) to tell them apart. Chat depth (message count) and shares are also ignored.
9. **`preferences.about`** (free-text self description) is used only for LLM texts; it could seed the semantic profile (embed it once).

Guardrails for changes: keep weights env-overridable in `app/config.py`, keep the breakdown/explanations in sync (`parts`, "Why it is in your feed", score inspector), update `tests/test_scoring.py`, and don't add any viewing-behaviour tracking.

## Suggested reading order for an implementer

1. `app/scoring.py` (`score_story`, `ranked_stories`, `inferred_weights`, `tag_signals`)
2. `app/config.py` ("Ranking v2" and `TAG_MERGE_*` knobs)
3. `app/tags.py` + `app/tag_merge.py` (normalization, aliases, synonym pass)
4. `app/frontend/app.js` + `app/frontend/auth.js` (story card UI, tag popup, preferences modal)
5. `app/schema.sql` + `app/story.py` (stored schema and Prompt 2 output fields)
