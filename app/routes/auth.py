"""Sign in / out, session state, own password change."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import auth, config
from ..web import client_ip, current_user, require_uid

router = APIRouter()


class Login(BaseModel):
    username: str = Field("", max_length=80)  # empty = the owner (user 1)
    password: str = Field(max_length=1024)


class PasswordChange(BaseModel):
    current: str = Field("", max_length=1024)
    new: str = Field(min_length=8, max_length=1024)


def _throttle(ip: str) -> None:
    if auth.login_throttled(ip):
        raise HTTPException(429, "too many failed attempts — try again shortly")


@router.post("/api/auth/login")
def login(body: Login, request: Request):
    ip = client_ip(request)
    _throttle(ip)
    user = auth.authenticate(body.username, body.password)
    if user is None:
        auth.record_failure(ip)
        raise HTTPException(401, "incorrect user name or password")
    auth.clear_failures(ip)
    resp = JSONResponse({"ok": True, "user": auth.public_user(user)})
    resp.set_cookie(
        config.AUTH_COOKIE,
        auth.create_session(user["id"]),
        max_age=int(config.AUTH_SESSION_HOURS * 3600),
        httponly=True,
        samesite="lax",
        path="/",
        secure=config.PUBLIC_BASE.startswith("https"),
    )
    return resp


@router.post("/api/auth/password")
def change_password(body: PasswordChange, request: Request):
    ip = client_ip(request)
    _throttle(ip)
    uid = require_uid(request)
    if not auth.change_password(uid, body.current, body.new):
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
