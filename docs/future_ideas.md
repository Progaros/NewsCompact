## Do soon
- follow a story or ask ai for notification if xyz happens? (refine idea!)

- what to do with generated / downloaded data long term? (maybe its possible to verify / enrich new articles with the existing data?)

- many ui improvements!

- security review!


## Maybe at some point
- multiple users share "expensive" ai summaries and classifications, but have seperate likes and scores

## From the 2026-10-13 review (not scheduled yet)
- **Fix clustering from the UI**: "doesn't belong" per source article and "merge with…" per
  story; every click repairs the story and stores a labeled pair (real benchmark data).
- **Related stories** instead of merges: keep the verifier's `SAME_TOPIC_DISTINCT_EVENT`
  links (~240/day, currently discarded) and show "Related: …" on cards.
- **Lazy Prompt 2**: single-article, low-quality stories keep the article title until they get
  a second source or pass the quality cut (est. 30–40 % less GPU).
- **"New since last visit"** marker; implicit feedback (source/article clicks, chat) as weak 👍;
  decay for tag weights.
- **Near-duplicate wire copies** (embedding sim ≥ ~0.93, same day) skip the AI like title dupes.
- **Ops**: auto-disable feeds after N consecutive failures, nightly `sqlite3 .backup`,
  Ollama/queue health in the UI, benchmark re-run on prompt/threshold changes.
- **UI**: thumbnails (needs og:image at ingest — only 30/2203 stories have one), hero +
  sections, story detail page with timeline, light theme, keyboard shortcuts, skeleton
  loading / infinite scroll.
