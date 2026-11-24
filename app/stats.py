"""Model usage statistics: LLM + embedding token counts over recent days.

Reads the pre-aggregated ModelTokenStats table (one row per model/role/day,
written by app/ollama.py as calls happen) and rolls it up for the stats page.
Cheap at any history depth because the totals are already per-day.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from . import config, db

# roles written to ModelTokenStats: harness | verify | story | chat | brief | podcast | analysis | embedding
_ROLES = ("harness", "verify", "story", "chat", "brief", "podcast", "analysis", "embedding")
_EMBED_ROLES = ("embedding",)


def _day_series(conn, today: dt.date, days: int) -> list[dict[str, Any]]:
    """Per-day in/out tokens for the last ``days`` days (UTC), oldest first, gaps included.

    ``embed_tokens`` is the subset of the day's tokens that came from embedding calls
    (used to split the daily bar LLM vs embedding in the chart).
    """
    start = (today - dt.timedelta(days=days - 1)).isoformat()
    end = today.isoformat()
    rows = conn.execute(
        "SELECT day, SUM(in_tokens), SUM(out_tokens), "
        "SUM(CASE WHEN role = 'embedding' THEN in_tokens + out_tokens ELSE 0 END) "
        "FROM ModelTokenStats WHERE day BETWEEN ? AND ? GROUP BY day",
        (start, end),
    ).fetchall()
    by_day = {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0) for r in rows}
    out = []
    for i in range(days):
        d = (today - dt.timedelta(days=days - 1 - i)).isoformat()
        i_tok, o_tok, e_tok = by_day.get(d, (0, 0, 0))
        out.append({"day": d, "in_tokens": i_tok, "out_tokens": o_tok, "embed_tokens": e_tok,
                    "tokens": i_tok + o_tok})
    return out


def _role_totals(conn, start: str, end: str) -> dict[str, dict[str, int]]:
    """{role: {calls, in_tokens, out_tokens}} for the window."""
    rows = conn.execute(
        "SELECT role, SUM(calls), SUM(in_tokens), SUM(out_tokens) FROM ModelTokenStats "
        "WHERE day BETWEEN ? AND ? GROUP BY role", (start, end),
    ).fetchall()
    return {r[0]: {"calls": r[1] or 0, "in_tokens": r[2] or 0, "out_tokens": r[3] or 0}
            for r in rows}


def _model_totals(conn, start: str, end: str) -> list[dict[str, Any]]:
    """Per-model totals and roles used within the window, sorted by total tokens desc."""
    rows = conn.execute(
        "SELECT model, role, SUM(calls), SUM(in_tokens), SUM(out_tokens) FROM ModelTokenStats "
        "WHERE day BETWEEN ? AND ? GROUP BY model, role", (start, end),
    ).fetchall()
    by_model: dict[str, dict[str, Any]] = {}
    for model, role, calls, i_tok, o_tok in rows:
        entry = by_model.setdefault(model, {"model": model, "roles": [], "calls": 0, "in_tokens": 0, "out_tokens": 0})
        if role not in entry["roles"]:
            entry["roles"].append(role)
        entry["calls"] += calls or 0
        entry["in_tokens"] += i_tok or 0
        entry["out_tokens"] += o_tok or 0
    for entry in by_model.values():
        entry["roles"].sort(key=lambda r: _ROLES.index(r) if r in _ROLES else len(_ROLES))
    return sorted(by_model.values(), key=lambda m: (-(m["in_tokens"] + m["out_tokens"]), m["model"]))


def summary(days: int = 30) -> dict[str, Any]:
    """Token usage over the last ``days`` days, split LLM vs embedding and by role."""
    today = dt.datetime.now(dt.UTC).date()
    start = (today - dt.timedelta(days=days - 1)).isoformat()
    end = today.isoformat()
    llm_roles = tuple(r for r in _ROLES if r not in _EMBED_ROLES)
    with db.get_conn() as conn:
        series = _day_series(conn, today, days)
        roles = _role_totals(conn, start, end)
        models = _model_totals(conn, start, end)

    llm_models = sorted(m["model"] for m in models if any(r in llm_roles for r in m["roles"]))
    embed_models = sorted(m["model"] for m in models if any(r in _EMBED_ROLES for r in m["roles"]))

    def sub(roles_: tuple[str, ...]) -> dict[str, int]:
        return {
            "calls": sum(roles.get(r, {}).get("calls", 0) for r in roles_),
            "in_tokens": sum(roles.get(r, {}).get("in_tokens", 0) for r in roles_),
            "out_tokens": sum(roles.get(r, {}).get("out_tokens", 0) for r in roles_),
        }

    return {
        "days": days,
        "series": series,
        "roles": roles,
        "models": models,
        "llm": sub(llm_roles),
        "embedding": sub(_EMBED_ROLES),
        "llm_models": llm_models,
        "embed_models": embed_models,
        "chat_model": config.CHAT_MODEL,
        "embed_model": config.EMBED_MODEL,
    }
