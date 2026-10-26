"""Login: user name + password → cookie session, for several users.

Design decisions (see findings.md, "Authentication & API security"):
- Sessions live in the AuthSession table keyed by the SHA-256 of the cookie token
  (the token itself is never stored), so they survive restarts. Expiry is fixed
  (AUTH_SESSION_HOURS); the table is capped at _MAX_SESSIONS (oldest dropped).
  Owner sessions opened with NEWS_AUTH_PASSWORD end when that password changes.
- Passwords are stored as salted scrypt hashes (AppUser.password_hash). User 1
  (the admin from the single-user era) logs in with NEWS_AUTH_PASSWORD until
  it gets a password of its own.
- Startup fails unless NEWS_AUTH_PASSWORD is set or an admin has a password of
  their own (there is no unauthenticated mode).
- Login is throttled per client IP (sliding window) and per account (a growing
  pause after many failures from any IP; IPs the account signed in from before
  skip it, so an attacker can't lock the owner out).
- Unknown user names cost one scrypt run like known ones (no timing oracle).
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


_DUMMY_HASH = hash_password("timing")


# --- sessions --------------------------------------------------------------


def login_configured() -> bool:
    """Someone can sign in as admin: NEWS_AUTH_PASSWORD is set or an admin has a stored password."""
    if config.AUTH_PASSWORD:
        return True
    with db.get_conn() as conn:
        return conn.execute("SELECT 1 FROM AppUser WHERE is_admin = 1 AND COALESCE(password_hash, '') != ''"
                            ).fetchone() is not None


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
        verify_password(password, _DUMMY_HASH)  # don't reveal unknown names by timing
        return None
    if row["password_hash"]:
        return row if verify_password(password, row["password_hash"]) else None
    return row if row["id"] == OWNER_ID and check_password(password) else None


def change_password(user_id: int, current: str, new: str) -> bool:
    """Set a new password after checking the current one."""
    with db.get_conn() as conn:
        row = _user_row(conn, user_id=user_id)
        if row is None:
            return False
        if row["password_hash"]:
            ok = verify_password(current, row["password_hash"])
        else:
            ok = check_password(current)
        if ok:
            conn.execute("UPDATE AppUser SET password_hash = ? WHERE id = ?", (hash_password(new), user_id))
        return ok


_MAX_SESSIONS = 500


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _env_stamp() -> str:
    return hashlib.sha256(("env:" + config.AUTH_PASSWORD).encode("utf-8")).hexdigest()[:16]


def create_session(user_id: int = OWNER_ID) -> str:
    token = secrets.token_urlsafe(32)  # 256-bit
    now = time.time()
    with db.get_conn() as conn:
        row = _user_row(conn, user_id=user_id)
        stamp = _env_stamp() if row is not None and not row["password_hash"] else None
        conn.execute("DELETE FROM AuthSession WHERE expires_at <= ?", (now,))
        excess = conn.execute("SELECT count(*) FROM AuthSession").fetchone()[0] - _MAX_SESSIONS + 1
        if excess > 0:
            conn.execute("DELETE FROM AuthSession WHERE token_hash IN "
                         "(SELECT token_hash FROM AuthSession ORDER BY expires_at LIMIT ?)", (excess,))
        conn.execute("INSERT INTO AuthSession (token_hash, user_id, expires_at, env_stamp) VALUES (?, ?, ?, ?)",
                     (_token_hash(token), user_id, now + config.AUTH_SESSION_HOURS * 3600, stamp))
    return token


def current_user(token: str | None) -> dict[str, Any] | None:
    """The signed-in user; None when anonymous."""
    if not token:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT u.id, u.name, u.is_admin, u.password_hash, s.env_stamp FROM AuthSession s "
            "JOIN AppUser u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
            (_token_hash(token), time.time()),
        ).fetchone()
    if row is None:
        return None
    if not row["password_hash"] and row["env_stamp"] != _env_stamp():
        return None  # opened with a NEWS_AUTH_PASSWORD that has changed since
    return public_user(row)


def is_authenticated(token: str | None) -> bool:
    return current_user(token) is not None


def destroy_session(token: str | None) -> None:
    if token:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM AuthSession WHERE token_hash = ?", (_token_hash(token),))


def end_user_sessions(user_id: int, *, keep: str | None = None) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM AuthSession WHERE user_id = ? AND token_hash != ?",
                     (user_id, _token_hash(keep) if keep else ""))


# --- login throttle (per client IP, sliding window) -------------------------

_MAX_FAILS = 5            # failures allowed ...
_WINDOW_SECONDS = 60.0    # ... within this window before a short lockout
_MAX_IPS = 200            # memory bound for the failure table

_failures: dict[str, list[float]] = {}
_fail_lock = threading.Lock()


def _prune_locked(ip: str) -> list[float]:
    """Refresh the IP's window; drop expired IPs, then the least recently seen, to bound memory."""
    cutoff = time.time() - _WINDOW_SECONDS
    recent = [t for t in _failures.pop(ip, []) if t > cutoff]
    if len(_failures) >= _MAX_IPS:
        for other in [k for k, v in _failures.items() if not v or v[-1] <= cutoff]:
            del _failures[other]
        while len(_failures) >= _MAX_IPS:
            del _failures[next(iter(_failures))]
    _failures[ip] = recent  # re-inserted: dict order = least recently seen first
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


# --- login throttle per account (all IPs): a growing pause, skipped by known IPs ---

_ACCOUNT_FREE_FAILS = 10          # failures before pauses start
_ACCOUNT_MAX_PAUSE = 300.0        # seconds
_ACCOUNT_RESET_SECONDS = 3600.0   # a quiet hour forgets the failures
_MAX_ACCOUNTS = 1000              # memory bound (unknown names are tracked too)
_KNOWN_IPS_PER_ACCOUNT = 20

_accounts: dict[str, tuple[int, float]] = {}   # account key -> (failures, last failure)
_known_ips: dict[str, dict[str, None]] = {}    # account key -> IPs it signed in from (oldest first)


def account_key(name: str | None) -> str:
    """Throttle key of a login name; no name = the owner, so "" and the owner's name share one budget."""
    name = (name or "").strip()
    if not name:
        with db.get_conn() as conn:
            row = conn.execute("SELECT name FROM AppUser WHERE id = ?", (OWNER_ID,)).fetchone()
        name = row[0] if row else ""
    return name.casefold()


def _account_fails_locked(key: str, now: float) -> tuple[int, float]:
    fails, last = _accounts.get(key, (0, 0.0))
    return (0, last) if now - last > _ACCOUNT_RESET_SECONDS else (fails, last)


def account_wait(key: str, ip: str) -> float:
    """Seconds before the account takes another attempt from ``ip`` (0 = now)."""
    now = time.time()
    with _fail_lock:
        if ip in _known_ips.get(key, {}):
            return 0.0
        fails, last = _account_fails_locked(key, now)
    excess = fails - _ACCOUNT_FREE_FAILS
    if excess < 0:
        return 0.0
    return max(0.0, last + min(_ACCOUNT_MAX_PAUSE, 2.0 ** excess) - now)


def record_account_failure(key: str) -> None:
    now = time.time()
    with _fail_lock:
        fails, _ = _account_fails_locked(key, now)
        _accounts.pop(key, None)
        _accounts[key] = (fails + 1, now)
        while len(_accounts) > _MAX_ACCOUNTS:
            del _accounts[next(iter(_accounts))]


def record_account_success(key: str, ip: str) -> None:
    with _fail_lock:
        _accounts.pop(key, None)
        known = _known_ips.setdefault(key, {})
        known.pop(ip, None)
        known[ip] = None
        while len(known) > _KNOWN_IPS_PER_ACCOUNT:
            del known[next(iter(known))]
