"""Calibrate story search on real data: what the meaning side finds and how RRF merges it.

For each query prints the nearest stories by meaning (unfiltered, with cosine, "W" = also a
word match, "*" = kept by SEARCH_MIN_SIM / SEARCH_SIM_MARGIN) and the fused top list.
Set NEWS_SEARCH_MIN_SIM / NEWS_SEARCH_SIM_MARGIN to where relevant stops and noise starts.

Run on the server:  uv run python scripts/search_probe.py "merz zölle" "EU chip subsidies" [--archived]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, db, search  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--archived", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=config.STORY_ACTIVE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    scope = "s.headline IS NOT NULL AND s.news_at " + ("< ?" if args.archived else ">= ?")
    with db.get_conn() as conn:
        headline = lambda sid: conn.execute("SELECT headline FROM Story WHERE id = ?", (sid,)).fetchone()[0]  # noqa: E731
        for q in args.queries:
            key = search.normalize(q)
            t = time.monotonic()
            vec = search._embed(key)
            embed_ms = (time.monotonic() - t) * 1000
            words = set(search.word_hits(conn, key, scope, [cutoff]))
            with config_override(SEARCH_MIN_SIM=-1.0, SEARCH_SIM_MARGIN=2.0, SEARCH_SEMANTIC_MAX_STORIES=args.top):
                near = search.meaning_hits(conn, vec, scope, [cutoff])
            floor = max(config.SEARCH_MIN_SIM, (near[0][1] if near else 0) - config.SEARCH_SIM_MARGIN)
            print(f"\n=== {q!r}  embed {embed_ms:.0f} ms, {len(words)} word matches, floor {floor:.3f}")
            for sid, sim in near:
                print(f"  {sim:.3f} {'*' if sim >= floor else ' '}{'W' if sid in words else ' '} {sid:>6} {headline(sid)[:90]}")
            search._cache[key] = vec
            fused = search.search(conn, q, archived=args.archived, cutoff=cutoff, semantic=True)
            print("  -- fused (before priority boost):")
            for sid, hit in list(fused.items())[:args.top]:
                print(f"  {hit['score']:.4f} {hit['match']:<7} {sid:>6} {headline(sid)[:90]}")


@contextlib.contextmanager
def config_override(**values):
    saved = {k: getattr(config, k) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


if __name__ == "__main__":
    main()
