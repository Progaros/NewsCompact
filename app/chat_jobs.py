"""Chat answers as server-side background jobs, independent of the HTTP request.

Closing the popup, reloading or leaving the page no longer interrupts an answer:
each job runs in its own thread, keeps every NDJSON event it produced, and any
client of the same user can attach to it again (replay + live). Only ``stop``
(the Stop button, or server shutdown) interrupts it; whatever was generated is saved.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

from . import chat, chat_history, config

log = logging.getLogger("news.chat")

_POLL_S = 0.1  # followers poll the event list instead of pinning a thread each


class Busy(Exception):
    """This story already has an answer in progress."""


class TooMany(Exception):
    """The user already has CHAT_MAX_JOBS_PER_USER answers in progress."""


class Job:
    def __init__(self, user_id: int, story_id: int, question: str) -> None:
        self.user_id = user_id
        self.story_id = story_id
        self.question = question
        self.started = time.time()
        self.stop = threading.Event()
        self._events: list[dict[str, Any]] = []
        self._done = False
        self._lock = threading.Lock()

    @property
    def done(self) -> bool:
        return self._done

    def emit(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(record)

    def _since(self, i: int) -> tuple[list[dict[str, Any]], bool]:
        with self._lock:
            return self._events[i:], self._done

    def _finish(self) -> None:
        with self._lock:
            self._done = True
        with _lock:
            if _jobs.get((self.user_id, self.story_id)) is self:
                del _jobs[(self.user_id, self.story_id)]

    async def follow(self) -> AsyncIterator[dict[str, Any]]:
        """All events from the start, then live ones until the job ends."""
        i = 0
        while True:
            new, done = self._since(i)
            i += len(new)
            for record in new:
                yield record
            if done:
                return
            if not new:
                await asyncio.sleep(_POLL_S)


_jobs: dict[tuple[int, int], Job] = {}
_lock = threading.Lock()


def get(user_id: int, story_id: int) -> Job | None:
    with _lock:
        return _jobs.get((user_id, story_id))


def running(user_id: int) -> dict[int, Job]:
    """story_id -> job of the user's answers in progress."""
    with _lock:
        return {sid: job for (uid, sid), job in _jobs.items() if uid == user_id}


def reserve(user_id: int, story_id: int, question: str) -> Job:
    """Claim the story's answer slot (before the question is stored); raises Busy / TooMany."""
    with _lock:
        if (user_id, story_id) in _jobs:
            raise Busy
        if sum(1 for uid, _ in _jobs if uid == user_id) >= config.CHAT_MAX_JOBS_PER_USER:
            raise TooMany
        job = _jobs[(user_id, story_id)] = Job(user_id, story_id, question)
        return job


def release(job: Job) -> None:
    """Give a reserved slot back without running it."""
    job._finish()


def run(job: Job, story: dict, history: list[dict], web_search: bool, about: str,
        session_id: int, question_id: int) -> None:
    threading.Thread(target=_run, args=(job, story, history, web_search, about, session_id, question_id),
                     name=f"chat-{job.story_id}", daemon=True).start()


def stop_all() -> None:
    with _lock:
        jobs = list(_jobs.values())
    for job in jobs:
        job.stop.set()


def _run(job: Job, story: dict, history: list[dict], web_search: bool, about: str,
         session_id: int, question_id: int) -> None:
    parts: list[str] = []
    steps: list[dict] = []
    saved = False

    def save(stopped: bool = False) -> dict | None:
        nonlocal saved
        if saved:
            return None
        saved = True
        reply = "".join(parts).strip()
        for s in steps:
            if s.get("status") == "running":
                if s.get("kind") == "thinking":
                    s.update(status="ok", title="Thought (interrupted)")
                else:
                    s.update(status="warn", title=f"{s['title']} (stopped)")
        marker = None
        if stopped:
            marker = chat.add_step(steps, "stopped", f"Stopped — answer interrupted{' (partial)' if reply else ''}",
                                   status="warn")
        chat_history.save_reply(session_id, question_id, reply, steps)
        return marker

    gen = chat.stream(story, history, job.question, web_search, steps, about=about)
    try:
        for kind, payload in gen:
            if job.stop.is_set():
                break
            if kind == "delta":
                parts.append(payload)
            # Steps are updated in place later on: emit a snapshot.
            job.emit({"type": kind, "step": dict(payload)} if kind == "step" else {"type": kind, "text": payload})
        if job.stop.is_set():
            gen.close()
            marker = save(stopped=True)
            if marker:
                job.emit({"type": "step", "step": dict(marker)})
        else:
            save()
        job.emit({"type": "done"})
    except Exception as e:
        log.exception("chat failed for story %s", job.story_id)
        step = chat.add_step(steps, "error", "Answer failed", status="error", detail=chat.error_text(e))
        save()
        job.emit({"type": "step", "step": dict(step)})
        job.emit({"type": "error", "text": f"chat failed: {type(e).__name__}"})
    finally:
        try:
            save(stopped=True)
        except Exception:
            log.exception("saving the chat answer failed for story %s", job.story_id)
        job._finish()
