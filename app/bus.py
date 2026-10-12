"""In-process pub/sub for Server-Sent Events.

``publish`` may be called from any thread; each subscriber is an asyncio
queue on the server's event loop, so SSE streams don't pin threadpool workers.
"""
from __future__ import annotations

import asyncio
import json
import threading

_subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
_lock = threading.Lock()
_QUEUE_MAX = 100


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    with _lock:
        _subscribers.add((asyncio.get_running_loop(), q))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        for sub in [s for s in _subscribers if s[1] is q]:
            _subscribers.discard(sub)


def _put(q: asyncio.Queue, record: dict | None) -> None:
    try:
        q.put_nowait(record)
    except asyncio.QueueFull:  # slow client: drop the event
        pass


def publish(record: dict | None) -> None:
    """Fan out ``record`` to every subscriber; ``None`` tells streams to close."""
    with _lock:
        subs = list(_subscribers)
    for loop, q in subs:
        try:
            loop.call_soon_threadsafe(_put, q, record)
        except RuntimeError:  # loop already closed
            unsubscribe(q)


def close_all() -> None:
    publish(None)


def sse(record: dict) -> str:
    return f"data: {json.dumps(record)}\n\n"
