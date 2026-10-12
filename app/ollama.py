"""Ollama HTTP client (one shared pooled connection).

- embeddings: native /api/embed
- free-form chat: OpenAI-compatible /v1/chat/completions (with retries)
- schema-constrained chat: native /api/chat with ``format`` + ``think: false``
  (on Qwen3 thinking models, thinking tokens otherwise eat num_predict and
  ``format`` is silently ignored).
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Iterator

import httpx

from . import config

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            # trust_env=False: Ollama is a LAN host; an http(s)_proxy env var must not reroute it.
            _client = httpx.Client(base_url=config.OLLAMA_BASE_URL, timeout=config.OLLAMA_TIMEOUT,
                                   trust_env=False)
        return _client


def _keep_alive(payload: dict[str, Any]) -> dict[str, Any]:
    if config.OLLAMA_KEEP_ALIVE:
        payload["keep_alive"] = config.OLLAMA_KEEP_ALIVE
    return payload


# Errors meaning "Ollama is unreachable" (as opposed to a bad/slow answer).
UNAVAILABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


def available() -> bool:
    try:
        _http().get("/api/version", timeout=5).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


# --- embeddings ------------------------------------------------------------
def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    r = _http().post("/api/embed", json=_keep_alive({"model": config.EMBED_MODEL, "input": texts}))
    r.raise_for_status()
    out = r.json().get("embeddings", [])
    if len(out) != len(texts):
        raise RuntimeError(f"embed count mismatch: got {len(out)} for {len(texts)} inputs")
    return out


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]


# --- free-form chat --------------------------------------------------------
def chat(messages: list[dict[str, Any]], *, temperature: float = 0.2,
         max_tokens: int | None = None) -> str:
    payload: dict[str, Any] = {
        "model": config.CHAT_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    for attempt in range(3):
        try:
            r = _http().post("/v1/chat/completions", json=payload)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def chat_stream(messages: list[dict[str, Any]], *, temperature: float = 0.2,
                max_tokens: int | None = None) -> Iterator[str]:
    """Like :func:`chat`, but yields answer text chunks as the model produces them."""
    payload: dict[str, Any] = {
        "model": config.CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    with _http().stream("POST", "/v1/chat/completions", json=payload) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            choices = json.loads(data).get("choices") or [{}]
            text = (choices[0].get("delta") or {}).get("content")
            if text:
                yield text


def extract_json(text: str) -> Any:
    """Lenient JSON extraction: strip code fences, else take the outermost object/array."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for start, end in (("{", "}"), ("[", "]")):
        i, j = text.find(start), text.rfind(end)
        if i != -1 and j > i:
            try:
                return json.loads(text[i : j + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"could not parse JSON from model output: {text[:300]!r}")


def chat_json(messages: list[dict[str, Any]], **kwargs: Any) -> Any:
    return extract_json(chat(messages, **kwargs))


# --- schema-constrained chat ----------------------------------------------
def structured(schema: dict[str, Any], messages: list[dict[str, Any]], *,
               temperature: float = 0.0, num_predict: int = 1024,
               timeout: float | None = None, prose_fallback: bool = False) -> Any:
    """Return the parsed JSON the model emitted under ``schema``.

    With ``prose_fallback`` an empty constrained reply is retried as a plain
    /v1 chat and parsed leniently (the known ``think`` interaction).
    """
    payload = _keep_alive({
        "model": config.CHAT_MODEL,
        "messages": messages,
        "format": schema,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    })
    r = _http().post("/api/chat", json=payload, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
    r.raise_for_status()
    content = (r.json().get("message") or {}).get("content") or ""
    if not content.strip():
        if prose_fallback:
            return chat_json(messages, temperature=temperature, max_tokens=num_predict)
        raise ValueError("constrained chat returned empty content")
    return extract_json(content)
