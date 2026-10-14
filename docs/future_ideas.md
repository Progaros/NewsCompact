# Future Ideas

> All 14 `FIX NOW` items from 2026-10-14 are implemented — see [`plan.md`](./plan.md#changelog),
> as are the quality-neutral review items (related stories, per-user home, batch embeddings, …).
> Actionable next steps are tracked in [`todo.md`](./todo.md).

## Needs refinement
- **Follow a story / AI alert**: follow a story or ask the AI for an ntfy notification when a specific condition or development happens.
- **Long-term use of downloaded / generated data**: use existing stories and articles to verify or enrich incoming articles before or during Prompt 2.
- **Mobile UI for score breakdowns**: make tooltips (`rel 7.55 · q 7 · cred 10 · impact 5 · interest 10 · dist 0`) accessible on touch screens (tap-to-toggle or inline legend/help).

## From the 2026-10-13 & 2026-10-14 reviews (not scheduled yet)
- **Fix clustering from the UI**: "doesn't belong" per source article and "merge with…" per
  story; every click repairs the story and stores a labeled pair (real benchmark data).
- **Personalization refinements**: "New since last visit" marker; implicit feedback (source/article clicks, chat) as weak 👍.
- **Ops**: auto-disable feeds after N consecutive failures, nightly `sqlite3 .backup`,
  Ollama/queue health indicator in the UI, benchmark re-run on prompt/threshold changes.
- **UI**: thumbnails (`og:image` extraction during `fetch_page_text` — only 41/7,556 articles have an enclosure image), hero +
  sections, story detail page with timeline, light theme, keyboard shortcuts, skeleton
  loading / infinite scroll.

## Deferred: would trade data or quality for speed (decided 2026-10-14)
Only revisit with measurements showing nothing relevant is lost.
- **Lazy Prompt 2** for single-article stories with `quality <= 3` (~28 % of Prompt 2 GPU time):
  such stories would show raw titles and could hide relevant single-source news.
- **Freshness window on pending articles** (e.g. 48 h): re-enabled feeds would lose their backlog.
- **Near-duplicate verifier bypass** (sim ≥ 0.93) and **anchor cap** on 60–100+ article stories:
  both change clustering decisions.
- **Harness batching** (several articles per call): changes quality scores.
- **Feedback normalization by $1/\sqrt{\text{tags}}$ + tag-weight decay**: changes the ranking of
  existing profiles; needs an A/B on real votes first.
- **Per-account login lockout** (lets anyone lock a user out) and **share-link auto-expiry**
  (breaks links people rely on; links can now be revoked instead).
