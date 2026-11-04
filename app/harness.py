"""Fast schema-constrained LLM classifiers ("harness") over the chat model.

Each call is a few-token JSON classification (native /api/chat, ``format``
schema, ``think: false``, temperature 0). Prompt wording is calibrated against
the benchmark in scripts/benchmark/ — change it only together with a re-run.
"""
from __future__ import annotations

from typing import Any

from . import config, content_quality, ollama

QUALITY_FULL = (
    "Rate the factual density, journalistic neutrality, and lack of "
    "clickbait/sensationalism of this article. Be discriminating: "
    "9-10 exceptional, deeply-sourced, precise reporting; "
    "7-8 solid journalism with specific, checkable facts; "
    "5-6 standard wire/news copy, accurate but unremarkable; "
    "3-4 thin, vague, or lightly-sourced; "
    "1-2 speculative, sensational, or junk. Do not default to high scores."
)
QUALITY_TITLE = (
    "You only have the article's headline (the body is unavailable, so judge "
    "the headline alone). Rate how substantive and factual the news event "
    "appears to be. 8-10 major, specific, factual news; 5-7 ordinary news "
    "with a clear subject; 1-3 vague, promotional, or trivial."
)
BREAKING_QUESTION = (
    "Does this article describe an immediate, breaking crisis or major "
    "global event requiring instant notification?"
)

EVENT_CLASSES = (
    "EXACT_MATCH",
    "CONTINUING_DEVELOPMENT",
    "SAME_TOPIC_DISTINCT_EVENT",
    "UNRELATED",
)
MERGE_CLASSES = frozenset({"EXACT_MATCH", "CONTINUING_DEVELOPMENT"})
# Story-vs-story "continuation" chained a whole saga into one 102-article story (2026-10-13).
STORY_MERGE_CLASSES = frozenset({"EXACT_MATCH"})
# Shared by the production verifier and the benchmark so the benchmark stays a
# faithful proxy. The CONTINUING_DEVELOPMENT clause is stated flatly on purpose:
# phrased as a condition, the 27B model read it as a rejection gate (recall -0.05).
CLASS_DEFINITIONS = (
    "EXACT_MATCH means the same incident, release, announcement, or "
    "decision. CONTINUING_DEVELOPMENT means a direct reaction, "
    "consequence, or timeline update to the same event. A follow-up that "
    "reports reactions, statements, or updates about the same event is a "
    "CONTINUING_DEVELOPMENT even when it names new people or "
    "organizations. SAME_TOPIC_DISTINCT_EVENT means the same entity or "
    "domain but a separate incident. UNRELATED means different subject "
    "matter."
)
_CLASSIFICATIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {"type": "string", "enum": list(EVENT_CLASSES)},
        },
    },
    "required": ["classifications"],
}


def _call(system: str, user: str, schema: dict[str, Any], *, model: str, max_tokens: int = 100,
          timeout: float | None = None) -> dict[str, Any]:
    result = ollama.structured(
        schema,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0,
        num_predict=max_tokens,
        timeout=timeout or config.HARNESS_TIMEOUT,
        model=model,
    )
    if not isinstance(result, dict):
        raise ValueError(f"harness expected an object, got {type(result).__name__}")
    return result


def _words(text: str, n: int) -> str:
    return " ".join((text or "").split()[:n])


# --- article assessment ------------------------------------------------------
def assess_article(title: str, content: str) -> dict[str, Any]:
    """Quality (1-10) + breaking flag in one call.

    Bodies that are not real article text (broken/thin/empty) are judged from
    the headline alone so a bot-blocked fetch can't poison the story quality.
    """
    full = content_quality.classify(content) == "ok"
    if full:
        context, criteria = f"Title: {title}\n\n{content[:12000]}", QUALITY_FULL
    else:
        context, criteria = f"Title: {title}", QUALITY_TITLE
    res = _call(
        "You are a strict news classifier. Answer both questions about the "
        "text and respond with the JSON object requested.",
        f"Context:\n{context}\n\n1) Quality: {criteria}\n2) {BREAKING_QUESTION}",
        {
            "type": "object",
            "properties": {
                "quality": {"type": "integer", "minimum": 1, "maximum": 10},
                "breaking": {"type": "boolean"},
            },
            "required": ["quality", "breaking"],
        },
        model=config.ASSESS_MODEL,
    )
    quality = max(1, min(10, int(res["quality"])))
    if not full:
        quality = min(quality, config.TITLE_QUALITY_MAX)
    return {"quality": quality, "breaking": bool(res["breaking"])}


# --- event verification (two-stage clustering) ------------------------------
def classify_event_candidates(new_title: str, new_text: str,
                              candidates: list[dict[str, Any]]) -> dict[int, str]:
    """Classify several candidate stories against one article in a single call.

    ``candidates``: ``[{"id", "headline", "summary"}]``. Returns ``{id: class}``
    for every candidate the model answered validly.
    """
    candidate_text = "\n\n".join(
        f"Candidate {c['id']}:\n"
        f"Headline: {(c.get('headline') or '')[:240]}\n"
        f"Summary: {(c.get('summary') or '')[:400]}"
        for c in candidates
    )
    prompt = (
        "Compare the new article with every candidate story. Classify each "
        "candidate independently, in the same order as presented. "
        f"{CLASS_DEFINITIONS} "
        "Return one decision for every candidate and no extra text.\n\n"
        f"New article title: {new_title}\n"
        f"New article text: {_words(new_text, 300)}\n\n"
        f"{candidate_text}"
    )
    result = _call(
        "You are a precise news event identity classifier. Use the four "
        "allowed classifications exactly as defined.",
        prompt,
        _CLASSIFICATIONS_SCHEMA,
        model=config.VERIFY_MODEL,
        max_tokens=400,
        timeout=config.CLUSTER_VERIFY_TIMEOUT_SECONDS,
    )
    classes = result.get("classifications")
    if not isinstance(classes, list):
        raise ValueError("event verifier returned non-list classifications")
    return {c["id"]: cls for c, cls in zip(candidates, classes) if cls in EVENT_CLASSES}


def _pairs_text(pairs: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"Pair {p['key']}:\n"
        f"New article title: {p['new_title']}\n"
        f"New article text: {_words(p.get('new_text', ''), 300)}\n"
        f"Candidate headline: {p['headline'][:240]}\n"
        f"Candidate summary: {p.get('summary', '')[:400]}"
        for p in pairs
    )


def classify_event_pairs(pairs: list[dict[str, Any]]) -> dict[str, str]:
    """Benchmark variant: classify independent (article, story) pairs in one call."""
    result = _call(
        "You are a precise news event identity classifier. Classify every "
        "comparison independently, in the same order as presented. "
        f"{CLASS_DEFINITIONS} "
        "EXACT_MATCH and CONTINUING_DEVELOPMENT are merges; the other two "
        "classifications are not merges. Return one classification for "
        "every pair and no extra text.",
        _pairs_text(pairs),
        _CLASSIFICATIONS_SCHEMA,
        model=config.VERIFY_MODEL,
        max_tokens=400,
        timeout=config.CLUSTER_VERIFY_TIMEOUT_SECONDS,
    )
    classes = result.get("classifications") or []
    return {p["key"]: cls for p, cls in zip(pairs, classes) if cls in EVENT_CLASSES}


def analyze_event_pairs(pairs: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Benchmark error analysis: keyed decisions (``reasoning`` kept empty for tooling)."""
    schema = {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pair_key": {"type": "string"},
                        "classification": {"type": "string", "enum": list(EVENT_CLASSES)},
                    },
                    "required": ["pair_key", "classification"],
                },
            },
        },
        "required": ["decisions"],
    }
    result = _call(
        "You are analyzing event-verifier errors. Classify every comparison "
        "independently and return no extra text.",
        _pairs_text(pairs),
        schema,
        model=config.VERIFY_MODEL,
        max_tokens=60,
        timeout=config.CLUSTER_VERIFY_TIMEOUT_SECONDS,
    )
    return {
        str(item["pair_key"]): {"classification": item["classification"], "reasoning": ""}
        for item in result.get("decisions") or []
        if isinstance(item, dict)
        and item.get("classification") in EVENT_CLASSES
        and item.get("pair_key") is not None
    }
