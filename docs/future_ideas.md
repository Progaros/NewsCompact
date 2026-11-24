# Future Ideas


## Needs refinement
- **Follow a story / AI alert**: follow a story or ask the AI for an ntfy notification when a specific condition or development happens.
- **Long-term use of downloaded / generated data**: use existing stories and articles to verify or enrich incoming articles before or during Prompt 2.
- **Mobile UI for score breakdowns**: make tooltips (`rel 7.55 · q 7 · cred 10 · impact 5 · interest 10 · dist 0`) accessible on touch screens (tap-to-toggle or inline legend/help).


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
