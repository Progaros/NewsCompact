"""Classify a stored article body so junk fetches don't poison the quality score.

~70% of stored ``content_raw`` is NOT the article: it's the RSS description for
feeds whose description is social/bot-block chrome ("Post permalink / Top
comments / m.youtube", German "Ihre Anfrage wurde blockiert", JS-required
paywalls, ...). Scoring that chrome as journalism gives quality 1-2 for a story
whose *headline* is perfectly substantive — the root cause behind the ICC
sanctions story sitting at quality 2.

The classifier is a cheap deterministic gate (no LLM): it only looks at the
first ~400 chars, where bot-block/social pages announce themselves, so a legit
article that merely *mentions* "captcha" mid-body is not flagged.
"""
from __future__ import annotations

# Strong signals of a non-article page. Checked only in the opening window.
BLOCK_SIGS = [
    "post permalink",           # social feed chrome
    "top comments",             # social feed chrome
    "m.youtube",                # social video embed
    "wurde blockiert",          # bot-block (German: "was blocked")
    "wurde blockiert",          # bot-block (alt. spelling)
    "wurde blockiert",          # bot-block
    "access denied",
    "unusual traffic",
    "captcha",
    "enable javascript",
    "javascript seems disabled",
    "please enable cookies",
    "subscribe for free",
    "comment permalink",      # social feed chrome (repeats per comment)
    "one small step",           # NYT paywall phrase
    "this content is only available",
]
_HEAD_WINDOW = 400
_THIN_CHARS = 250


def classify(text: str | None) -> str:
    """Return one of: 'ok' | 'thin' | 'junk' | 'empty'.

    - 'junk'  : the body is chrome, not the article (bot-block / social / paywall,
                 or binary garbage such as raw image bytes stored as text).
    - 'thin'  : too short to be a real body (a blurb/lead).
    - 'empty' : no text at all.
    - 'ok'    : looks like real article text.
    """
    t = (text or "").strip()
    if not t:
        return "empty"
    # Binary garbage (raw image bytes etc.) announces itself with control
    # chars; a few NULs/newlines are legit text, >10 per 200 chars is not.
    h = t[:200]
    if sum(ch < " " or ch == "\x7f" for ch in h) > 10:
        return "junk"
    head = t[:_HEAD_WINDOW].lower()
    if any(sig in head for sig in BLOCK_SIGS):
        # Chrome signals repeated across the whole page ("Comment
        # permalink" xN) = social feed page. An article that merely opens
        # with one chrome line ("Reading time | Post permalink") followed
        # by real text is not — classify it by length below.
        if sum(t.lower().count(sig) for sig in BLOCK_SIGS) >= 3:
            return "junk"
    if len(t) < _THIN_CHARS:
        return "thin"
    return "ok"
