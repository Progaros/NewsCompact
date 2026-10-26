"""Compare "For you" orders under several update-ranking variants (read-only, in memory).

Each variant replays every active story's StoryUpdate history with its own freshness refresh
rule (how far a minor / notable / major update makes a story new again) and ranks with its
own velocity / boost / read-state / variety settings, using your real profile. Prints the top
of each list plus a few numbers to judge it:

  minor   stories in the top whose only news in the last 24 h were minor (significance 1)
  age     median hours since the last news that counted
  dupes   pairs of near-duplicate topics in the top 10 (story vectors / StoryLink)
  moved   how many of the top differ from the current configuration

Run on the server:  uv run python scripts/compare_rankings.py [--user 1] [--top 20] [--show-read]
"""
from __future__ import annotations

import argparse
import contextlib
import statistics
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, db, diversity, scoring  # noqa: E402

_LINEAR = {"1": 1 / 3, "2": 2 / 3, "3": 1.0}
_NO_READER = {"read": 1.0, "updated": 1.0, "updated_major": 1.0}
# name -> (description, freshness refresh share per significance, use publication time, config overrides)
VARIANTS: dict[str, tuple[str, dict[str, float], bool, dict[str, Any]]] = {
    "before": (("v2: every update makes the story brand new (processing time), linear velocity, "
                "read state ignored, no variety rerank"),
               {"1": 1.0, "2": 1.0, "3": 1.0}, False,
               {"VELOCITY_SIG_WEIGHT": _LINEAR, "BOOST_SIG_WEIGHT": _LINEAR,
                "READER_FACTOR": _NO_READER, "DIVERSITY_LAMBDA": 1.0}),
    "gentle": ("minor updates refresh 25 %, notable 70 %",
               {"1": 0.25, "2": 0.7, "3": 1.0}, True, {}),
    "current": ("v2.1 defaults (minor 10 %, notable 50 %, major 100 %)",
                dict(config.FRESHNESS_REFRESH), True, {}),
    "strict": ("minor updates don't count at all (no refresh, velocity or boost)",
               {"1": 0.0, "2": 0.5, "3": 1.0}, True,
               {"VELOCITY_SIG_WEIGHT": {**config.VELOCITY_SIG_WEIGHT, "1": 0.0},
                "BOOST_SIG_WEIGHT": {**config.BOOST_SIG_WEIGHT, "1": 0.0}}),
}


@contextlib.contextmanager
def _overrides(values: dict[str, Any], news_at: dict[int, str]) -> Iterator[None]:
    saved = {k: getattr(config, k) for k in values}
    news_time = scoring.news_time
    try:
        for k, v in values.items():
            setattr(config, k, v)
        scoring.news_time = lambda s: news_at.get(s["id"]) or news_time(s)
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
        scoring.news_time = news_time


def replay_news_at(conn, refresh: dict[str, float], use_event_time: bool) -> dict[int, str]:
    """``{story_id: news_at}`` as if every StoryUpdate had been applied with ``refresh``."""
    out: dict[int, str] = {}
    created_at = dict(conn.execute("SELECT id, created_at FROM Story WHERE headline IS NOT NULL"))
    saved = config.FRESHNESS_REFRESH
    config.FRESHNESS_REFRESH = refresh
    try:
        for sid, sig, created, event in conn.execute(
            "SELECT story_id, significance, created_at, event_at FROM StoryUpdate ORDER BY id"
        ):
            if sid not in created_at:
                continue
            out.setdefault(sid, created_at[sid])
            at = (event if use_event_time and event else created) or out[sid]
            out[sid] = scoring.refreshed_news_at(out[sid], at, sig)
    finally:
        config.FRESHNESS_REFRESH = saved
    return out


def rank(conn, name: str, user_id: int, show_read: bool) -> list[dict[str, Any]]:
    """The "For you" list as /api/stories builds it, under variant ``name``."""
    _, refresh, event_time, values = VARIANTS[name]
    with _overrides(values, replay_news_at(conn, refresh, event_time)):
        items = scoring.ranked_stories(conn, personal=True, user_id=user_id, diverse=False)
        if not show_read:
            items = [i for i in items if i["read"] != "read"]
        return diversity.rerank(conn, items)


def _recent_updates(conn, ids: list[int]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {sid: [] for sid in ids}
    for sid, sig in conn.execute(
        "SELECT story_id, significance FROM StoryUpdate WHERE COALESCE(event_at, created_at) >= "
        "datetime('now', '-24 hours')"
    ):
        if sid in out:
            out[sid].append(sig)
    return out


def summarize(conn, items: list[dict[str, Any]], top: int, reference: list[int] | None = None) -> dict[str, Any]:
    head = items[:top]
    ids = [i["story"]["id"] for i in head]
    recent = _recent_updates(conn, ids)
    first10 = ids[:10]
    vectors = diversity.story_vectors(conn, first10)
    links = diversity.link_similarity(conn, first10)
    dupes = 0
    for n, a in enumerate(first10):
        for b in first10[n + 1:]:
            va, vb = vectors.get(a), vectors.get(b)
            cos = sum(x * y for x, y in zip(va, vb)) if va is not None and vb is not None else 0.0
            dupes += int(cos >= 0.8 or links.get((min(a, b), max(a, b)), 0.0) >= 0.75)
    return {
        "minor": sum(1 for sid in ids if recent[sid] and max(recent[sid]) == 1),
        "age": round(statistics.median(i["relevance"]["age_h"] for i in head), 1) if head else 0.0,
        "dupes": dupes,
        "moved": len(set(ids) - set(reference)) if reference is not None else 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", type=int, default=1)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--show-read", action="store_true", help='rank as with "Show read" on')
    args = ap.parse_args()

    with db.get_conn() as conn:
        lists = {name: rank(conn, name, args.user, args.show_read) for name in VARIANTS}
        reference = [i["story"]["id"] for i in lists["current"][:args.top]]
        stats = {name: summarize(conn, items, args.top, reference) for name, items in lists.items()}
        recent = _recent_updates(conn, [i["story"]["id"] for items in lists.values() for i in items[:args.top]])
    for name, items in lists.items():
        print(f"\n=== {name}: {VARIANTS[name][0]}")
        for n, i in enumerate(items[:args.top], 1):
            sigs = recent.get(i["story"]["id"]) or []
            news = f"24h:{','.join(map(str, sigs))}" if sigs else "24h:-"
            read = {"read": "R", "updated": "U"}.get(i["read"] or "", " ")
            print(f"{n:3d} {i['display']:5.1f} {i['relevance']['age_h']:5.1f}h {news:<12} {read} "
                  f"{i['story']['headline'][:90]}")
    print(f"\n{'variant':<9} {'minor':>5} {'age h':>6} {'dupes':>5} {'moved':>5}")
    for name, s in stats.items():
        print(f"{name:<9} {s['minor']:>5} {s['age']:>6} {s['dupes']:>5} {s['moved']:>5}")


if __name__ == "__main__":
    main()
