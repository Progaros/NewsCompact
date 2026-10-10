"""Dedup Article_Embeddings: keep one vec0 row per article_id.

The pipeline used to re-embed an article on every retry (see ensure_embedding
history), leaving duplicate vec0 rows that double-weighted story centroids.
This drops the extras, keeping the first inserted row. Safe to re-run.

Usage: PYTHONPATH=. uv run python scripts/dedup_embeddings.py
"""
import sys

sys.path.insert(0, ".")
from app import db  # noqa: E402

with db.get_conn() as conn:
    rows = conn.execute(
        "SELECT article_id, embedding FROM Article_Embeddings"
    ).fetchall()

    # vec0 virtual table: delete by rowid is reliable (one row per
    # article_id is the invariant; keep the first inserted row).
    seen: set[int] = set()
    drop = []
    for r in rows:
        if r["article_id"] in seen:
            drop.append(r)
        else:
            seen.add(r["article_id"])
    print(f"found {len(drop)} duplicate vec0 rows to remove")

    for art_id in sorted({r["article_id"] for r in drop}):
        keep = conn.execute(
            "SELECT rowid FROM Article_Embeddings WHERE article_id = ? LIMIT 1", (art_id,)
        ).fetchone()
        if keep is None:
            continue
        conn.execute(
            "DELETE FROM Article_Embeddings WHERE article_id = ? AND rowid != ?",
            (art_id, keep["rowid"]),
        )

    remaining = conn.execute(
        "SELECT article_id, count(*) n FROM Article_Embeddings GROUP BY article_id HAVING n > 1"
    ).fetchall()
    total = conn.execute("SELECT count(*) n FROM Article_Embeddings").fetchone()["n"]
    print(f"done. duplicate article_ids remaining: {len(remaining)}; total vec0 rows: {total}")
