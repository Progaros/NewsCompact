# News Feed Algorithm — Handoff Doc

Purpose: everything a (human or AI) developer needs to understand and improve the two feed lists — **"For You"** (`view=personal`) and **"For Everyone"** (`view=default`) — in this news aggregator.

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

## The ranking formula v2 (app/scoring.py, since 2026-10-23)

All features are normalized to 0..1 so weights are comparable; personalization is bounded;
freshness multiplies instead of subtracting, so every story fades unless it gets new facts.

```python
public   = 0.30·quality + 0.22·coverage + 0.16·urgency + 0.12·impact + 0.12·velocity + 0.08·status
personal = public + 0.30·tag_affinity (−1..1) + 0.08·geo (0..1)        # "For you" only
score    = 100 · freshness · soft_cap(value + update_boost)            # 0..100, shown as "Priority"
```

| Feature | Definition |
|---|---|
| quality | trust-weighted median article quality / 10 (`source_stats`) |
| coverage | `1 − e^(−outlet_trust / 2)`: distinct outlets × their trust; saturating (2nd outlet adds more than the 6th) |
| urgency | `(urgency_level − 1) / 9`, capped at 0.65 for `Resolved` stories |
| impact | Local 0.25 · Regional 0.45 · National 0.7 · Global 1.0 |
| status | Breaking 1.0 · Ongoing 0.6 · Resolved 0.15 |
| velocity | `1 − e^(−(0.7·Σ sig/3·fade(8 h) over StoryUpdate + 0.3·Σ outlet trust·fade(6 h) of each outlet's newest article)/2)` |
| update_boost | `0.08 · last_significance/3 · fade(8 h)` |
| freshness | `2^(−h since news_at / half-life)`, half-life Breaking 12 h · Ongoing 30 h · Resolved 12 h |
| tag_affinity | per matching tag `tanh(weight/4)` (1→0.25, 3→0.64, 10→0.99); tag list wins over learned weights; learned ones × specificity (normalized IDF over active stories, floor 0.2); combined by noisy-OR per sign: `pos − neg` |
| geo | city 1.0 · country 0.6 · language 0.35, or 0.5 per `interest_regions`/`locations` tag hit |
| soft_cap | linear to 0.8, then `0.8 + 0.2·tanh((x−0.8)/0.2)` — no ties among the best stories |

- **Mute**: a tag at weight ≤ −10 hides its stories from "For you" (hard filter, not a penalty); an explicit tag filter still shows them.
- **Tag list vs learned weights**: `UserProfile.tag_weights` is only what the user set. 👍/👎 (±2/√n per tag, half-life up 60 d / down 90 d) and story chats (+1/√n, 90 d) are derived at ranking time from `FeedbackLog`/`ChatSession` (`inferred_weights`, never stored). Migration v8 removed weights that votes alone had written (179 → 71 tags on the owner profile).
- **For Everyone** = `public` only (no tags, no location, category trust from `SOURCE_TRUST`).
- **Archive** = older than `STORY_ACTIVE_DAYS`, newest first, not scored.
- Every knob is `NEWS_*` env-overridable (`app/config.py`, "Ranking v2"). Weights are starting hypotheses, not validated constants.
- API: `story.relevance` is the breakdown (`version`, `parts` in points, `matches`, `geo_reason`, `freshness`, `half_life_h`, `age_h`, …), `display_score` the priority.

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
- **Per-user**: `UserProfile` `tag_weights`, `source_weights`, `feed_weights`, `preferences` (interest_regions, locations, min_quality, excluded_feeds, about, home_city/country/language), `FeedbackLog` (one up/down per user×story, with headline/tags snapshots!), `StoryRead` (read/updated/unread timestamps — dwell/read-history is NOT yet a signal), per-story **chat** (`ChatSession`/`ChatMessage`, tree-structured) — user asks questions about stories; a strong interest signal.
- **`metrics`** JSON already contains fields scoring mostly ignores: `impact_scale` (Local→Global), `status` (Breaking/Ongoing/Resolved), `language`, `location` — impact_scale and status could modulate urgency/distance directly.
- **Config knobs**: nearly every constant above is an env-overridable knob, so experiments need no code changes for weight tuning.

## Interaction (feedback & UI) surface

- **Tag popup on story cards** (`app/frontend/app.js`, `tagMenuRender`): one click sets a weight — `Mute (−10) · −3 · +1 · +3 · +10` — plus an exact input, Remove, and "Filter feed by this tag". A toast offers Undo. Tag pills show their weight (`#ai +9`); negative ones are red, muted ones struck through.
- **👍/👎** (`POST /api/feedback`): one vote per user × story, logged in `FeedbackLog` with a headline/tags snapshot. Feeds learned weights only; the tag list is never edited. Hidable via ⚙ Preferences → "Show 👍/👎 on stories" (`preferences.show_votes`).
- **Preferences** (`app/frontend/auth.js`): editable tag list; read-only "Learned interests" (click to pin one to the tag list); source trust sliders, `⊘` hide feed (`excluded_feeds`); home city/country/language, `interest_regions`, `locations`, `min_quality`, `about` (LLM texts only).
- **Read / unread & resurfacing**: opening a card marks it read; `update_significance ≥ 2` resurfaces it with "New since you read it".
- **Expanded card**: plain facts (`High urgency · Global impact · Quality 7.0 · 7 outlets · 2 updates today`), "Why it is in your feed" (built only from the breakdown: tag-list matches, learned matches, location reason, new facts), and a collapsed "Priority 71/100 · how it is computed" table with every part in points and the freshness factor.
- **Story chat** (`app/chat.py`): counts as a learned positive signal for the story's tags.
- **Sharing** (`ShareLink`): not a signal.

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
1. **Impression logging** (`FeedImpression`: user, story, position, view, score version, components, visible ms). Prerequisite for using skips/dwell and for any offline evaluation (NDCG@10, pairwise accuracy). Without it, non-clicks must not be treated as negatives.
2. **List-level diversity rerank** (MMR on story vectors; ≤ 2 per topic/outlet in the first 10; collapse `CONTINUING_DEVELOPMENT` chains).
3. **Semantic profile**: trust-weighted story vectors from `Article_Vectors`, decayed positive/negative centroids from votes/chats/reads → semantic affinity next to tag affinity.
4. **Calibrate** freshness half-lives and weights against impression data; calibrate tag-merge thresholds on a labeled EN/DE pair set.
5. **Learned ranker** (logistic / pairwise LTR / GBDT) only once enough impressions exist; keep mutes, freshness, diversity and canonicalization outside it.

## Suggested reading order for an implementer

1. `app/scoring.py` (`score_story`, `ranked_stories`, `inferred_weights`, `tag_signals`)
2. `app/config.py` ("Ranking v2" and `TAG_MERGE_*` knobs)
3. `app/tags.py` + `app/tag_merge.py` (normalization, aliases, synonym pass)
4. `app/frontend/app.js` + `app/frontend/auth.js` (story card UI, tag popup, preferences modal)
5. `app/schema.sql` + `app/story.py` (stored schema and Prompt 2 output fields)
