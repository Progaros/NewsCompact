"""Background worker: periodically runs ingest + AI pipeline.

Runs on a daemon thread with a lock so cycles never overlap. A manual refresh
endpoint also calls run_cycle() from the API thread.
"""
from __future__ import annotations

import logging
import resource
import threading
import time

from . import bus, config, db, ingest, pipeline

log = logging.getLogger("news.worker")

_lock = threading.Lock()
_stop = threading.Event()


def _proc_cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def run_cycle() -> dict:
    """One full ingestion + pipeline pass. Returns a small summary."""
    result = {"ingest_new": 0, "processed": 0, "error": None}
    wall0 = time.perf_counter()
    cpu0 = _proc_cpu()
    try:
        result["ingest_new"] = ingest.ingest_all()
        ingest_wall, ingest_cpu = time.perf_counter() - wall0, _proc_cpu() - cpu0
    except Exception as e:  # noqa: BLE001
        result["error"] = f"ingest: {e}"
        log.exception("ingest error")
        ingest_wall, ingest_cpu = time.perf_counter() - wall0, _proc_cpu() - cpu0
    wall0 = time.perf_counter()
    cpu0 = _proc_cpu()
    try:
        result["processed"] = pipeline.process_new_articles(emit=bus.publish)
        pipeline_wall, pipeline_cpu = time.perf_counter() - wall0, _proc_cpu() - cpu0
    except Exception as e:  # noqa: BLE001
        result["error"] = result.get("error") or f"pipeline: {e}"
        log.exception("pipeline error")
        pipeline_wall, pipeline_cpu = time.perf_counter() - wall0, _proc_cpu() - cpu0
    log.info(
        "cycle ingest=%.1fs/cpu%.1fs (%d new) pipeline=%.1fs/cpu%.1fs (%d processed)",
        ingest_wall, ingest_cpu, result["ingest_new"],
        pipeline_wall, pipeline_cpu, result["processed"],
    )
    return result


def _loop() -> None:
    while not _stop.is_set():
        if _lock.acquire(blocking=False):
            try:
                run_cycle()
                bus.publish({"type": "cycle_done"})
            finally:
                _lock.release()
        _stop.wait(config.WORKER_INTERVAL_SECONDS)


def start() -> None:
    db.init_db()
    from .opml import import_opml

    import_opml()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def stop() -> None:
    _stop.set()
