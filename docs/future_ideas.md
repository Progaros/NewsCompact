# Future Ideas

> All 14 `FIX NOW` items from 2026-10-14 are implemented — see [`plan.md`](./plan.md#changelog).
> Actionable next steps are tracked in [`todo.md`](./todo.md).

## Needs refinement
- **Follow a story / AI alert**: follow a story or ask the AI for an ntfy notification when a specific condition or development happens.
- **Long-term use of downloaded / generated data**: use existing stories and articles to verify or enrich incoming articles before or during Prompt 2.
- **Mobile UI for score breakdowns**: make tooltips (`rel 7.55 · q 7 · cred 10 · impact 5 · interest 10 · dist 0`) accessible on touch screens (tap-to-toggle or inline legend/help).

## From the 2026-10-13 & 2026-10-14 reviews (not scheduled yet)
- **Lazy Prompt 2**: single-article stories with `quality <= 3` (44% of single-article stories, ~60 min of GPU time in `news.db`) keep the article title + opening sentences until a second source joins.
- **Fix clustering from the UI**: "doesn't belong" per source article and "merge with…" per
  story; every click repairs the story and stores a labeled pair (real benchmark data).
- **Related stories** (`StoryLink` table): persist the verifier's `SAME_TOPIC_DISTINCT_EVENT`
  and `UNRELATED` links (~240/day, currently discarded) to avoid re-verifying pairs on minor updates and show "Related: …" on cards.
- **Personalization refinements**: normalize feedback step by $1/\sqrt{\text{tags}}$ so 6-tag stories don't get 6× the boost of 1-tag stories; exponential decay for `tag_weights`; per-user `city`/`country`/`language` in `UserProfile.preferences`; "New since last visit" marker; implicit feedback (source/article clicks, chat) as weak 👍.
- **Near-duplicate wire copies** (embedding sim ≥ ~0.93, same day) skip the AI like title dupes.
- **Ops**: auto-disable feeds after N consecutive failures, nightly `sqlite3 .backup`,
  Ollama/queue health indicator in the UI, benchmark re-run on prompt/threshold changes.
- **UI**: thumbnails (`og:image` extraction during `fetch_page_text` — only 41/7,556 articles have an enclosure image), hero +
  sections, story detail page with timeline, light theme, keyboard shortcuts, skeleton
  loading / infinite scroll.
