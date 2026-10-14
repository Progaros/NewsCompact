"""Recall merge: fold duplicate stories together while the pipeline is idle.

Clustering favours precision, so the same event often ends up as two stories
(1 126 of 1 259 stories were single-article). After the queue drains, a few
recently updated, not-yet-checked stories are compared with their nearest
neighbour stories (vec0 KNN over their anchors); candidates above
MERGE_SIM_THRESHOLD go through the same event verifier as new articles.
Spend per run is capped at MERGE_MAX_CHECKS_PER_RUN verifier calls plus one
Prompt 2 rewrite per merge.

Embeddings miss cross-language and title-only copies (a German body vs. an
English headline), so stories that share several distinctive headline terms
(names, numbers — rare within the window) are verified first as well.
"""
from __future__ import annotations

import collections
import logging
import re
import sqlite3
import time
from typing import Callable

from . import clustering, config, db, harness, ollama, story

log = logging.getLogger("news.merge")

_WORD = re.compile(r"[^\W_]+(?:[-.][^\W_]+)*")
_STOPWORDS = frozenset("""
with from that this have will been were their they them into over after amid about under than more
most some what when where which while your other also just only very says said
über unter nicht eine einer eines einem einen dass sich auch nach beim vom zum zur mehr wird werden
""".split())


def headline_terms(headline: str | None) -> set[str]:
    """Content words of a headline: 4+ letters, or anything with a digit ("GLM-5.3", "2026")."""
    return {w for w in _WORD.findall((headline or "").casefold())
            if w not in _STOPWORDS and (len(w) >= 4 or (len(w) >= 2 and any(c.isdigit() for c in w)))}


class _TermIndex:
    """Distinctive headline terms (document frequency <= MERGE_LEXICAL_MAX_DF) of the window's stories."""

    def __init__(self, conn: sqlite3.Connection, since: str):
        rows = conn.execute("SELECT id, headline FROM Story WHERE headline IS NOT NULL AND updated_at >= ?",
                            (since,)).fetchall()
        terms = {r["id"]: headline_terms(r["headline"]) for r in rows}
        df = collections.Counter(w for ws in terms.values() for w in ws)
        self.terms = {sid: {w for w in ws if df[w] <= config.MERGE_LEXICAL_MAX_DF} for sid, ws in terms.items()}
        self.by_term: dict[str, set[int]] = collections.defaultdict(set)
        for sid, ws in self.terms.items():
            for w in ws:
                self.by_term[w].add(sid)

    def candidates(self, story_id: int, headline: str | None) -> set[int]:
        mine = self.terms.get(story_id) or {w for w in headline_terms(headline) if w in self.by_term}
        shared = collections.Counter(sid for w in mine for sid in self.by_term[w] if sid != story_id)
        return {sid for sid, n in shared.items() if n >= config.MERGE_LEXICAL_MIN_TERMS}


def _absorb(conn: sqlite3.Connection, keep: int, drop: int) -> list[int]:
    """Move everything from story ``drop`` into ``keep`` and delete ``drop``."""
    moved = [r[0] for r in conn.execute("SELECT id FROM Article WHERE story_id = ?", (drop,))]
    conn.execute("UPDATE Article SET story_id = ? WHERE story_id = ?", (keep, drop))
    conn.execute("UPDATE FeedbackLog SET story_id = ? WHERE story_id = ?", (keep, drop))
    if conn.execute("SELECT 1 FROM ChatSession WHERE story_id = ?", (keep,)).fetchone() is None:
        conn.execute("UPDATE ChatSession SET story_id = ? WHERE story_id = ?", (keep, drop))
    conn.execute("UPDATE Story SET news_at = MAX(COALESCE(news_at, ''), "
                 "COALESCE((SELECT news_at FROM Story WHERE id = ?), '')) WHERE id = ?", (drop, keep))
    conn.execute("DELETE FROM Story WHERE id = ?", (drop,))
    return moved


def _size(conn: sqlite3.Connection, story_id: int) -> int:
    return conn.execute("SELECT count(*) FROM Article WHERE story_id = ?", (story_id,)).fetchone()[0]


def _check(conn: sqlite3.Connection, s: sqlite3.Row, since: str,
           index: _TermIndex) -> tuple[bool, int | None, int | None]:
    """Returns (verifier_called, keep, drop)."""
    anchors = clustering.AnchorCache(conn)
    best: dict[int, float] = {}
    for vec in anchors(s["id"])[: config.MERGE_ANCHORS_PER_STORY]:
        for sid, sim in clustering.nearest_stories(conn, vec, anchors, since=since, exclude=s["id"]):
            best[sid] = max(best.get(sid, -1.0), sim)
    lexical = frozenset(sid for sid in index.candidates(s["id"], s["headline"])
                        if conn.execute("SELECT 1 FROM Story WHERE id = ?", (sid,)).fetchone())
    cands = [(sid, sim) for sid, sim in best.items() if sim >= config.MERGE_SIM_THRESHOLD]
    cands += [(sid, best.get(sid, 0.0)) for sid in lexical if best.get(sid, 0.0) < config.MERGE_SIM_THRESHOLD]
    if not cands:
        return False, None, None
    target, _ = clustering.verify(conn, s["headline"] or "", s["summary"] or "", cands,
                                  config.MERGE_SIM_THRESHOLD, f"story={s['id']}",
                                  accept=harness.STORY_MERGE_CLASSES, priority=lexical)
    if target is None:
        return True, None, None
    # Keep the bigger story (older on ties) so its id / chat / votes survive.
    a, b = (s["id"], _size(conn, s["id"])), (target, _size(conn, target))
    keep, drop = (a, b) if (a[1], -a[0]) > (b[1], -b[0]) else (b, a)
    return True, keep[0], drop[0]


def run(should_stop: Callable[[], bool] = lambda: False) -> dict[str, int]:
    result = {"checked": 0, "verified": 0, "merged": 0}
    if not config.MERGE_ENABLED or config.CLUSTER_MODE != "two_stage" or not ollama.available():
        return result
    since = clustering.window_cutoff()
    with db.get_conn() as conn:
        todo = conn.execute(
            "SELECT id FROM Story WHERE headline IS NOT NULL AND updated_at >= ? "
            "AND (merge_checked_at IS NULL OR merge_checked_at < updated_at) "
            "ORDER BY updated_at DESC LIMIT ?",
            (since, config.MERGE_MAX_CHECKS_PER_RUN * 4),
        ).fetchall()
        index = _TermIndex(conn, since)
    for row in todo:
        if should_stop() or result["verified"] >= config.MERGE_MAX_CHECKS_PER_RUN:
            break
        t0 = time.perf_counter()
        with db.get_conn() as conn:
            s = conn.execute("SELECT id, headline, summary FROM Story WHERE id = ?", (row["id"],)).fetchone()
            if s is None:  # absorbed earlier in this run
                continue
            result["checked"] += 1
            verified, keep, drop = _check(conn, s, since, index)
            result["verified"] += int(verified)
            conn.execute("UPDATE Story SET merge_checked_at = datetime('now') WHERE id = ?", (s["id"],))
            if keep is None:
                continue
            moved = _absorb(conn, keep, drop)
            try:
                story.update_story(conn, keep, force_full=True)
            except Exception:  # noqa: BLE001 - merged anyway; next article updates the summary
                log.exception("prompt2 after merge failed for story %s", keep)
            secs = time.perf_counter() - t0
            for aid in moved:
                conn.execute("UPDATE Article SET t_merge_s = ? WHERE id = ?", (secs, aid))
            result["merged"] += 1
            log.info("merged story %s into %s (%d articles, %.1fs)", drop, keep, len(moved), secs)
    return result
