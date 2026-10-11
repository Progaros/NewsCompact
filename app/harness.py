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
          max_tokens: int = 100, timeout: float | None = None) -> dict[str, Any]:
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
    r = _http().post("/api/chat", json=payload, timeout=timeout)
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

    # --- event verification -----------------------------------------------
    EVENT_CLASSES = (
        "EXACT_MATCH",
        "CONTINUING_DEVELOPMENT",
        "SAME_TOPIC_DISTINCT_EVENT",
        "UNRELATED",
    )
    # Identical class definitions for the production candidate verifier and the
    # benchmark pair verifier, so the benchmark stays a faithful proxy for
    # production routing. The CONTINUING_DEVELOPMENT clause is unconditional
    # about new entities: the literal 27B model read "ONLY IF ... reacting" as
    # a rejection gate and recall dropped (0.709 -> 0.655), so the acceptance
    # is stated flatly instead of as a condition.
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

    def classify_event_candidates(
        self, new_title: str, new_text: str,
        candidates: list[dict[str, Any]],
    ) -> dict[int, str]:
        """Classify several story candidates in one constrained LLM call."""
        candidate_text = "\n\n".join(
            f"Candidate {candidate['id']}:\n"
            f"Headline: {(candidate.get('headline') or '')[:240]}\n"
            f"Summary: {(candidate.get('summary') or '')[:400]}"
            for candidate in candidates
        )
        schema = {
            "type": "object",
            "properties": {
                "classifications": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(self.EVENT_CLASSES)},
                },
            },
            "required": ["classifications"],
        }
        prompt = (
            "Compare the new article with every candidate story. Classify each "
            "candidate independently, in the same order as presented. "
            f"{self.CLASS_DEFINITIONS} "
            "Return one decision for every candidate and no extra text.\n\n"
            f"New article title: {new_title}\n"
            f"New article text: {' '.join(new_text.split()[:300])}\n\n"
            f"{candidate_text}"
        )
        result = _call(
            "You are a precise news event identity classifier. Use the four "
            "allowed classifications exactly as defined.",
            prompt,
            schema,
            max_tokens=400,
            timeout=config.CLUSTER_VERIFY_TIMEOUT_SECONDS,
        )
        classifications = result.get("classifications", [])
        if not isinstance(classifications, list):
            raise ValueError("event verifier returned non-list classifications")
        allowed = set(self.EVENT_CLASSES)
        return {
            candidate["id"]: classification
            for candidate, classification in zip(candidates, classifications)
            if classification in allowed
        }

    def classify_event_pairs(self, pairs: list[dict[str, Any]]) -> dict[str, str]:
        """Classify independent benchmark pairs in one bounded request."""
        comparisons = "\n\n".join(
            f"Pair {pair['key']}:\n"
            f"New article title: {pair['new_title']}\n"
            f"New article text: {' '.join(pair.get('new_text', '').split()[:300])}\n"
            f"Candidate headline: {pair['headline'][:240]}\n"
            f"Candidate summary: {pair.get('summary', '')[:400]}"
            for pair in pairs
        )
        schema = {
            "type": "object",
            "properties": {
                "classifications": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(self.EVENT_CLASSES)},
                },
            },
            "required": ["classifications"],
        }
        result = _call(
            "You are a precise news event identity classifier. Classify every "
            "comparison independently, in the same order as presented. "
            f"{self.CLASS_DEFINITIONS} "
            "EXACT_MATCH and CONTINUING_DEVELOPMENT are merges; the other two "
            "classifications are not merges. Return one classification for "
            "every pair and no extra text.",
            comparisons,
            schema,
            max_tokens=400,
            timeout=config.CLUSTER_VERIFY_TIMEOUT_SECONDS,
        )
        classifications = result.get("classifications", [])
        allowed = set(self.EVENT_CLASSES)
        return {
            pair["key"]: classification
            for pair, classification in zip(pairs, classifications)
            if classification in allowed
        }

    def analyze_event_pairs(self, pairs: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        """Classify benchmark pairs (zero-reasoning) and retain the decision.

        The reasoning property was removed to starve the 27B model of output
        tokens: a single-pair reasoning pass exceeded the 60 s verifier budget.
        The returned ``reasoning`` key is kept (empty) so review tooling that
        reads it does not break.
        """
        comparisons = "\n\n".join(
            f"Pair {pair['key']}:\n"
            f"New article title: {pair['new_title']}\n"
            f"New article text: {' '.join(pair.get('new_text', '').split()[:300])}\n"
            f"Candidate headline: {pair['headline'][:240]}\n"
            f"Candidate summary: {pair.get('summary', '')[:400]}"
            for pair in pairs
        )
        schema = {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pair_key": {"type": "string"},
                            "classification": {"type": "string", "enum": list(self.EVENT_CLASSES)},
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
            comparisons,
            schema,
            max_tokens=60,
            timeout=config.CLUSTER_VERIFY_TIMEOUT_SECONDS,
        )
        decisions = result.get("decisions", [])
        allowed = set(self.EVENT_CLASSES)
        return {
            str(item["pair_key"]): {
                "classification": item["classification"],
                "reasoning": str(item.get("reasoning", ""))[:500],
            }
            for item in decisions
            if isinstance(item, dict)
            and item.get("classification") in allowed
            and item.get("pair_key") is not None
        }

    # --- compatibility convenience --------------------------------------
    def verify_event_match(self, new_title: str, new_text: str,
                           story_headline: str, story_summary: str = "") -> bool:
        """Stage-2 of two-stage clustering: is this article the *same specific
        event* as the candidate story? Returns True only for an exact
        incident/announcement match, NOT a shared topic/company/trend.

        Same mechanics as the quality calls (native /api/chat, think:false,
        temperature 0, schema-constrained) so each check is a few-token
        classification. This compatibility method intentionally returns only
        the merge decision and no reasoning.
        """
        result = self.classify_event_candidates(
            new_title,
            new_text,
            [{"id": 1, "headline": story_headline, "summary": story_summary}],
        )
        return result.get(1) in {"EXACT_MATCH", "CONTINUING_DEVELOPMENT"}

    def article_quality(self, title: str, content: str, summary: str = "") -> tuple[int, str]:
        """One constrained quality call. Returns (quality, content_class).

        When the stored body is broken/thin/empty (bot-block, social chrome, a
        thin blurb) the score is derived from the headline (+ optional summary)
        instead, so a bot-blocked fetch doesn't poison a story's quality.
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
        """Per-article quality + breaking check — ONE constrained call.

        Merged from two calls (quality + breaking): the breaking check re-sent
        the same context, so the second prefill cost ~half the harness time.
        One schema call returns both fields; latency roughly halved
        (2026-10-11, backlog-drain speedup).
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
        res = _call(
            "You are a strict news classifier. Answer both questions about the "
            "text and respond with the JSON object requested.",
            f"Context:\n{context}\n\n"
            "1) Quality: " + criteria + "\n"
            "2) " + BREAKING_QUESTION,
            {
                "type": "object",
                "properties": {
                    "quality": {"type": "integer", "minimum": 1, "maximum": 10},
                    "breaking": {"type": "boolean"},
                },
                "required": ["quality", "breaking"],
            },
        )
        return {
            "quality": int(res["quality"]),
            "breaking": bool(res["breaking"]),
            "content_class": cls,
        }


# Module-level instance (stateless; shares one httpx client).
harness = LocalJevHarness()
