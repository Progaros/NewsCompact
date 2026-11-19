"""User administration (admins only, enforced by the middleware in app/api.py)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import db, tag_merge, users
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


class AliasReview(BaseModel):
    alias: str = Field(min_length=1, max_length=120)
    canonical: str = Field(min_length=1, max_length=120)
    status: Literal["approved", "rejected"]


@router.get("/api/admin/tag-aliases")
def list_tag_aliases(status: Literal["pending", "approved", "rejected"] = "pending",
                     limit: int = Query(50, ge=1, le=200)):
    with db.get_conn() as conn:
        counts = dict(conn.execute("SELECT status, count(*) FROM TagAlias GROUP BY status"))
        rows = [
            dict(r) for r in conn.execute(
                "SELECT alias, canonical, status, similarity, relation, confidence, method, created_at "
                "FROM TagAlias WHERE status = ? ORDER BY COALESCE(confidence, 0) DESC, COALESCE(similarity, 0) DESC "
                "LIMIT ?", (status, limit),
            )
        ]
    return {
        "counts": {k: counts.get(k, 0) for k in ("pending", "approved", "rejected")},
        "aliases": rows,
    }


@router.post("/api/admin/tag-aliases/review")
def review_tag_alias(body: AliasReview):
    with db.get_conn() as conn:
        try:
            changed = tag_merge.review(conn, body.alias, body.status, body.canonical)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
    if not changed:
        raise HTTPException(404, "tag alias not found")
    return {"ok": True, "changed": changed}
