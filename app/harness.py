"""Local "System One" harness: fast, schema-constrained classification.

Emulates the Jev-style primitives (Choice / Score / Boolean) on top of the
local Ollama chat model, so no separate classifier is ever loaded into VRAM.

Key mechanics (verified against our Qwen3 chat model):
- Uses the NATIVE /api/chat endpoint, because the OpenAI-compat /v1 endpoint
  does not accept the `format` schema.
- `"think": false` is REQUIRED. On Qwen3 thinking models the thinking tokens
  consume the num_predict budget and `format` appears to be ignored
  (content comes back empty, done_reason="length").
- `format` + `temperature=0` + a small `num_predict` cap makes each call a
  few-JSON-token classifier: typically 5–20 eval tokens, well under a second
  of generation.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from . import config, content_quality

_client: httpx.Client | None = None

# --- quality criteria (calibrated) ----------------------------------------
# Calibration anchors keep the score spread across 1-10 instead of top-heavy:
# reserve 9-10 for exceptional, 7-8 for solid, 5-6 for ordinary, 3-4 thin,
# 1-2 speculative/junk. The title-based variant is used when the stored body is
# bot-block/social chrome, so a junk FETCH no longer drags a real story down.
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


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=config.OLLAMA_BASE_URL, timeout=120.0)
    return _client


def _call(system_prompt: str, user_text: str, schema: dict[str, Any],
          max_tokens: int = 100) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "format": schema,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": max_tokens},
    }
    r = _http().post("/api/chat", json=payload)
    r.raise_for_status()
    content = r.json()["message"]["content"]
    return json.loads(content)


class LocalJevHarness:
    """Choice / Score / Boolean primitives over the local Qwen chat model."""

    def choice(self, context: str, question: str, options: list[str]) -> str:
        """Pick exactly one option from a fixed list."""
        res = _call(
            "You are a precise routing system. You must select exactly one "
            "option from the provided list.",
            f"Context:\n{context}\n\nQuestion: {question}\nOptions: {options}",
            {
                "type": "object",
                "properties": {"selected_option": {"type": "string", "enum": options}},
                "required": ["selected_option"],
            },
        )
        return res["selected_option"]

    def score(self, context: str, criteria: str,
              min_score: int = 1, max_score: int = 10) -> int:
        """Integer score within [min_score, max_score]."""
        res = _call(
            "You are a strict scoring engine. Evaluate the text based on the "
            f"criteria and assign an integer score between {min_score} and {max_score}.",
            f"Context:\n{context}\n\nCriteria: {criteria}",
            {
                "type": "object",
                "properties": {
                    "score": {"type": "integer", "minimum": min_score, "maximum": max_score}
                },
                "required": ["score"],
            },
        )
        return int(res["score"])

    def boolean(self, context: str, question: str) -> bool:
        """Binary true/false classification."""
        res = _call(
            "You are a binary classification engine. Answer the question about "
            "the context with only true or false.",
            f"Context:\n{context}\n\nQuestion: {question}",
            {
                "type": "object",
                "properties": {"is_true": {"type": "boolean"}},
                "required": ["is_true"],
            },
        )
        return bool(res["is_true"])

    # --- convenience -------------------------------------------------------
    def article_quality(self, title: str, content: str, summary: str = "") -> tuple[int, str]:
        """One constrained quality call. Returns (quality, content_class).

        When the stored body is junk/thin/empty (bot-block, social chrome, a
        thin blurb) the score is derived from the headline (+ optional summary)
        instead, so a bot-blocked fetch doesn't poison a real story's quality.
        """
        cls = content_quality.classify(content)
        if cls == "ok":
            context = f"Title: {title}\n\n{content[:12000]}"
            criteria = QUALITY_FULL
        else:
            context = f"Title: {title}"
            if summary and summary.strip():
                context += f"\n\nSummary: {summary[:600]}"
            criteria = QUALITY_TITLE
        return self.score(context, criteria), cls

    def assess_article(self, title: str, content: str, summary: str = "") -> dict[str, Any]:
        """Per-article quality + breaking check (2 cheap constrained calls)."""
        quality, cls = self.article_quality(title, content, summary)
        breaking = self.boolean(
            f"Title: {title}\n\n{content[:12000]}", BREAKING_QUESTION
        )
        return {"quality": quality, "breaking": breaking, "content_class": cls}


# Module-level instance (stateless; shares one httpx client).
harness = LocalJevHarness()
