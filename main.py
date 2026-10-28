"""Entrypoint: serve the FastAPI app with uvicorn (see app/api.py)."""
import gzip
import logging
import logging.handlers
import os
import shutil
import sys
from typing import IO

import uvicorn

from app import bus, chat_jobs, config

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as src, gzip.open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(source)


def setup_logging() -> None:
    if config.LOG_FILE:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            config.LOG_FILE, maxBytes=int(config.LOG_MAX_MB * 1024 * 1024),
            backupCount=config.LOG_BACKUPS, encoding="utf-8",
        )
        handler.namer = lambda name: name + ".gz"  # server.log.1.gz, server.log.2.gz, ...
        handler.rotator = _gzip_rotator
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    logging.basicConfig(level=config.LOG_LEVEL, handlers=[handler], force=True)
    # Per-request client logs (hundreds of feed fetches per cycle) drown the pipeline lines.
    for noisy in ("httpx", "httpcore", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class _Server(uvicorn.Server):
    def handle_exit(self, sig, frame) -> None:
        # Uvicorn waits for open responses before shutting down; end the endless SSE streams now
        # and interrupt running chat answers (their partial text is saved).
        bus.close_all()
        chat_jobs.stop_all()
        super().handle_exit(sig, frame)


def _single_instance() -> IO | None:
    """Lock held for the process lifetime. A second server runs its lifespan (workers, ntfy
    scheduler -> duplicate pushes) before it fails to bind the port, and overwrites the PID file."""
    try:
        import fcntl
    except ImportError:  # Windows dev box
        return None
    path = config.PID_FILE.with_name(config.PID_FILE.name + ".lock")
    handle = open(path, "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"another server is already running ({path} is locked)")
    return handle


def main() -> None:
    lock = _single_instance()  # noqa: F841 - released when the process ends
    setup_logging()
    config.PID_FILE.write_text(str(os.getpid()))
    try:
        # log_config=None: uvicorn's loggers propagate to the handler above.
        server = _Server(uvicorn.Config(
            "app.api:app", host=config.HOST, port=config.PORT, log_config=None,
            proxy_headers=True, forwarded_allow_ips=config.FORWARDED_ALLOW_IPS,
            timeout_graceful_shutdown=5,
        ))
        server.run()
    finally:
        if config.PID_FILE.exists() and config.PID_FILE.read_text().strip() == str(os.getpid()):
            config.PID_FILE.unlink()


if __name__ == "__main__":
    main()
