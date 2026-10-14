"""Pipeline timing report from news.db (read-only, stdlib only).

Answers "where does the time go between an article appearing and its story":
  wait     first seen (ingest) -> pipeline start (queue)
  work     per-article pipeline time (what /list's "Work" column shows)
  latency  first seen -> story written (wait + work)

Rows processed before ``processed_at`` existed are estimated from the
creating story's ``created_at`` (written right after embedding + clustering).

Usage: python scripts/pipeline_report.py [--db news.db] [--hours 24]
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from pathlib import Path


def pct(values: list[float], p: int) -> float:
    if len(values) < 2:
        return values[0] if values else float("nan")
    return statistics.quantiles(values, n=100)[p - 1]


def fmt(seconds: float) -> str:
    if seconds != seconds:  # NaN
        return "-"
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def line(label: str, values: list[float]) -> str:
    if not values:
        return f"  {label:<10} n=0"
    return (f"  {label:<10} n={len(values):<5} median {fmt(statistics.median(values)):>6}"
            f"  p90 {fmt(pct(values, 90)):>6}  max {fmt(max(values)):>6}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[1] / "news.db")
    ap.add_argument("--hours", type=float, default=24, help="window for the timing section")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    q = conn.execute
    since = f"-{args.hours} hours"
    has_processed = any(r[1] == "processed_at" for r in q("PRAGMA table_info(Article)"))
    processed_expr = "processed_at" if has_processed else "NULL"
    processed_a = "a.processed_at" if has_processed else "NULL"

    print(f"== Backlog ({args.db.name})")
    for status, n in q("""
        SELECT CASE
          WHEN a.story_id IS NOT NULL THEN 'assigned'
          WHEN COALESCE(a.pipeline_failures, 0) >= 3 THEN 'dead letter'
          WHEN f.enabled = 0 THEN 'disabled feed'
          WHEN a.content_class = 'broken' THEN 'broken body (never processed)'
          ELSE 'pending' END, count(*)
        FROM Article a JOIN Feed f ON f.id = a.feed_id GROUP BY 1 ORDER BY 2 DESC"""):
        print(f"  {status:<32} {n}")

    # Same opening under many titles = extraction picked up site chrome (sidebar, disclaimer, paywall).
    print("\n== Repeated body openings (one feed, >= 3 titles, last 48h of ingests)")
    repeated = q("""
        SELECT f.title, count(DISTINCT a.title), substr(a.content_raw, 1, 70) FROM Article a
        JOIN Feed f ON f.id = a.feed_id
        WHERE a.content_raw != '' AND a.created_at >= (SELECT datetime(max(created_at), '-48 hours') FROM Article)
        GROUP BY a.feed_id, substr(a.content_raw, 1, 120) HAVING count(DISTINCT a.title) >= 3
        ORDER BY 2 DESC""").fetchall()
    for feed, n, head in repeated:
        print(f"  {n:>3}x  {feed[:30]:<30}  {head!r}")
    if not repeated:
        print("  none")

    rows = q(f"""
        SELECT (julianday({processed_a}) - julianday(a.created_at)) * 86400, a.t_wall_s
        FROM Article a
        WHERE {processed_a} IS NOT NULL AND a.t_wall_s IS NOT NULL
          AND a.created_at >= (SELECT datetime(max(created_at), ?) FROM Article)""",
             (since,)).fetchall()
    source = "processed_at"
    if not rows:
        source = "estimated from Story.created_at (pre-processed_at rows)"
        rows = q("""
            SELECT (julianday(s.created_at) - julianday(a.created_at)) * 86400
                     + COALESCE(a.t_harness_s, 0) + COALESCE(a.t_story_s, 0), a.t_wall_s
            FROM Article a JOIN Story s ON s.id = a.story_id
            WHERE a.t_wall_s IS NOT NULL
              AND a.id = (SELECT min(id) FROM Article WHERE story_id = s.id)
              AND a.created_at >= (SELECT datetime(max(created_at), ?) FROM Article)""",
                 (since,)).fetchall()
    latency = [r[0] for r in rows if r[0] is not None and r[0] >= 0]
    work = [r[1] for r in rows]
    wait = [max(0.0, lat - w) for lat, w in rows if lat is not None and lat >= 0]
    print(f"\n== Timing, last {args.hours:g}h of ingests ({source})")
    print(line("wait", wait))
    print(line("work", work))
    print(line("latency", latency))
    if latency and work:
        share = statistics.median(work) / max(statistics.median(latency), 1e-9)
        print(f"  -> /list 'Work' covers ~{share:.0%} of the median ingest->story latency; "
              "the rest is queue wait.\n     Before an article is even seen it also waits up to "
              "NEWS_WORKER_INTERVAL for the next feed poll.")

    print("\n== Stage medians (work)")
    stages = q("""SELECT t_embed_s, t_cluster_s, t_harness_s, t_story_s FROM Article
                  WHERE t_wall_s IS NOT NULL
                    AND created_at >= (SELECT datetime(max(created_at), ?) FROM Article)""", (since,)).fetchall()
    if not stages:
        stages = q("SELECT t_embed_s, t_cluster_s, t_harness_s, t_story_s FROM Article "
                   "WHERE t_wall_s IS NOT NULL ORDER BY id DESC LIMIT 200").fetchall()
    for i, name in enumerate(("embed", "cluster", "harness", "story")):
        print(line(name, [r[i] for r in stages if r[i] is not None]))

    print("\n== Writes per hour (UTC): articles ingested / stories created / articles processed")
    hours = q(f"""
        WITH h AS (SELECT substr(created_at, 1, 13) AS hr, count(*) AS n FROM Article
                   WHERE created_at >= datetime((SELECT max(created_at) FROM Article), '-24 hours') GROUP BY 1)
        SELECT h.hr, h.n,
               (SELECT count(*) FROM Story WHERE substr(created_at, 1, 13) = h.hr),
               (SELECT count(*) FROM Article WHERE substr({processed_expr}, 1, 13) = h.hr)
        FROM h ORDER BY h.hr""").fetchall()
    for hr, n_art, n_story, n_proc in hours:
        print(f"  {hr}:00  {n_art:>4}  {n_story:>4}  {n_proc:>4}")


if __name__ == "__main__":
    main()
