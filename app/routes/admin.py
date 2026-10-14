"""User administration (admins only, enforced by the middleware in app/api.py)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import users
from ..web import require_uid

router = APIRouter()

_USER_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{1,39}$"


class UserCreate(BaseModel):
    name: str = Field(pattern=_USER_NAME)
    password: str = Field(min_length=8, max_length=1024)
    is_admin: bool = False


class UserUpdate(BaseModel):
    is_admin: bool | None = None
    password: str | None = Field(None, min_length=8, max_length=1024)


@router.get("/api/admin/users")
def list_users():
    return {"users": users.list_users()}


@router.post("/api/admin/users")
def create_user(body: UserCreate):
    try:
        return {"users": users.create(body.name, body.password, body.is_admin)}
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@router.patch("/api/admin/users/{user_id}")
def update_user(user_id: int, body: UserUpdate, request: Request):
    try:
        result = users.update(user_id, require_uid(request), is_admin=body.is_admin, password=body.password)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    if result is None:
        raise HTTPException(404, "user not found")
    return {"users": result}


@router.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, request: Request):
    try:
        result = users.delete(user_id, require_uid(request))
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    if result is None:
        raise HTTPException(404, "user not found")
    return {"users": result}
