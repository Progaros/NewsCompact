"""Article body cleanup + classification (deterministic, no LLM).

``clean_body`` normalizes what ingest stores:
- upvoterss (Reddit / Hacker News) descriptions are "Reading time | Post
  permalink <post text> Top comments <c1> Comment permalink <c2> …". They are
  rewritten to the post text plus a "Top comments:" list (first
  SOCIAL_MAX_COMMENTS). The raw permalink chrome used to classify ~95 % of
  these items as ``broken`` although they carried the full post / linked article.
- binary payloads (images fetched as pages) are dropped.

``classify`` → ``ok`` / ``thin`` / ``broken`` / ``empty``. Non-``ok`` bodies are
embedded and quality-scored from the headline only; ``broken`` ones are skipped
by the pipeline.
"""
from __future__ import annotations

import re

from . import config

MAX_BODY_CHARS = 100_000
MAX_COMMENT_CHARS = 1_000
COMMENTS_HEADER = "Top comments:"

# Signals of a non-article page. Only a page that opens with one is checked further, so an
# article that merely mentions "captcha" mid-body is not flagged; it is broken when the
# signals then appear >= 3 times anywhere in the body (social feed / bot wall, not one chrome line).
BLOCK_SIGS = (
    "post permalink",
    "top comments",
    "comment permalink",
    "m.youtube",
    "wurde blockiert",
    "access denied",
    "unusual traffic",
    "captcha",
    "enable javascript",
    "javascript seems disabled",
    "please enable cookies",
    "subscribe for free",
    "one small step",
    "this content is only available",
)
_HEAD_WINDOW = 400
_THIN_CHARS = 250

_SOCIAL_LEAD = re.compile(r"^\s*(?:Reading time:\s*\d+\s*min\s*\|\s*)?Post permalink\s*", re.I)
_SOCIAL_COMMENTS = re.compile(r"\s*\bTop comments\b", re.I)
_COMMENT_END = re.compile(r"\s*\bComment permalink\b\s*", re.I)
_HEADER_SPLIT = "\n\n" + COMMENTS_HEADER + "\n"


def is_binary(text: str) -> bool:
    head = text[:200]
    return sum(ch < " " and ch not in "\n\r\t" or ch in "\x7f\ufffd" for ch in head) > 10


def with_comments(post: str, comments: list[str]) -> str:
    if not comments:
        return post
    listing = COMMENTS_HEADER + "\n" + "\n".join(f"- {c}" for c in comments)
    return f"{post}\n\n{listing}" if post else listing


def split_post(text: str | None) -> tuple[str, str]:
    """``(post, comments_section)`` of a cleaned body; comments may be ""."""
    t = text or ""
    if t.startswith(COMMENTS_HEADER + "\n"):
        return "", t
    post, sep, rest = t.partition(_HEADER_SPLIT)
    return post, (COMMENTS_HEADER + "\n" + rest) if sep else ""


def clean_body(text: str | None) -> str:
    t = (text or "").strip()
    if not t or is_binary(t):
        return ""
    if _SOCIAL_LEAD.match(t):
        parts = _SOCIAL_COMMENTS.split(_SOCIAL_LEAD.sub("", t, count=1), maxsplit=1)
        thread = parts[1] if len(parts) > 1 else ""
        comments = [c.strip()[:MAX_COMMENT_CHARS] for c in _COMMENT_END.split(thread) if c.strip()]
        t = with_comments(parts[0].strip(), comments[: config.SOCIAL_MAX_COMMENTS])
    return t[:MAX_BODY_CHARS]


def classify(text: str | None) -> str:
    t = (text or "").strip()
    if not t:
        return "empty"
    if is_binary(t):
        return "broken"
    head = t[:_HEAD_WINDOW].lower()
    # One chrome line before real text is fine; chrome repeated across the page is not.
    if any(sig in head for sig in BLOCK_SIGS):
        low = t.lower()
        if sum(low.count(sig) for sig in BLOCK_SIGS) >= 3:
            return "broken"
    if len(t) < _THIN_CHARS:
        return "thin"
    return "ok"
