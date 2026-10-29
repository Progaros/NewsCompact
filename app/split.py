"""Split over-merged stories while the pipeline is idle.

Article-level CONTINUING_DEVELOPMENT joins can chain a long-running saga into one
story (471: 330 articles mixing the diesel deal, peace talks and an EU summit). A
story with SPLIT_MIN_ARTICLES+ articles of which at least SPLIT_FAR_PAIR_SHARE of
the article pairs are unrelated (cosine < SPLIT_FAR_SIM) is regrouped by embedding:
articles join the group whose centroid they match best (>= SPLIT_GROUP_SIM, in time
order, then reassigned to the final centroids); groups under SPLIT_MIN_GROUP fold
into the nearest one and parts closer than SPLIT_PART_SIM rejoin. No LLM decides
the grouping.

The largest group keeps the story (id, votes, chat, share links, update facts);
every other group becomes a new story that inherits the read marks and is linked
to the others as related. Each part gets one full Prompt 2 rewrite, so at most
SPLIT_MAX_PER_RUN stories are split per run.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Callable

import httpx

from . import clustering, config, db, ollama, story

log = logging.getLogger("news.split")

Vector = list[float]
_GATE_SAMPLE = 120  # members (evenly spaced in time) used for the far-pair share

_dot = clustering.dot
_unit = clustering.normalize


def far_pair_share(vecs: list[Vector]) -> float:
    """Share of article pairs (unit vectors) that are unrelated (cosine < SPLIT_FAR_SIM)."""
    s = vecs[:: max(1, math.ceil(len(vecs) / _GATE_SAMPLE))]
    pairs = far = 0
    for i in range(len(s)):
        for j in range(i + 1, len(s)):
            pairs += 1
            far += _dot(s[i], s[j]) < config.SPLIT_FAR_SIM
    return far / pairs if pairs else 0.0


def group(vecs: list[Vector]) -> list[list[int]]:
    """Indices of ``vecs`` (unit vectors in time order) grouped by centroid similarity, largest first."""
    members: list[list[int]] = []
    sums: list[Vector] = []
    cents: list[Vector] = []
    for k, v in enumerate(vecs):
        sims = [_dot(v, c) for c in cents]
        best = max(range(len(sims)), key=sims.__getitem__, default=None)
        if best is not None and sims[best] >= config.SPLIT_GROUP_SIM:
            members[best].append(k)
            sums[best] = [a + b for a, b in zip(sums[best], v)]
            cents[best] = _unit(sums[best])
        else:
            members.append([k])
            sums.append(list(v))
            cents.append(list(v))
    for _ in range(5):  # reassign to the final centroids until every group is big enough
        order = sorted(range(len(members)), key=lambda i: -len(members[i]))
        keep = [i for i in order if len(members[i]) >= config.SPLIT_MIN_GROUP][: config.SPLIT_MAX_PARTS] or order[:1]
        regrouped: dict[int, list[int]] = {i: [] for i in keep}
        for k, v in enumerate(vecs):
            regrouped[max(keep, key=lambda i: _dot(v, cents[i]))].append(k)
        stable = len(keep) == len(members)
        members = [m for m in regrouped.values() if m]
        cents = [_unit([sum(col) for col in zip(*(vecs[k] for k in m))]) for m in members]
        if stable and all(len(m) >= config.SPLIT_MIN_GROUP for m in members):
            break
    # Facets of one event (riot reports vs. curfew reports: centroids ~0.87) stay together;
    # distinct events in one story sit at 0.4-0.75.
    while len(members) > 1:
        i, j = max(((i, j) for i in range(len(members)) for j in range(i + 1, len(members))),
                   key=lambda p: _dot(cents[p[0]], cents[p[1]]))
        if _dot(cents[i], cents[j]) < config.SPLIT_PART_SIM:
            break
        members[i] += members.pop(j)
        cents.pop(j)
        cents[i] = _unit([sum(col) for col in zip(*(vecs[k] for k in members[i]))])
    return sorted((sorted(m) for m in members), key=len, reverse=True)


def plan(conn: sqlite3.Connection, story_id: int) -> list[list[int]] | None:
    """Article ids per part (largest first) if ``story_id`` should be split, else None."""
    ids, vecs = [], []
    for (aid,) in conn.execute("SELECT id FROM Article WHERE story_id = ? AND duplicate_of IS NULL "
                               "ORDER BY created_at, id", (story_id,)).fetchall():
        v = clustering.get_vector(conn, aid)
        if v is not None:
            ids.append(aid)
            vecs.append(_unit(v))
    if len(ids) < config.SPLIT_MIN_ARTICLES or far_pair_share(vecs) < config.SPLIT_FAR_PAIR_SHARE:
        return None
    groups = group(vecs)
    if len(groups) < 2:
        return None
    # Articles without a vector stay with the largest part.
    return [[ids[k] for k in g] for g in groups]


def _create_part(conn: sqlite3.Connection, story_id: int, article_ids: list[int]) -> int:
    """New story for ``article_ids`` (and their copies) with the original's text until rewritten."""
    ids = json.dumps(article_ids)
    new_id = clustering.next_story_id(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO Story (id, headline, summary, tags, metrics, quality_score, created_at, updated_at, "
            "news_at, material_at, merge_checked_at, split_checked_at) "
            "SELECT ?, s.headline, s.summary, s.tags, s.metrics, s.quality_score, a.first, datetime('now'), "
            "MIN(COALESCE(s.news_at, a.last), a.last), "
            "CASE WHEN s.material_at IS NULL THEN NULL ELSE MIN(s.material_at, a.last) END, "
            "datetime('now'), datetime('now') "
            "FROM Story s, (SELECT MIN(created_at) AS first, MAX(created_at) AS last FROM Article "
            "WHERE id IN (SELECT value FROM json_each(?))) a WHERE s.id = ?",
            (new_id, ids, story_id))
        conn.execute("UPDATE Article SET story_id = ? WHERE story_id = ? AND (id IN (SELECT value FROM json_each(?)) "
                     "OR duplicate_of IN (SELECT value FROM json_each(?)))", (new_id, story_id, ids, ids))
        # Readers of the saga have read these parts too (they resurface on material updates).
        conn.execute("INSERT OR IGNORE INTO StoryRead (user_id, story_id, read_at) "
                     "SELECT user_id, ?, read_at FROM StoryRead WHERE story_id = ?", (new_id, story_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return new_id


def _undo_part(conn: sqlite3.Connection, story_id: int, part_id: int) -> None:
    conn.execute("UPDATE Article SET story_id = ? WHERE story_id = ?", (story_id, part_id))
    conn.execute("DELETE FROM Story WHERE id = ?", (part_id,))


def apply(conn: sqlite3.Connection, story_id: int, groups: list[list[int]]) -> list[int]:
    """Split ``story_id`` into ``groups`` (from ``plan``); returns the new story ids.

    A part whose rewrite fails goes back into the story; Ollama timeouts stop the split
    (re-raised after the parts done so far are linked).
    """
    new_ids: list[int] = []
    paused: Exception | None = None
    for part in groups[1:]:
        part_id = _create_part(conn, story_id, part)
        try:
            story.update_story(conn, part_id, force_full=True)
        except (httpx.TimeoutException, *ollama.UNAVAILABLE_ERRORS) as e:
            _undo_part(conn, story_id, part_id)
            paused = e
            break
        except Exception:  # noqa: BLE001 - this part stays in the story
            log.exception("rewrite of split part failed for story %s", story_id)
            _undo_part(conn, story_id, part_id)
            continue
        new_ids.append(part_id)
    if new_ids and paused is None:
        try:
            story.update_story(conn, story_id, force_full=True)
        except (httpx.TimeoutException, *ollama.UNAVAILABLE_ERRORS) as e:
            paused = e
        except Exception:  # noqa: BLE001 - the next joining article rewrites it
            log.exception("rewrite after split failed for story %s", story_id)
    parts = [story_id, *new_ids]
    for i, a in enumerate(parts):
        for b in parts[i + 1:]:
            # The merge pass never re-verifies split parts (a new article in one part would undo the split).
            clustering.link_stories(conn, a, b, "CONTINUING_DEVELOPMENT", "split")
    if paused is not None:
        raise paused
    return new_ids


def run(should_stop: Callable[[], bool] = lambda: False) -> dict[str, int]:
    result = {"checked": 0, "split": 0, "parts": 0}
    if not config.SPLIT_ENABLED or not ollama.available():
        return result
    with db.get_conn() as conn:
        todo = [r[0] for r in conn.execute(
            "SELECT s.id FROM Story s JOIN Article a ON a.story_id = s.id AND a.duplicate_of IS NULL "
            "WHERE s.headline IS NOT NULL AND (s.split_checked_at IS NULL OR s.split_checked_at < s.updated_at) "
            "GROUP BY s.id HAVING count(*) >= ? ORDER BY s.updated_at DESC", (config.SPLIT_MIN_ARTICLES,))]
    for sid in todo:
        if should_stop() or result["split"] >= config.SPLIT_MAX_PER_RUN:
            break
        with db.get_conn() as conn:
            result["checked"] += 1
            groups = plan(conn, sid)
            if groups:
                try:
                    new_ids = apply(conn, sid, groups)
                except (httpx.TimeoutException, *ollama.UNAVAILABLE_ERRORS) as e:
                    log.warning("split pass paused at story %s: %s", sid, type(e).__name__)
                    break
                if new_ids:
                    result["split"] += 1
                    result["parts"] += len(new_ids)
                    log.info("split story %s into %s (%s articles)", sid, [sid, *new_ids], [len(g) for g in groups])
            conn.execute("UPDATE Story SET split_checked_at = datetime('now') WHERE id = ?", (sid,))
    return result
