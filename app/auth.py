"""Single-user login: one long password (env) → cookie session, in-memory.

Design decisions (see findings.md, "Authentication & API security"):
- Sessions live in a module-level dict — a server restart means re-login,
  which is exactly the blast radius we want (no DB migration for auth).
- When `config.AUTH_PASSWORD` is empty, auth is DISABLED (open mode) and
  every check passes, so the app runs out of the box on a trusted LAN.
- Login is throttled per client IP (sliding window) so the long password
  is the only real credential, not a brute-force target.
- The cookie is HttpOnly + SameSite=Lax: JS can't read it, and cross-site
  pages can't issue state-changing POSTs to us.
"""
from __future__ import annotations

import secrets
import threading
import time

from . import config

# --- sessions --------------------------------------------------------------

_sessions: dict[str, float] = {}  # token -> expires (epoch seconds)
_lock = threading.Lock()


def auth_enabled() -> bool:
    return bool(config.AUTH_PASSWORD)


def check_password(candidate: str) -> bool:
    """Constant-time comparison; the password itself never reaches logs."""
    expected = config.AUTH_PASSWORD.encode("utf-8")
    actual = (candidate or "").encode("utf-8")
    return bool(expected) and secrets.compare_digest(expected, actual)


def _sweep_locked() -> None:
    """Drop expired sessions. Caller must hold `_lock`."""
    now = time.time()
    for tok in [t for t, exp in _sessions.items() if exp <= now]:
        del _sessions[tok]


def create_session() -> str:
    token = secrets.token_urlsafe(32)  # 256-bit
    with _lock:
        _sweep_locked()
        _sessions[token] = time.time() + config.AUTH_SESSION_HOURS * 3600
    return token


def is_authenticated(token: str | None) -> bool:
    """True in open mode (no password set); otherwise token must be live."""
    if not auth_enabled():
        return True
    if not token:
        return False
    with _lock:
        _sweep_locked()
        exp = _sessions.get(token)
    return exp is not None and exp > time.time()


def destroy_session(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


# --- login throttle (per client IP, sliding window) -------------------------

_MAX_FAILS = 5            # failures allowed ...
_WINDOW_SECONDS = 60.0    # ... within this window before a short lockout
_MAX_IPS = 200            # memory bound for the failure table

_failures: dict[str, list[float]] = {}
_fail_lock = threading.Lock()


def _prune_locked(ip: str) -> list[float]:
    """Refresh the IP's window; drop fully-expired IPs to bound memory."""
    now = time.time()
    cutoff = now - _WINDOW_SECONDS
    recent = [t for t in _failures.get(ip, []) if t > cutoff]
    _failures[ip] = recent
    if len(_failures) > _MAX_IPS:
        for other in [k for k, v in _failures.items() if not v]:
            del _failures[other]
    return recent


def login_throttled(ip: str) -> bool:
    """True once the IP burned its failure budget inside the window."""
    with _fail_lock:
        return len(_prune_locked(ip)) >= _MAX_FAILS


def record_failure(ip: str) -> None:
    with _fail_lock:
        _prune_locked(ip).append(time.time())


def clear_failures(ip: str) -> None:
    with _fail_lock:
        _failures.pop(ip, None)
