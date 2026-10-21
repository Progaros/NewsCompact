"""Background worker: an ingest loop and a pipeline loop on separate threads.

Ingest runs every WORKER_INTERVAL_SECONDS (start-to-start). The pipeline
thread sleeps until ingest stores new articles for an enabled feed (or a
feed is enabled / a refresh is requested) and then drains the queue, so new
articles no longer wait for the remaining feeds or the next cycle.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import bus, config, db, ingest, merge, pipeline, retention, split

log = logging.getLogger("news.worker")

_ingest_lock = threading.Lock()
_pipeline_lock = threading.Lock()
_wake = threading.Event()
_stop = threading.Event()
_threads: list[threading.Thread] = []

# Live status for /api/health and /list.
status: dict[str, Any] = {
    "ingest_running": False,
    "ingest_last_started": None,
    "ingest_last_seconds": None,
    "ingest_last_new": None,
    "pipeline_running": False,
    "pipeline_last_finished": None,
    "pipeline_last_processed": None,
    "pipeline_last_seconds": None,
    "merge_last_finished": None,
    "merge_last_checked": None,
    "merge_last_merged": None,
    "merge_last_seconds": None,
    "split_last_finished": None,
    "split_last_checked": None,
    "split_last_split": None,
    "split_last_seconds": None,
    "retention_last_run": None,
    "retention_last_deleted_articles": None,
    "retention_last_deleted_stories": None,
}
_retention_due = 0.0  # monotonic time of the next retention run


def wake_pipeline() -> None:
    _wake.set()


def run_ingest() -> int:
    with _ingest_lock:
        status.update(ingest_running=True, ingest_last_started=db.utcnow())
        t0 = time.perf_counter()
        try:
            new = ingest.ingest_all(on_new=wake_pipeline)
        except Exception:  # noqa: BLE001
            log.exception("ingest failed")
            new = 0
        finally:
            status["ingest_running"] = False
        secs = time.perf_counter() - t0
        status.update(ingest_last_seconds=round(secs, 1), ingest_last_new=new)
        log.info("ingest done: %d new articles in %.1fs", new, secs)
        return new


def _run_retention() -> None:
    global _retention_due
    if time.monotonic() < _retention_due:
        return
    _retention_due = time.monotonic() + config.RETENTION_INTERVAL_HOURS * 3600
    try:
        result = retention.run()
    except Exception:  # noqa: BLE001
        log.exception("retention failed")
        return
    status.update(retention_last_run=db.utcnow(), retention_last_deleted_articles=result["articles"],
                  retention_last_deleted_stories=result["stories"])


def run_pipeline() -> dict[str, int]:
    with _pipeline_lock:
        _run_retention()
        status["pipeline_running"] = True
        t0, cpu0 = time.perf_counter(), time.process_time()
        try:
            result = pipeline.process_pending(emit=bus.publish, should_stop=_stop.is_set)
        except Exception:  # noqa: BLE001
            log.exception("pipeline run failed")
            result = {"processed": 0, "failed": 0}
        secs = time.perf_counter() - t0
        status.update(pipeline_last_finished=db.utcnow(), pipeline_last_processed=result["processed"],
                      pipeline_last_seconds=round(secs, 1))
        merged = _run_merge()
        splits = _run_split()
        status["pipeline_running"] = False
        if result["processed"] or result["failed"]:
            log.info("pipeline run: %d processed, %d failed in %.1fs (cpu %.1fs)",
                     result["processed"], result["failed"], secs, time.process_time() - cpu0)
        if result["processed"] or result["failed"] or merged or splits:
            bus.publish({"type": "cycle_done", **result, "merged": merged, "split": splits})
        return result


def _run_merge() -> int:
    if _stop.is_set():
        return 0
    t0 = time.perf_counter()
    try:
        result = merge.run(should_stop=_stop.is_set)
    except Exception:  # noqa: BLE001
        log.exception("merge pass failed")
        return 0
    status.update(merge_last_finished=db.utcnow(), merge_last_checked=result["checked"],
                  merge_last_merged=result["merged"], merge_last_seconds=round(time.perf_counter() - t0, 1))
    return result["merged"]


def _run_split() -> int:
    if _stop.is_set():
        return 0
    t0 = time.perf_counter()
    try:
        result = split.run(should_stop=_stop.is_set)
    except Exception:  # noqa: BLE001
        log.exception("split pass failed")
        return 0
    status.update(split_last_finished=db.utcnow(), split_last_checked=result["checked"],
                  split_last_split=result["split"], split_last_seconds=round(time.perf_counter() - t0, 1))
    return result["split"]


def refresh() -> dict[str, Any]:
    """Manual refresh: ingest now, then drain the queue (blocks)."""
    new = run_ingest()
    return {"ingest_new": new, **run_pipeline()}


def _ingest_loop() -> None:
    while not _stop.is_set():
        started = time.monotonic()
        run_ingest()
        _stop.wait(max(0.0, config.WORKER_INTERVAL_SECONDS - (time.monotonic() - started)))


def _pipeline_loop() -> None:
    _wake.set()  # drain any backlog left from the previous run
    while not _stop.is_set():
        _wake.wait(timeout=config.WORKER_INTERVAL_SECONDS)
        _wake.clear()
        if not _stop.is_set():
            run_pipeline()


def start() -> None:
    if _threads:
        return
    _stop.clear()
    for name, target in (("ingest", _ingest_loop), ("pipeline", _pipeline_loop)):
        t = threading.Thread(target=target, name=f"news-{name}", daemon=True)
        t.start()
        _threads.append(t)


def stop(timeout: float = 5.0) -> None:
    """Signal both loops; the pipeline finishes its current article at most."""
    _stop.set()
    _wake.set()
    deadline = time.monotonic() + timeout
    for t in _threads:
        t.join(max(0.0, deadline - time.monotonic()))
    _threads.clear()
