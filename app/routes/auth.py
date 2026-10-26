"""Sign in / out, session state, own password change.

Failed and throttled sign-ins are logged as ``login failed ip=<ip> ...`` (fail2ban-friendly).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import auth, config
from ..web import client_ip, current_user, is_https, require_uid

router = APIRouter()
log = logging.getLogger("news.auth")


class Login(BaseModel):
    username: str = Field("", max_length=80)  # empty = the owner (user 1)
    password: str = Field(max_length=1024)


class PasswordChange(BaseModel):
    current: str = Field("", max_length=1024)
    new: str = Field(min_length=8, max_length=1024)


def _throttle(ip: str, what: str) -> None:
    if auth.login_throttled(ip):
        log.warning("%s throttled ip=%s reason=ip", what, ip)
        raise HTTPException(429, "too many failed attempts — try again shortly")


@router.post("/api/auth/login")
def login(body: Login, request: Request):
    ip = client_ip(request)
    _throttle(ip, "login")
    account = auth.account_key(body.username)
    wait = auth.account_wait(account, ip)
    if wait > 0:
        log.warning("login throttled ip=%s reason=account user=%r wait=%ds", ip, account, wait)
        raise HTTPException(429, "too many failed attempts — try again shortly",
                            headers={"Retry-After": str(int(wait) + 1)})
    user = auth.authenticate(body.username, body.password)
    if user is None:
        log.warning("login failed ip=%s user=%r", ip, account)
        auth.record_failure(ip)
        auth.record_account_failure(account)
        raise HTTPException(401, "incorrect user name or password")
    log.info("login ok ip=%s user=%r", ip, user["name"])
    auth.clear_failures(ip)
    auth.record_account_success(account, ip)
    resp = JSONResponse({"ok": True, "user": auth.public_user(user)})
    resp.set_cookie(
        config.AUTH_COOKIE,
        auth.create_session(user["id"]),
        max_age=int(config.AUTH_SESSION_HOURS * 3600),
        httponly=True,
        samesite="lax",
        path="/",
        secure=is_https(request),
    )
    return resp


@router.post("/api/auth/password")
def change_password(body: PasswordChange, request: Request):
    ip = client_ip(request)
    _throttle(ip, "password change")
    uid = require_uid(request)
    if not auth.change_password(uid, body.current, body.new):
        log.warning("password change failed ip=%s user_id=%s", ip, uid)
        auth.record_failure(ip)
        raise HTTPException(403, "current password is wrong")
    auth.end_user_sessions(uid, keep=request.cookies.get(config.AUTH_COOKIE))
    return {"ok": True}


@router.post("/api/auth/logout")
def logout(request: Request):
    auth.destroy_session(request.cookies.get(config.AUTH_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.AUTH_COOKIE, path="/")
    return resp


@router.get("/api/auth/session")
def session(request: Request):
    user = current_user(request)
    return {"authenticated": user is not None, "user": user}
