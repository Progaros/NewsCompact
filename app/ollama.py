"""Minimal Ollama HTTP client (chat via OpenAI-compat /v1, embeddings via /api/embed).

Sync client (httpx) so it can be called from the worker thread and from FastAPI
sync endpoints (which run in a threadpool). All models live on one local server.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

import httpx

from . import config

_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=config.OLLAMA_BASE_URL, timeout=300.0)
    return _client


# --- Embeddings ------------------------------------------------------------
def embed_texts(texts: Iterable[str]) -> list[list[float]]:
    """Embed a batch of strings. Returns one vector per input, in order."""
    texts = [t for t in texts]
    if not texts:
        return []
    r = _http().post(
        "/api/embed",
        json={"model": config.EMBED_MODEL, "input": texts, "options": {"embedding": True}},
    )
    r.raise_for_status()
    data = r.json()
    out = data.get("embeddings", [])
    if len(out) != len(texts):
        raise RuntimeError(f"embed count mismatch: got {len(out)} for {len(texts)} inputs")
    return out


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]


# --- Chat (OpenAI-compatible) ---------------------------------------------
def chat(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str:
    """Return the assistant text for a chat completion."""
    payload: dict[str, Any] = {
        "model": config.CHAT_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = _http().post("/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as e:
            last_exc = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _extract_json(text: str) -> Any:
    """Lenient JSON extraction: strip code fences, find the first JSON blob."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost object/array.
    for start, end in (("{", "}"), ("[", "]")):
        i, j = text.find(start), text.rfind(end)
        if i != -1 and j > i:
            try:
                return json.loads(text[i : j + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Could not parse JSON from model output: {text[:300]!r}")


def chat_json(messages: list[dict[str, Any]], **kwargs) -> Any:
    """Chat, expecting a JSON object/array back. Returns the parsed value."""
    raw = chat(messages, **kwargs)
    return _extract_json(raw)
