"""Tag normalization.

The LLM invents tag spellings freely (``AI`` / ``ai`` / ``A.I.``,
``machine-learning`` / ``machine learning``, ...), which splits user
interest weight across variants — a 👍 on one spelling never affected the
other. All tags are funneled through :func:`normalize_tag` at storage time
(pipeline) and at read time (scoring), so matching always agrees.
"""
from __future__ import annotations

import re

# Conservative alias table: only well-known synonyms. Keys must already be
# normalized (lowercase, single spaces).
TAG_ALIASES = {
    "artificial intelligence": "ai",
    "a i": "ai",
    "gen ai": "ai",
    "generative ai": "ai",
    "genai": "ai",
    "large language model": "llm",
    "large language models": "llm",
    "llms": "llm",
    "ml": "machine learning",
    "mlp": "mlp",
    "usa": "us",
    "u s": "us",
    "uk": "uk",
    "eu": "eu",
    "eur union": "eu",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Common Latin diacritics → ASCII, mapped *before* the non-alnum strip so
# accented tags keep their letters: "Identitätengesetz" → "identitaengesetz"
# (one tag) instead of "identita tengesetz" (two tags). ß/æ map to two
# chars, so the table is built by hand rather than with str.maketrans.
_DIACRITICS = {
    ord(k): v
    for k, v in {
        "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "à": "a", "ù": "u", "ç": "c", "ñ": "n",
        "å": "a", "ø": "o", "æ": "ae",
    }.items()
}


def normalize_tag(tag: str) -> str:
    """Lowercase, collapse punctuation/whitespace, apply alias table.

    Returns "" when nothing survives (e.g. a bare punctuation tag)."""
    t = str(tag).strip().lower()
    t = t.translate(_DIACRITICS)
    t = _NON_ALNUM.sub(" ", t).strip()
    t = " ".join(t.split())
    if t in TAG_ALIASES:
        return TAG_ALIASES[t]
    return t


def normalize_tags(tags: list[str]) -> list[str]:
    """Normalize, dedupe (preserving order), and drop empty tags."""
    seen: list[str] = []
    for t in tags:
        n = normalize_tag(t)
        if n and n not in seen:
            seen.append(n)
    return seen
