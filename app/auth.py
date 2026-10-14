"""Login: user name + password → cookie session (in-memory), for several users.

Design decisions (see findings.md, "Authentication & API security"):
- Sessions live in a module-level dict (token → user id, expiry) — a server
  restart means re-login, which is exactly the blast radius we want.
- Passwords are stored as salted scrypt hashes (AppUser.password_hash). User 1
  (the admin from the single-user era) logs in with NEWS_AUTH_PASSWORD until
  it gets a password of its own.
- When `config.AUTH_PASSWORD` is empty, auth is DISABLED (open mode): requests
  without a session act as user 1, so the app runs out of the box on a trusted LAN.
- Login is throttled per client IP (sliding window) so passwords are not a
  brute-force target.
- The cookie is HttpOnly + SameSite=Lax: JS can't read it, and cross-site
  pages can't issue state-changing POSTs to us.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from typing import Any

from . import config, db

OWNER_ID = 1
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}

# --- passwords -------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, digest = (stored or "").split("$")
        if algo != "scrypt":
            return False
        actual = hashlib.scrypt((password or "").encode("utf-8"), salt=bytes.fromhex(salt), dklen=32,
                                n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, bytes.fromhex(digest))


# --- sessions --------------------------------------------------------------

_sessions: dict[str, tuple[int, float]] = {}  # token -> (user id, expires epoch seconds)
_lock = threading.Lock()


def auth_enabled() -> bool:
    return bool(config.AUTH_PASSWORD)


def check_password(candidate: str) -> bool:
    """Constant-time comparison with NEWS_AUTH_PASSWORD; the password itself never reaches logs."""
    expected = config.AUTH_PASSWORD.encode("utf-8")
    actual = (candidate or "").encode("utf-8")
    return bool(expected) and secrets.compare_digest(expected, actual)


def _user_row(conn: sqlite3.Connection, *, user_id: int | None = None, name: str | None = None) -> dict | None:
    if user_id is not None:
        row = conn.execute("SELECT * FROM AppUser WHERE id = ?", (user_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM AppUser WHERE name = ?", ((name or "").strip(),)).fetchone()
    return dict(row) if row else None


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "is_admin": bool(row["is_admin"])}


def authenticate(name: str | None, password: str) -> dict[str, Any] | None:
    """The user for these credentials, or None. No name = the owner (user 1)."""
    with db.get_conn() as conn:
        row = _user_row(conn, user_id=OWNER_ID) if not (name or "").strip() else _user_row(conn, name=name)
    if row is None:
        verify_password(password, hash_password("timing"))  # don't reveal unknown names by timing
        return None
    if row["password_hash"]:
        return row if verify_password(password, row["password_hash"]) else None
    return row if row["id"] == OWNER_ID and check_password(password) else None


def change_password(user_id: int, current: str, new: str) -> bool:
    """Set a new password after checking the current one (open-mode owner without one: no check)."""
    with db.get_conn() as conn:
        row = _user_row(conn, user_id=user_id)
        if row is None:
            return False
        if row["password_hash"]:
            ok = verify_password(current, row["password_hash"])
        else:
            ok = check_password(current) if auth_enabled() else row["id"] == OWNER_ID
        if ok:
            conn.execute("UPDATE AppUser SET password_hash = ? WHERE id = ?", (hash_password(new), user_id))
        return ok


def _sweep_locked() -> None:
    """Drop expired sessions. Caller must hold `_lock`."""
    now = time.time()
    for tok in [t for t, (_uid, exp) in _sessions.items() if exp <= now]:
        del _sessions[tok]


_MAX_SESSIONS = 500


def create_session(user_id: int = OWNER_ID) -> str:
    token = secrets.token_urlsafe(32)  # 256-bit
    with _lock:
        _sweep_locked()
        if len(_sessions) >= _MAX_SESSIONS:
            oldest = min(_sessions, key=lambda t: _sessions[t][1])
            del _sessions[oldest]
        _sessions[token] = (user_id, time.time() + config.AUTH_SESSION_HOURS * 3600)
    return token


def session_user_id(token: str | None) -> int | None:
    if not token:
        return None
    with _lock:
        _sweep_locked()
        entry = _sessions.get(token)
    return entry[0] if entry and entry[1] > time.time() else None


def current_user(token: str | None) -> dict[str, Any] | None:
    """The signed-in user (open mode: user 1 without a session); None when anonymous."""
    uid = session_user_id(token)
    if uid is None and not auth_enabled():
        uid = OWNER_ID
    if uid is None:
        return None
    with db.get_conn() as conn:
        row = _user_row(conn, user_id=uid)
    return public_user(row) if row else None


def is_authenticated(token: str | None) -> bool:
    """True in open mode (no password set); otherwise token must be live."""
    return current_user(token) is not None


def destroy_session(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


def end_user_sessions(user_id: int, *, keep: str | None = None) -> None:
    with _lock:
        for tok in [t for t, (uid, _exp) in _sessions.items() if uid == user_id and t != keep]:
            del _sessions[tok]


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
