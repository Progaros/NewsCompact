"""Tag normalization.

The LLM invents tag spellings freely (``AI`` / ``ai`` / ``A.I.``,
``machine-learning`` / ``machine learning``, ``KI`` / ``AI``), which splits user
interest weight across variants — a 👍 on one spelling never affected the
other. All tags are funneled through :func:`normalize_tag` at storage time
(pipeline) and at read time (scoring), so matching always agrees.

Two alias sources: the static table below (unambiguous synonyms and EN/DE
translations) and approved ``TagAlias`` rows (scripts/merge_tags.py: embedding
candidates verified by the LLM), loaded with :func:`load_aliases`.
"""
from __future__ import annotations

import re
import sqlite3
import threading

# Conservative alias table: only well-known synonyms. Keys must already be
# normalized (lowercase, single spaces, no diacritics).
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
    "usa": "us",
    "u s": "us",
    "united states": "us",
    "united kingdom": "uk",
    "european union": "eu",
    # German → English (one meaning only; "bayern" is also the football club, "sicherheit" also safety)
    "ki": "ai",
    "kunstliche intelligenz": "ai",
    "munchen": "munich",
    "deutschland": "germany",
    "osterreich": "austria",
    "schweiz": "switzerland",
    "frankreich": "france",
    "russland": "russia",
    "europaische union": "eu",
    "politik": "politics",
    "wirtschaft": "economy",
    "gesundheit": "health",
    "finanzen": "finance",
    "klimawandel": "climate change",
    "bildung": "education",
    "forschung": "research",
    "wissenschaft": "science",
    "technologie": "technology",
    "wahlen": "elections",
    "nahverkehr": "public transport",
    "cybersicherheit": "cybersecurity",
    "it sicherheit": "cybersecurity",
}

_db_aliases: dict[str, str] = {}
_db_lock = threading.Lock()

# Tags with several common meanings never merge with anything ("bayern" = the state or the
# football club, "sicherheit" = security or safety, "netzwerk" = social or computer network).
AMBIGUOUS_TAGS = frozenset({"bayern", "sicherheit", "entwicklung", "beziehung", "beziehungen", "netzwerk", "storage"})

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


def _clean(tag: str) -> str:
    t = str(tag).strip().lower()
    t = t.translate(_DIACRITICS)
    t = _NON_ALNUM.sub(" ", t).strip()
    return " ".join(t.split())


def base_tag(tag: str) -> str:
    """Normalized with the static aliases only (the key TagAlias rows are stored under)."""
    t = _clean(tag)
    return TAG_ALIASES.get(t, t)


def normalize_tag(tag: str) -> str:
    """Lowercase, collapse punctuation/whitespace, apply the alias tables.

    Returns "" when nothing survives (e.g. a bare punctuation tag)."""
    t = base_tag(tag)
    return _db_aliases.get(t, t)


def normalize_tags(tags: list[str]) -> list[str]:
    """Normalize, dedupe (preserving order), and drop empty tags."""
    seen: list[str] = []
    for t in tags:
        n = normalize_tag(t)
        if n and n not in seen:
            seen.append(n)
    return seen


def set_aliases(pairs: dict[str, str]) -> None:
    """Install DB aliases (``alias -> canonical``, both normalized); chains resolve, cycles drop,
    pairs with an ambiguous tag are ignored."""
    base = {base_tag(a): base_tag(c) for a, c in pairs.items()}
    base = {a: c for a, c in base.items() if a not in AMBIGUOUS_TAGS and c not in AMBIGUOUS_TAGS}
    out = {}
    for alias in base:
        seen, target = {alias}, base[alias]
        while target in base and target not in seen:
            seen.add(target)
            target = base[target]
        if target not in seen and target and alias:
            out[alias] = target
    global _db_aliases
    with _db_lock:
        _db_aliases = out


def load_aliases(conn: sqlite3.Connection) -> None:
    """Reload approved TagAlias rows (cheap; called before ranking and each pipeline batch)."""
    set_aliases({r[0]: r[1] for r in conn.execute("SELECT alias, canonical FROM TagAlias WHERE status = 'approved'")})


def merge_weights(weights: dict[str, float]) -> dict[str, float]:
    """Canonical keys; weights of merged aliases keep the strongest value instead of adding up."""
    out: dict[str, float] = {}
    for tag, w in weights.items():
        key = normalize_tag(tag)
        try:
            w = max(-10.0, min(10.0, float(w)))
        except (TypeError, ValueError):
            continue
        if key and (key not in out or abs(w) > abs(out[key])):
            out[key] = w
    return out
