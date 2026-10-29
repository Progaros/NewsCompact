"""Ollama HTTP client (one shared pooled connection).

- embeddings: native /api/embed
- free-form chat: OpenAI-compatible /v1/chat/completions (with retries)
- schema-constrained chat: native /api/chat with ``format`` + ``think: false``
  (on Qwen3 thinking models, thinking tokens otherwise eat num_predict and
  ``format`` is silently ignored).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx

from . import config

log = logging.getLogger("news.ollama")
_client: httpx.Client | None = None
_client_lock = threading.Lock()
_stats = threading.local()
_STAT_KEYS = ("load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")


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
UNAVAILABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)


def available() -> bool:
    try:
        _http().get("/api/version", timeout=5).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


# --- embeddings ------------------------------------------------------------
def embed_texts(texts: list[str], *, timeout: float | None = None) -> list[list[float]]:
    if not texts:
        return []
    r = _http().post("/api/embed", json=_keep_alive({"model": config.EMBED_MODEL, "input": texts}),
                     **({"timeout": timeout} if timeout else {}))
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


class _ThinkSplitter:
    """Routes inline ``<think>…</think>`` spans of a streamed answer to "thinking"."""

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False

    def _kind(self) -> str:
        return "thinking" if self.inside else "content"

    def feed(self, text: str) -> Iterator[tuple[str, str]]:
        self.buf += text
        while True:
            tag = "</think>" if self.inside else "<think>"
            i = self.buf.find(tag)
            if i >= 0:
                if i:
                    yield self._kind(), self.buf[:i]
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            # Hold back a tag that may be split across chunks.
            keep = next((k for k in range(min(len(tag) - 1, len(self.buf)), 0, -1)
                         if tag.startswith(self.buf[-k:])), 0)
            out, self.buf = self.buf[:len(self.buf) - keep], self.buf[len(self.buf) - keep:]
            if out:
                yield self._kind(), out
            return

    def flush(self) -> Iterator[tuple[str, str]]:
        if self.buf:
            yield self._kind(), self.buf
        self.buf = ""


def chat_stream(messages: list[dict[str, Any]], *, temperature: float = 0.2,
                max_tokens: int | None = None) -> Iterator[tuple[str, str]]:
    """Like :func:`chat`, but yields ``("thinking" | "content", text)`` chunks as they arrive.

    Thinking comes from ``delta.reasoning`` (Ollama's OpenAI layer) or inline ``<think>`` tags.
    """
    payload: dict[str, Any] = {
        "model": config.CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    split = _ThinkSplitter()
    with _http().stream("POST", "/v1/chat/completions", json=payload) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            choices = json.loads(data).get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning:
                yield "thinking", reasoning
            text = delta.get("content")
            if text:
                yield from split.feed(text)
    yield from split.flush()


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
    obj_i, arr_i = text.find("{"), text.find("[")
    pairs = [("[", "]"), ("{", "}")] if 0 <= arr_i < (obj_i if obj_i >= 0 else len(text)) else [("{", "}")]
    for start, end in pairs:
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
def _record(body: dict[str, Any]) -> None:
    """Add Ollama's own timings (ns) of one /api/chat reply to this thread's totals."""
    acc = getattr(_stats, "acc", None)
    if acc is None:
        acc = _stats.acc = dict.fromkeys(("calls", *_STAT_KEYS), 0)
    acc["calls"] += 1
    for k in _STAT_KEYS:
        acc[k] += body.get(k) or 0
    if (body.get("load_duration") or 0) > 1e9:
        log.info("ollama loaded %s in %.1fs (evicted by another model or keep_alive expiry)",
                 config.CHAT_MODEL, body["load_duration"] / 1e9)


def take_stats() -> dict[str, float]:
    """Totals of this thread's structured calls since the last take (seconds / tokens)."""
    acc = getattr(_stats, "acc", None) or dict.fromkeys(("calls", *_STAT_KEYS), 0)
    _stats.acc = None
    return {"calls": acc["calls"], "load_s": acc["load_duration"] / 1e9,
            "in_tok": acc["prompt_eval_count"], "in_s": acc["prompt_eval_duration"] / 1e9,
            "out_tok": acc["eval_count"], "out_s": acc["eval_duration"] / 1e9}


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
    body = r.json()
    _record(body)
    content = (body.get("message") or {}).get("content") or ""
    if not content.strip():
        if prose_fallback:
            return chat_json(messages, temperature=temperature, max_tokens=num_predict)
        raise ValueError("constrained chat returned empty content")
    return extract_json(content)
