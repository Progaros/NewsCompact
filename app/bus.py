"""Tiny in-process pub/sub for Server-Sent Events.

Each SSE client gets its own queue; `publish` fans out to every subscriber.
Good enough for a single local server process.
"""
from __future__ import annotations

import json
import queue
import threading

_subscribers: set["queue.Queue[dict]"] = set()
_lock = threading.Lock()


def subscribe() -> "queue.Queue[dict]":
    q: "queue.Queue[dict]" = queue.Queue()
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: "queue.Queue[dict]") -> None:
    with _lock:
        _subscribers.discard(q)


def publish(record: dict) -> None:
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(record)
        except queue.Full:  # pragma: no cover - drops slow clients
            pass


def emit_json(record: dict) -> str:
    return f"data: {json.dumps(record)}\n\n"
