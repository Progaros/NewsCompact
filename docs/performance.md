# Performance & AI quality analysis

> Historical audit (2026-10-11). Most ranked items have since been implemented —
> see [`plan.md`](./plan.md) → Changelog; open ones are in [`todo.md`](./todo.md).

Quality audit of every AI-driven component, with evidence from the live DB,
`server.log`, and spot-reads. Companion to [`findings.md`](./findings.md)
(raw load/utilisation numbers live there) — this doc is about **whether the
AI output is good**, where it fails, and what to improve (ranked,
quality-preserving or quality-improving). All data as of 2026-10-11.

Components covered: the classification harness, the deterministic content
classifier, embeddings + clustering, Prompt2 (story rewrite), the heads-up
brief, and the LLM-adjacent tag/personalization path.

---

## 1. Current state (2026-10-11)

### 1.1 Classification harness — `LocalJevHarness.assess_article` (app/harness.py)

One schema-constrained `/api/chat` call (merged quality + breaking + content
routing on 2026-10-11): `format` JSON, `temperature=0`, `think:false`,
`num_predict=100`. Returns `{quality 1-10, breaking bool}` and pairs it with
the deterministic `content_class`.

**Observed quality (snapshot; counts drift as the backlog drains):**

| quality | 1-2 | 3 | 4-6 | 7-8 | 9-10 |
|---|---|---|---|---|---|
| count | 266 (51%) | 15 | 60 | 169 | 17 |

(n ≈ 527 scored at snapshot)

- The distribution is bimodal by design: the calibration anchors
  ("1-2 speculative/junk", "7-8 solid journalism") split articles into
  *junk-tier* (scored from headline only) and *journalism-tier*. The middle
  band (4-6 "ordinary wire copy") is barely used (11%) — the scale is really
  a 2-tier gate, which is fine for its actual job (junk must not drag story
  quality down), but it means `quality` is a weak ordering tool *within*
  journalism (see ideas).
- Recent 18 harness calls (German news batch): quality 6×1, 7×5, 8×12 —
  consistent with the anchors.
- Breaking flag: strict. 7 "Breaking" statuses across 260+ stories —
  appropriate for a personal feed, not a false-alarm generator.
- Harness latency: 7–26 s (prefill-dominated on 12k-char bodies; output is
  5–20 tokens). Typical per-article wall after the merge: 26–51 s.

### 1.2 Content classifier — `content_quality.py` (deterministic)

Opening-window boilerplate check, no LLM. Of 522 classified articles:
**junk 380 (73%), ok 119 (23%), thin 23 (4%)** — i.e. ~3 of 4 stored bodies
are chrome, confirming the classifier is load-bearing: it decides embedding
text (title-only for non-ok) and whether quality is scored from the headline.

`content_class` is persisted *only* when an article runs through the
pipeline — the 2,445 NULL rows are the unprocessed backlog, **not** "ok".
(The column's semantics are confusing; see ideas.)

### 1.3 Embeddings + clustering (app/pipeline.py)

`qwen3-embedding:0.6b-CPU` (1024-dim), title-first text (title + first 100
body words; non-ok bodies → title only). Member-anchor KNN (2026-10-11):
join if max cosine to any non-junk member ≥ 0.64 (48 h window), revive at
0.62 (no window). Supersedes star/centroid mode — the centroid kept
*drifting* even with junk excluded (story 7: 23-article mush → 4-member
OpenAI core; see findings.md "Member-anchor clustering"). Known trade:
generic-band stories (e.g. 295, Local AI) now fragment to tight cores
(their members sit in the 0.53–0.59 band the 0.6b embedder can't
separate) instead of merging across topics.

**Observed coherence (spot-reads, 2026-10-11):**

- Story 465 (largest, 20 members, q7.0): *all* September-2026 zero-day vendor
  advisories (Citrix NetScaler, F5 BIG-IP, Cisco SEG, SonicWall SMA1000, SAP,
  GitLab, …) — a genuinely coherent cluster.
- Junk-only clusters stay tight under the stricter rule: story 302 (7
  GrapheneOS junk members, one topic), story 295 (2 local-AI members) — both
  coherent, no cross-topic absorption.
- Size distribution: 231/260 (89%) single-member, 13 two-member, 17 with ≥3
  (max 20). Distinct topics get distinct stories; no catch-all dumping.
- **174/260 (67%) stories are junk-only** (no real-body anchor). This is
  expected given the 73% junk fetch rate, but it means most "stories" in the
  DB are 1-2 member junk clusters kept alive by the quality=2 floor —
  see ideas for whether that should stay.

### 1.4 Prompt2 — story rewrite (app/pipeline.py `prompt2_update_story`)

Full rewrite per new member: all member titles + full text (budget-capped at
200k chars; older members compressed to bullets) → `{headline, summary
(2-4 sentences), tags, quality, metrics}` via `/v1` + lenient JSON
extraction. Story quality = median of member article qualities.

**Observed quality (spot-reads of high- and low-q stories):**

- High-q (7-8): headlines are specific and factual ("Bundesrechnungshof
  kritisiert Digitalministerium: Mindestens 1,2 Mrd. Euro …"), summaries
  2-4 sentences with named sources/numbers, metrics plausible
  (urgency 3-8, Local/National/Regional used proportionally to content).
- Low-q (2-3): correctly labeled junk (forum asks, Reddit threads, human
  interest) — *accurately* low, not miscorated.
- Language handling: German and English stories both handled in their source
  language.
- Metrics usage across 260 stories: status Ongoing 184 / Resolved 45 /
  Breaking 7; impact Local 111 / Regional 46 / National 49 / Global 51.
  Enums used consistently; one placeholder story at snapshot had empty
  metrics (created, not yet Prompt2'd — normal cycle state).

**Failure mode (observed live):** the model occasionally returns `null` or
prose → `_extract_json`/type check raises → article un-pended, placeholder
story deleted, retried next cycle. Two instances in the current log window
(articles 1113, 1122: `Prompt2 returned non-object JSON: 'None'`).
Retry state today: 2 enabled-feed articles waiting (harmless), 162
partial-state articles belong to *disabled* feeds and cost nothing. **The
drain is not stalled** — but the retry policy has no backoff, no in-cycle
retry, and no dead letter (see ideas).

**Cost grows with story size:** the 20-member story 465 sends a 198,291-char
prompt per new member → p2 = 163 s (vs 21-30 s typical). Prompt2 latency is
~linear in story size; the largest stories are 3-6× slower.

### 1.5 Heads-up brief (app/api.py `_build_brief`)

Top-5 stories by `display_score` → one LLM call → "2-3 sentence plain-text
brief", temp 0.2, 3-min TTL cache, failures not cached, scheduled ntfy push
(07:00). Live check 2026-10-11: output was factual, preamble-free, and every
claim traced to a real top story (Cloudflare/Deno acquihire, Nobel Pillay,
ICC sanctions, BND Hanning detention, OpenAI math papers). No hallucinated
story-level facts observed; risk is bounded by construction (brief may only
compress the given summaries).

### 1.6 Tags & personalization (app/tags.py, app/scoring.py)

- 1,590 tag occurrences across stories → **1,261 distinct (80%)**. The alias
  table is tiny (11 entries), so LLM spelling variants mostly *do* split
  weight — but the bigger defect is visible: umlauts are deleted, not
  mapped: `"Zölle" → "z lle"`, `"Identitätengesetz" → "digitale identit
  tengesetz"`, `"IT-Sicherheit" → "it sicherheit"`. German feeds (a large
  fraction) get broken tags, and "Zölle" vs "Zolle" would be two tags.
- `FeedbackLog` has 0 rows: the interest-weight personalization path is
  unexercised; `display_score` today is effectively quality + urgency +
  decay. Fine for single-user, but means tag quality's only consumer is
  untested.
- Dead code: `prompt1_assess` + `_P1_SYSTEM` (the old per-article tags
  path) are no longer called — story tags come from Prompt2 only.

---

## 2. Known issues & failure modes

1. **Prompt2 can still fail after the constrained call and fallback.** The
  article is rolled back, partial quality/class state is cleared, and
  `pipeline_failures` caps repeated selection at three attempts by default.
  There is still no in-cycle retry or persisted failure reason.
3. **Prompt2 payload grows with story size** (198k chars observed) → the
   biggest stories are the slowest per article, and full re-sends re-parse
   the same context every member (drift risk grows with size).
4. **Quality scale is bimodal** (51% ≤2, 32% 7-8, ~11% middle) — by design,
   but only the junk-vs-real split is actually informative.
5. **Tags break non-ASCII** (`normalize_tag` regex `[^a-z0-9]` strips
   umlauts instead of mapping them) + 80% tag cardinality → weight splitting
   for personalization if/when it's used.
6. **`content_class` NULL ≠ ok** — NULL means "never classified"; the column
   is written late (at pipeline time) instead of at ingest, so "classify" is
   conflated with "processed".
7. **67% junk-only stories** — most DB rows in `Story` are 1-2 member junk
   clusters. Coherent, but they dominate the table and every new junk fetch
   spends a Prompt2 call on a story that will likely stay junk-only.
8. **`BLOCK_SIGS` has `"wurde blockiert"` three times byte-identical**
   (the "count ≥ 3" repetition guard counts each occurrence ×3, so a single
   occurrence in a body already triggers junk). Works today because the
   opening-window gate is narrow, but the threshold is not what it claims.
9. **No automated quality gate** — every prompt/model/threshold change is
   verified by eye. The clustering thresholds (0.64/0.62, member-anchor)
   against the 0.6b embedder and the current prompts; nothing would flag a
   regression if the model or a prompt changed.
10. **Single serial LLM stream** — one article at a time; harness and
    Prompt2 for article N+1 can't overlap. ~99% of wall time is remote
    Ollama latency, so the app is a thin client of one GPU (see
    findings.md "Load & utilisation" for the measured split).

---

## 3. Improvement ideas (ranked by quality impact / cost)

Ranked so that every item **keeps or improves** quality; cost = effort +
risk, quality impact = what it buys.

### 3.1 Constrain Prompt2 with `format` (like the harness) — *reliability, low cost*

Prompt2 goes through `/v1` + lenient JSON extraction; the harness already
proved `/api/chat` + `format` works on this model. A constrained call kills
the `null`/prose failure mode (issue 1) structurally instead of retrying
around it. Caveats to verify first: (a) the story JSON is larger than the
harness's — check `num_predict` headroom and that `think:false` stays on;
(b) `metrics` nested object + enums fit a schema cleanly. Fallback: keep
`chat_json` path if constrained output degrades prose quality.

### 3.2 Prompt2 incremental update — *throughput + quality on big stories, medium cost*

Rewrite from **previous summary + new member article(s)** instead of
re-sending all members. Kills the 198k-char payloads (issue 3): latency
becomes constant per story regardless of size (the biggest stories today
cost 3-6× the smallest), and the model focuses on the delta instead of
re-deriving a known summary (less drift, less truncation pressure).
Risk: summaries become compounding memory — a bad summary can persist.
Mitigation: recompute-from-scratch on story revival (the 0.62 path already
re-enters stories) and/or when member count is small; keep the 200k
full-resend path as the "rebuild" action. This is the single biggest
quality-per-second win for the future as stories grow.

### 3.3 Output validation layer on Prompt2 — *cheap, do with 3.1*

Even with constrained JSON, validate at write time: `impact_scale`/`status`
must be in the enum (today `setdefault` only fills *missing* keys, not
invalid values), `urgency_level` clamped to 1-10, headline non-empty +
length-capped, summary sentence count in 1-5, tags ≤ 6. Store the raw
output on the story row on violation so failures are diagnosable instead of
logged.

### 3.4 Retry policy + dead letter for pipeline articles — *low cost*

On Prompt2/harness failure: retry once in-cycle (the transient `null` case
almost always passes on the second call — article 1113 did), then mark the
article with a fail count and stop re-picking it after N (e.g. 3) cycles;
log + expose in `/api/health` so a persistently failing feed is visible
instead of silently burning GPU. Also make the rollback clean (issue 2):
clear `quality`/`content_class` on un-pend, or move the harness write after
Prompt2.

### 3.5 Fix tag normalization for non-ASCII — *visible quality on German feeds, trivial*

Map diacritics *before* the `[^a-z0-9]` strip (`ö→o, ä→a, ü→u, ß→ss`) —
"Zölle" → "zolle" instead of "z lle". Then the alias table can actually
unify German variants ("zolle"/"zoll" for tariffs etc.). One-line change +
a `normalize_tags.py` re-run over stored stories.

### 3.6 Classify content at ingest; embed in batches — *cleaner semantics, low cost*

Run `content_quality.classify` when the article is stored (not at pipeline
time): `content_class NULL` stops meaning "unprocessed", junk bodies are
known before any LLM spend, and the title-first embedding text is fixed at
ingest → embeddings can be batched (`embed_texts` already exists) instead of
one-per-article, and pipeline cost drops to harness + Prompt2 only.

### 3.7 Decide what junk-only stories are *for* — *design, medium*

Two defensible end-states, pick one and document:
- **(a) keep**: junk stories are real content (a personal feed's junk is
  still signal), the quality=2 floor already sinks them — then the 0.62
  junk-only rule is the design, keep it, and consider *not* re-running
  Prompt2 on a junk-only story when a *second junk* member joins (a title
  list + median quality is enough; spend the LLM call when a real anchor
  joins).
- **(b) quarantine**: junk-only articles don't form stories until an ok/thin
  article joins (cluster them, hold them out of `Story`); the story list
  becomes 100% real news.
Either way, the current "every junk fetch is a Prompt2 call" is the most
expensive part of the pipeline relative to its value.

### 3.8 Quality gate / eval harness — *the safety net for everything above, medium*

No automated metric exists today. Minimal viable version, in `scripts/`
style: a fixed golden set (~30 articles across junk/ok/real-news + a few
known-topic article sets for clustering) scored by an LLM-judge prompt
(headline accuracy, summary faithfulness, correct junk call, cluster
membership) run after any prompt/model/threshold change; plus a monthly
20-story human spot-read (5 min). Without this, 3.1/3.2/3.6 are all
unverifiable regressions by construction.

### 3.9 Metric calibration anchors — *low cost, small gain*

Quality got explicit anchors (9-10 … 1-2) and became discriminating;
`urgency_level` and `impact_scale` got none. Add the same treatment
("urgency 8+ = active outage / live coverage only; impact from named
geography in the text") — the current metrics look sensible, anchors would
keep them sensible after model changes.

### 3.10 Harness batching — *throughput, low-medium*

The harness call's cost is prefill (12k context) not output (5-20 tokens).
Batch 5-10 articles per call (JSON array of assessments, one `format` schema
with an array) → prefill cost per article drops ~5-10× during backlog
drain. Only worth it while the backlog exists; the serial-stream design
(issue 10) is otherwise fine because the GPU is the bottleneck anyway.

### 3.11 Embedding model upgrade — *quality, do deliberately*

A GPU-resident or larger `qwen3-embedding` variant tightens clusters
(noticeably more so for title-only junk, where the 0.6b CPU model is the
weak link). **Must ship with re-calibrated member-anchor thresholds
(0.64/0.62)** — the
current values are calibrated to the 0.6b scale and are not portable.
Gate it with the 3.8 golden set: run both models on the set, compare
membership before touching config. New articles embed in ~0-5 s either
way (embeds are cached once), so day-2 cost is zero.

### 3.12 Hygiene — *trivial*

- Fix `BLOCK_SIGS`: `"wurde blockiert"` ×3 byte-identical (issue 8) → dedupe
  the list (or use a `set`); the "≥ 3 repetitions" guard is currently "≥ 1".
- Delete dead code: `prompt1_assess`, `_P1_SYSTEM` (tags path superseded by
  the harness merge + Prompt2 tags).
- Ingest-time dedup of cross-feed syndication (same article URL/title from
  two feeds = two harness + Prompt2 spends); `scripts/dedup_embeddings.py`
  suggests this was prototyped — promote to ingest or drop.

---

## 4. What is good and should stay

- **The harness merge + schema-constrained calls** are the template:
  fast, typed, retry-free-by-construction. Every future LLM call should
  default to `format`-constrained, prose only where prose is the product
  (summaries, brief).
- **Title-first embeddings + member-anchor gate (max sim to any non-junk
  member ≥ 0.64, revive 0.62)** — this superseded centroid mode (junk
  exclusion alone wasn't enough; story 7 went from a 23-article mush to a
  4-member OpenAI core). No catch-alls, coherent large clusters; don't
  touch it without the golden-set gate.
- **Story quality = median of member qualities** — junk fetches can't
  poison a real story; the 174 junk-only stories at q2.0 confirm the
  separation works.
- **Heads-up brief** — grounded in the stored top stories, TTL-cached,
  failures not cached; the only LLM call where the failure text is visible
  to the user, and it's honest about it.
- **Calibration anchors in prompts** — the reason quality is bimodal
  *predictably* instead of uniformly middling.
