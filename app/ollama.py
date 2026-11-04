"""LLM / embedding HTTP client (pooled connections).

Two backends, chosen per base URL:

- Ollama native (default, ``OLLAMA_BASE_URL``): embeddings via /api/embed, free-form chat via
  its OpenAI layer /v1/chat/completions, schema-constrained chat via /api/chat with ``format``
  + ``think: false`` (on Qwen3 thinking models, thinking tokens otherwise eat num_predict and
  ``format`` is silently ignored).
- OpenAI-compatible (``NEWS_LLM_BASE_URL`` / ``NEWS_EMBED_BASE_URL``, ending in /v1): llama.cpp
  ``llama serve``, AI routers such as Tianji, hosted APIs. /chat/completions with
  ``response_format: json_schema`` for structured calls, /embeddings, optional Bearer key.
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
_clients: dict[tuple[str, str], httpx.Client] = {}
_client_lock = threading.Lock()
_stats = threading.local()
_STAT_KEYS = ("load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")


def _client(base_url: str, api_key: str) -> httpx.Client:
    with _client_lock:
        c = _clients.get((base_url, api_key))
        if c is None:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            # trust_env=False: the model server is usually a LAN host; an http(s)_proxy env var must not reroute it.
            c = _clients[(base_url, api_key)] = httpx.Client(
                base_url=base_url, headers=headers, timeout=config.OLLAMA_TIMEOUT, trust_env=False)
        return c


def _openai() -> bool:
    return bool(config.LLM_BASE_URL)


def _http() -> httpx.Client:
    """Client for LLM calls."""
    if _openai():
        return _client(config.LLM_BASE_URL, config.LLM_API_KEY)
    return _client(config.OLLAMA_BASE_URL, "")


def _embed_http() -> httpx.Client:
    if config.EMBED_BASE_URL:
        return _client(config.EMBED_BASE_URL, config.EMBED_API_KEY)
    return _client(config.OLLAMA_BASE_URL, "")


def _chat_path() -> str:
    return "/chat/completions" if _openai() else "/v1/chat/completions"


def endpoint() -> str:
    """Where LLM calls go (for logs)."""
    return config.LLM_BASE_URL or config.OLLAMA_BASE_URL


def _keep_alive(payload: dict[str, Any]) -> dict[str, Any]:
    if config.OLLAMA_KEEP_ALIVE:
        payload["keep_alive"] = config.OLLAMA_KEEP_ALIVE
    return payload


# Errors meaning "the model server is unreachable" (as opposed to a bad/slow answer).
UNAVAILABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)


def _reachable(c: httpx.Client, openai: bool) -> bool:
    # OpenAI-compatible servers and routers may not serve /models; any non-5xx answer means "up".
    try:
        r = c.get("/models" if openai else "/api/version", timeout=5)
    except httpx.HTTPError:
        return False
    return r.status_code < 500 if openai else r.is_success


def available() -> bool:
    if not _reachable(_http(), _openai()):
        return False
    if _embed_http() is _http():
        return True
    return _reachable(_embed_http(), bool(config.EMBED_BASE_URL))


# --- embeddings ------------------------------------------------------------
def embed_texts(texts: list[str], *, timeout: float | None = None,
                model: str | None = None) -> list[list[float]]:
    if not texts:
        return []
    model = model or config.EMBED_MODEL
    kwargs = {"timeout": timeout} if timeout else {}
    if config.EMBED_BASE_URL:
        r = _embed_http().post("/embeddings", json={"model": model, "input": texts}, **kwargs)
        r.raise_for_status()
        data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
        out = [d["embedding"] for d in data]
    else:
        r = _embed_http().post("/api/embed", json=_keep_alive({"model": model, "input": texts}), **kwargs)
        r.raise_for_status()
        out = r.json().get("embeddings", [])
    if len(out) != len(texts):
        raise RuntimeError(f"embed count mismatch: got {len(out)} for {len(texts)} inputs")
    return out


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]


# --- free-form chat --------------------------------------------------------
def chat(messages: list[dict[str, Any]], *, temperature: float = 0.2,
         max_tokens: int | None = None, model: str | None = None) -> str:
    payload: dict[str, Any] = {
        "model": model or config.CHAT_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    for attempt in range(3):
        try:
            r = _http().post(_chat_path(), json=payload)
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
                max_tokens: int | None = None, model: str | None = None) -> Iterator[tuple[str, str]]:
    """Like :func:`chat`, but yields ``("thinking" | "content", text)`` chunks as they arrive.

    Thinking comes from ``delta.reasoning`` (Ollama's OpenAI layer) or inline ``<think>`` tags.
    """
    payload: dict[str, Any] = {
        "model": model or config.CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    split = _ThinkSplitter()
    with _http().stream("POST", _chat_path(), json=payload) as r:
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
def _record(body: dict[str, Any], model: str) -> None:
    """Add Ollama's own timings (ns) of one /api/chat reply to this thread's totals."""
    acc = getattr(_stats, "acc", None)
    if acc is None:
        acc = _stats.acc = dict.fromkeys(("calls", *_STAT_KEYS), 0)
    acc["calls"] += 1
    for k in _STAT_KEYS:
        acc[k] += body.get(k) or 0
    if (body.get("load_duration") or 0) > 1e9:
        log.info("ollama loaded %s in %.1fs (evicted by another model or keep_alive expiry)",
                 model, body["load_duration"] / 1e9)


def take_stats() -> dict[str, float]:
    """Totals of this thread's structured calls since the last take (seconds / tokens)."""
    acc = getattr(_stats, "acc", None) or dict.fromkeys(("calls", *_STAT_KEYS), 0)
    _stats.acc = None
    return {"calls": acc["calls"], "load_s": acc["load_duration"] / 1e9,
            "in_tok": acc["prompt_eval_count"], "in_s": acc["prompt_eval_duration"] / 1e9,
            "out_tok": acc["eval_count"], "out_s": acc["eval_duration"] / 1e9}


def _openai_stats(body: dict[str, Any]) -> dict[str, Any]:
    """OpenAI ``usage`` (+ llama.cpp ``timings`` in ms) in Ollama's stat keys."""
    usage, timings = body.get("usage") or {}, body.get("timings") or {}
    return {"prompt_eval_count": usage.get("prompt_tokens"), "eval_count": usage.get("completion_tokens"),
            "prompt_eval_duration": (timings.get("prompt_ms") or 0) * 1e6,
            "eval_duration": (timings.get("predicted_ms") or 0) * 1e6}


def structured(schema: dict[str, Any], messages: list[dict[str, Any]], *,
               temperature: float = 0.0, num_predict: int = 1024,
               timeout: float | None = None, prose_fallback: bool = False,
               model: str | None = None) -> Any:
    """Return the parsed JSON the model emitted under ``schema``.

    With ``prose_fallback`` an empty constrained reply is retried as a plain
    chat and parsed leniently (the known ``think`` interaction).
    """
    model = model or config.CHAT_MODEL
    timeout_arg = timeout or httpx.USE_CLIENT_DEFAULT
    if _openai():
        r = _http().post("/chat/completions", timeout=timeout_arg, json={
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema}},
            "stream": False,
            "temperature": temperature,
            "max_tokens": num_predict,
        })
        r.raise_for_status()
        body = r.json()
        _record(_openai_stats(body), model)
        content = (((body.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    else:
        r = _http().post("/api/chat", timeout=timeout_arg, json=_keep_alive({
            "model": model,
            "messages": messages,
            "format": schema,
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }))
        r.raise_for_status()
        body = r.json()
        _record(body, model)
        content = (body.get("message") or {}).get("content") or ""
    if not content.strip():
        if prose_fallback:
            return chat_json(messages, temperature=temperature, max_tokens=num_predict, model=model)
        raise ValueError("constrained chat returned empty content")
    return extract_json(content)
