"""Tag synonym merging: multilingual embeddings find candidates, the LLM decides.

    candidate tags → embeddings (cached in TagVector) → cosine neighbours → LLM verifier → TagAlias

Embeddings only retrieve pairs; a merge needs the verifier's SAME_CONCEPT verdict
(BROADER / NARROWER / RELATED never merge: "ai" ≠ "generative ai", "germany" ≠
"german politics"). Very similar, confidently judged pairs are approved at once, other
SAME_CONCEPT pairs wait for review (scripts/merge_tags.py); every other verdict is stored as
rejected so the pair isn't asked again. Approved aliases apply at read time
(tags.normalize_tag), so stored stories and tag lists follow without being rewritten.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any

import sqlite_vec

from . import config, db, ollama, tags

log = logging.getLogger("news.tag-merge")

# Qwen3-Embedding instruction format; the same text on both sides keeps the space symmetric.
_INSTRUCT = ("Instruct: Given a news topic tag, retrieve tags that name the same concept "
             "in English or German\nQuery: ")
RELATIONS = ("SAME_CONCEPT", "BROADER", "NARROWER", "RELATED", "AMBIGUOUS", "UNRELATED")
_SCHEMA = {
    "type": "object",
    "properties": {
        "relation": {"type": "string", "enum": list(RELATIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "canonical": {"type": "string", "enum": ["A", "B"]},
    },
    "required": ["relation", "confidence", "canonical"],
}
_SYSTEM = (
    "You maintain the topic tags of a news app with English and German stories. Decide how "
    "tag A relates to tag B. SAME_CONCEPT only when both name exactly the same thing (a "
    "translation, spelling variant or abbreviation), so a reader who follows one always wants "
    "the other: 'klimawandel'/'climate change', 'ki'/'ai', 'munchen'/'munich'. A qualifier only "
    "keeps SAME_CONCEPT when both still name the identical thing ('trump'/'donald trump'); "
    "otherwise the longer tag is NARROWER ('storage'/'data storage', 'bayern'/'bayern munich', "
    "'wahlkampf'/'election'). BROADER: A is "
    "more general than B. NARROWER: A is more specific than B ('generative ai' vs 'ai', "
    "'german politics' vs 'germany'). RELATED: connected but different ('economy'/'recession', "
    "'microsoft'/'azure', 'climate change'/'weather', 'medicine'/'medication', "
    "'journalist'/'journalism', 'telekom'/'telecom'). AMBIGUOUS: a tag has several meanings "
    "('bayern' = the state or the football club). Otherwise UNRELATED. canonical: which of the "
    "two is the better shared name, preferring the English one. Respond with JSON only."
)


def candidate_tags(conn: sqlite3.Connection) -> dict[str, int]:
    """``{tag: story count}`` of tags on ≥ TAG_MERGE_MIN_STORIES stories or on any tag list."""
    counts: dict[str, int] = {}
    for (raw,) in conn.execute("SELECT tags FROM Story WHERE headline IS NOT NULL"):
        for t in tags.normalize_tags([t for t in db.json_list(raw) if isinstance(t, str)]):
            counts[t] = counts.get(t, 0) + 1
    listed: set[str] = set()
    for (raw,) in conn.execute("SELECT tag_weights FROM UserProfile"):
        listed.update(tags.merge_weights(db.json_dict(raw)))
    return {t: counts.get(t, 0) for t in {*counts, *listed}
            if counts.get(t, 0) >= config.TAG_MERGE_MIN_STORIES or t in listed}


def ensure_vectors(conn: sqlite3.Connection, names: list[str], batch: int = 64) -> dict[str, bytes]:
    """Packed embeddings of ``names``; only tags not in TagVector yet are embedded."""
    have = {t: blob for t, blob in conn.execute("SELECT tag, embedding FROM TagVector")}
    missing = [t for t in names if t not in have]
    for i in range(0, len(missing), batch):
        chunk = missing[i:i + batch]
        rows = [(t, db.pack_floats(v)) for t, v in zip(chunk, ollama.embed_texts([_INSTRUCT + t for t in chunk]))]
        conn.executemany("INSERT OR REPLACE INTO TagVector (tag, embedding) VALUES (?, ?)", rows)
        have.update(rows)
    return {t: have[t] for t in names}


def neighbour_pairs(blobs: dict[str, bytes], min_sim: float, k: int = 5) -> list[tuple[str, str, float]]:
    """``(a, b, cosine)`` for each tag's k nearest neighbours at ≥ ``min_sim``, most similar first."""
    if len(blobs) < 2:
        return []
    mem = sqlite3.connect(":memory:")
    mem.enable_load_extension(True)
    sqlite_vec.load(mem)
    mem.enable_load_extension(False)
    names = list(blobs)
    dim = len(blobs[names[0]]) // 4
    mem.execute(f"CREATE VIRTUAL TABLE v USING vec0(id INTEGER PRIMARY KEY, embedding FLOAT[{dim}] distance_metric=cosine)")
    mem.executemany("INSERT INTO v (id, embedding) VALUES (?, ?)", [(i, blobs[n]) for i, n in enumerate(names)])
    best: dict[tuple[str, str], float] = {}
    for i, name in enumerate(names):
        for j, dist in mem.execute("SELECT id, distance FROM v WHERE embedding MATCH ? AND k = ?", (blobs[name], k + 1)):
            sim = 1.0 - dist
            if j != i and sim >= min_sim:
                key = (min(name, names[j]), max(name, names[j]))
                best[key] = max(best.get(key, 0.0), sim)
    mem.close()
    return sorted(((a, b, s) for (a, b), s in best.items()), key=lambda p: -p[2])


def _examples(conn: sqlite3.Connection, wanted: set[str], per_tag: int = 2) -> dict[str, list[str]]:
    """A few recent headlines per tag, so the verifier sees what each tag is used for."""
    out: dict[str, list[str]] = {t: [] for t in wanted}
    for headline, raw in conn.execute("SELECT headline, tags FROM Story WHERE headline IS NOT NULL "
                                      "ORDER BY news_at DESC LIMIT 5000"):
        for t in tags.normalize_tags([t for t in db.json_list(raw) if isinstance(t, str)]):
            if t in out and len(out[t]) < per_tag:
                out[t].append(headline[:140])
    return out


def verify(a: str, b: str, examples: dict[str, list[str]]) -> dict[str, Any]:
    """The LLM's ``{relation, confidence, canonical}`` for tags ``a`` / ``b``."""
    def block(name: str, tag: str) -> str:
        lines = "\n".join(f"- {h}" for h in examples.get(tag, [])) or "- (no example)"
        return f"Tag {name}: {tag}\nExample headlines:\n{lines}"

    out = ollama.structured(_SCHEMA, [{"role": "system", "content": _SYSTEM},
                                      {"role": "user", "content": f"{block('A', a)}\n\n{block('B', b)}"}],
                            num_predict=200, timeout=config.CLUSTER_VERIFY_TIMEOUT_SECONDS)
    relation = out.get("relation") if out.get("relation") in RELATIONS else "AMBIGUOUS"
    try:
        confidence = max(0.0, min(1.0, float(out.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    canonical = a if out.get("canonical") == "A" else b
    return {"relation": relation, "confidence": confidence, "canonical": canonical}


def _decided(conn: sqlite3.Connection) -> set[frozenset[str]]:
    return {frozenset(r) for r in conn.execute("SELECT alias, canonical FROM TagAlias")}


def run(*, apply: bool = True, max_verify: int | None = None,
        should_stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """One merge pass. ``apply=False`` asks the LLM but writes nothing (dry run)."""
    limit = config.TAG_MERGE_MAX_VERIFY if max_verify is None else max_verify
    result: dict[str, Any] = {"candidates": 0, "pairs": 0, "approved": 0, "pending": 0, "rejected": 0, "rows": []}
    with db.get_conn() as conn:
        tags.load_aliases(conn)
        cands = candidate_tags(conn)
        result["candidates"] = len(cands)
        pairs = neighbour_pairs(ensure_vectors(conn, sorted(cands)), config.TAG_MERGE_REVIEW_SIM)
        decided = _decided(conn)
        todo = [p for p in pairs if frozenset(p[:2]) not in decided
                and not tags.AMBIGUOUS_TAGS.intersection(p[:2])]
        result["pairs"] = len(todo)
        examples = _examples(conn, {t for a, b, _ in todo[:limit] for t in (a, b)})
        merged: set[str] = set()
        for a, b, sim in todo[:limit]:
            if should_stop():
                break
            if a in merged or b in merged:
                continue
            try:
                v = verify(a, b, examples)
            except Exception as e:  # noqa: BLE001
                log.warning("tag verify %r / %r failed: %s", a, b, e)
                continue
            canonical = v["canonical"]
            alias = b if canonical == a else a
            if v["relation"] != "SAME_CONCEPT":
                status = "rejected"
            elif sim >= config.TAG_MERGE_AUTO_SIM and v["confidence"] >= config.TAG_MERGE_AUTO_CONFIDENCE:
                status = "approved"
                merged.add(alias)
            else:
                status = "pending"
            result[status] += 1
            row = {"alias": alias, "canonical": canonical, "status": status, "similarity": round(sim, 3),
                   "relation": v["relation"], "confidence": v["confidence"],
                   "stories": (cands.get(alias, 0), cands.get(canonical, 0))}
            result["rows"].append(row)
            log.info("tag pair %s -> %s sim=%.3f %s %.2f => %s", alias, canonical, sim, v["relation"],
                     v["confidence"], status)
            if apply:
                conn.execute(
                    "INSERT OR REPLACE INTO TagAlias (alias, canonical, status, similarity, relation, confidence, method) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'embedding+llm')",
                    (alias, canonical, status, sim, v["relation"], v["confidence"]))
        if apply:
            tags.load_aliases(conn)
    return result


def review(conn: sqlite3.Connection, alias: str, status: str, canonical: str | None = None) -> int:
    """Approve / reject proposals for ``alias`` (only the one to ``canonical`` if given); rows changed.

    Approving without ``canonical`` needs exactly one pending proposal.
    """
    if status == "approved":
        if canonical is None:
            rows = conn.execute("SELECT canonical FROM TagAlias WHERE alias = ? AND status = 'pending'", (alias,)).fetchall()
            if len(rows) != 1:
                raise ValueError(f"{alias!r} has {len(rows)} pending proposals; name the canonical tag")
            canonical = rows[0][0]
        conn.execute("UPDATE TagAlias SET status = 'rejected', reviewed_at = datetime('now') "
                     "WHERE status = 'approved' AND ((alias = ? AND canonical != ?) OR (alias = ? AND canonical = ?))",
                     (alias, canonical, canonical, alias))
    sql, params = "UPDATE TagAlias SET status = ?, reviewed_at = datetime('now') WHERE alias = ?", [status, alias]
    if canonical:
        sql += " AND canonical = ?"
        params.append(canonical)
    n = conn.execute(sql, params).rowcount
    tags.load_aliases(conn)
    return n


def add_manual(conn: sqlite3.Connection, alias: str, canonical: str) -> tuple[str, str]:
    """Approve ``alias`` → ``canonical`` by hand; returns the normalized pair."""
    pair = (tags.base_tag(alias), tags.base_tag(canonical))
    if not all(pair) or pair[0] == pair[1]:
        raise ValueError(f"not a usable alias pair: {pair}")
    conn.execute("INSERT OR IGNORE INTO TagAlias (alias, canonical, method) VALUES (?, ?, 'manual')", pair)
    review(conn, pair[0], "approved", pair[1])
    return pair
