"""Story search: word matches (FTS5) fused with meaning matches (query embedding KNN).

- Words: every query word must occur in the story's headline, tags, summary or article
  titles (diacritics/case-insensitive, words of 3+ letters also as prefix). Complete list,
  ordered by bm25 — this is the filter everyone gets.
- Meaning (signed-in only, costs one Ollama call): the query is embedded with the
  Qwen3 query instruction, the nearest article vectors are mapped to their stories
  (best article per story), and only clearly similar ones are kept.
- Both rankings are merged with Reciprocal Rank Fusion. The embedding starts before the
  FTS query runs; it gets SEARCH_SEMANTIC_TIMEOUT, then the word matches are answered
  alone. Query vectors are cached (LRU) and at most SEARCH_EMBED_MAX_INFLIGHT embeddings
  run at once (identical queries share one), so typing can't queue up Ollama work.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from . import clustering, config, db, ollama

log = logging.getLogger("news.search")

_MAX_TERMS = 10
# bm25 column weights in table order: headline, tags, summary, titles.
_BM25 = f"bm25({db.SEARCH_TABLE}, 4.0, 2.0, 1.0, 1.5)"

_lock = threading.RLock()  # RLock: a finished future runs its done-callback inside add_done_callback
_cache: OrderedDict[str, list[float]] = OrderedDict()
_inflight: dict[str, Future] = {}
_pool = ThreadPoolExecutor(max_workers=max(1, config.SEARCH_EMBED_MAX_INFLIGHT), thread_name_prefix="search-embed")


def normalize(query: str) -> str:
    return " ".join(query.casefold().split())


def fts_query(query: str) -> str:
    """User text -> FTS5 query that can't carry operators: quoted words, all required."""
    terms = re.findall(r"\w+", query.casefold())[:_MAX_TERMS]
    return " AND ".join(f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in terms)


def _embed(text: str) -> list[float]:
    return ollama.embed_texts([f"Instruct: {config.SEARCH_INSTRUCT}\nQuery: {text}"],
                              timeout=config.SEARCH_EMBED_TIMEOUT)[0]


def _finished(key: str, fut: Future) -> None:
    with _lock:
        _inflight.pop(key, None)
        if fut.exception() is None:
            _cache[key] = fut.result()
            while len(_cache) > config.SEARCH_EMBED_CACHE:
                _cache.popitem(last=False)


def query_vector(key: str) -> Future | None:
    """Future of the query embedding (cached, shared, started now); None if all slots are busy."""
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            done: Future = Future()
            done.set_result(_cache[key])
            return done
        if key in _inflight:
            return _inflight[key]
        if len(_inflight) >= config.SEARCH_EMBED_MAX_INFLIGHT:
            return None
        fut = _pool.submit(_embed, key)
        _inflight[key] = fut
        fut.add_done_callback(lambda f: _finished(key, f))
        return fut


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def word_hits(conn: sqlite3.Connection, query: str, scope: str, params: list[Any]) -> list[int]:
    match = fts_query(query)
    if not match:
        return []
    t = db.SEARCH_TABLE
    rows = conn.execute(
        f"SELECT s.id FROM {t} JOIN Story s ON s.id = {t}.rowid WHERE {t} MATCH ? AND {scope} ORDER BY {_BM25}",
        (match, *params),
    )
    return [r[0] for r in rows]


def meaning_hits(conn: sqlite3.Connection, vec: list[float], scope: str,
                 params: list[Any]) -> list[tuple[int, float]]:
    """``[(story_id, best article cosine)]`` best first, above the similarity floor."""
    sims = dict(clustering.knn(conn, vec, config.SEARCH_KNN))
    if not sims:
        return []
    best: dict[int, float] = {}
    rows = conn.execute(
        f"SELECT a.id, s.id FROM Article a JOIN Story s ON s.id = a.story_id "
        f"WHERE a.id IN ({','.join('?' * len(sims))}) AND {scope}",
        (*sims, *params),
    )
    for aid, sid in rows:
        best[sid] = max(best.get(sid, -1.0), sims[aid])
    if not best:
        return []
    floor = max(config.SEARCH_MIN_SIM, max(best.values()) - config.SEARCH_SIM_MARGIN)
    hits = sorted(((sid, sim) for sid, sim in best.items() if sim >= floor), key=lambda x: -x[1])
    return hits[:config.SEARCH_SEMANTIC_MAX_STORIES]


def search(conn: sqlite3.Connection, query: str, *, archived: bool, cutoff: str,
           semantic: bool) -> dict[int, dict[str, Any]]:
    """Stories of the scope matching ``query``, best first: ``{id: {"score", "match"}}``.

    ``match`` is "words", "meaning" or "both"; ``score`` is the RRF value.
    """
    key = normalize(query)
    if not key:
        return {}
    started = time.monotonic()
    pending = query_vector(key) if semantic else None
    scope = "s.headline IS NOT NULL AND s.news_at " + ("< ?" if archived else ">= ?")
    words = word_hits(conn, key, scope, [cutoff])
    meaning: list[tuple[int, float]] = []
    note = "off" if not semantic else "busy" if pending is None else ""
    if pending is not None:
        try:
            vec = pending.result(timeout=max(0.0, config.SEARCH_SEMANTIC_TIMEOUT - (time.monotonic() - started)))
            meaning = meaning_hits(conn, vec, scope, [cutoff])
        except TimeoutError:
            note = "timeout"
        except Exception as e:  # noqa: BLE001 — Ollama trouble degrades to word matches
            note = f"error: {e}"
    k = config.SEARCH_RRF_K
    out: dict[int, dict[str, Any]] = {}
    for rank, sid in enumerate(words, 1):
        out[sid] = {"score": 1.0 / (k + rank), "match": "words"}
    for rank, (sid, sim) in enumerate(meaning, 1):
        hit = out.setdefault(sid, {"score": 0.0, "match": "meaning"})
        hit["score"] += config.SEARCH_SEMANTIC_WEIGHT / (k + rank)
        hit["similarity"] = round(sim, 3)
        if hit["match"] == "words":
            hit["match"] = "both"
    # No query text at INFO: what people search for is not logged by default.
    log.info("search: %d word + %d meaning -> %d stories in %.0f ms%s", len(words), len(meaning),
             len(out), (time.monotonic() - started) * 1000, f" (semantic {note})" if note else "")
    return dict(sorted(out.items(), key=lambda x: -x[1]["score"]))
