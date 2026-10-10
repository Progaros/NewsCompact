"""Re-cluster junk-classified articles (2026-10-10, one-shot migration).

Junk bodies (bot-block chrome, social feed boilerplate) embedded to nearly
identical vectors regardless of topic, so junk articles clustered by
boilerplate similarity and grew catch-all stories (story 27: 25 unrelated
members; story 87: 17 unrelated dating posts; story 113: mixed desk-setup
junk). embed_text_for now embeds title-only for non-'ok' bodies.

This script repairs the existing state:
1. Replace stored embeddings of junk articles with title-only vectors.
2. Unassign them (story_id = NULL) so the pipeline re-clusters by title.
3. Delete stories left with no articles.
4. Refresh headline/summary/metrics of stories that lost members,
   preserving their original updated_at (no time-decay reset).

Run while the server is stopped:
  uv run python scripts/recluster_junk.py
"""
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, ollama  # noqa: E402
from app import pipeline  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("recluster_junk")


def main() -> None:
    t0 = time.time()
    with db.get_conn() as conn:
        junk = conn.execute(
            "SELECT id, title FROM Article WHERE content_class = 'junk'"
        ).fetchall()
        affected = [r[0] for r in conn.execute(
            "SELECT DISTINCT story_id FROM Article "
            "WHERE content_class = 'junk' AND story_id IS NOT NULL"
        ).fetchall()]
        # original updated_at per affected story, to restore after refresh
        updated_at = {
            r[0]: r[1] for r in conn.execute("SELECT id, updated_at FROM Story")
        }

    # 1. Title-only embeddings for every junk article.
    vecs = ollama.embed_texts([a["title"] or "news" for a in junk])
    with db.get_conn() as conn:
        for a, v in zip(junk, vecs):
            conn.execute(
                "DELETE FROM Article_Embeddings WHERE article_id = ?", (a["id"],)
            )
            conn.execute(
                "INSERT INTO Article_Embeddings (article_id, embedding) VALUES (?, ?)",
                (a["id"], db.pack_floats(v)),
            )
        # 2. Unassign so the pipeline re-clusters on the next cycle.
        conn.execute("UPDATE Article SET story_id = NULL WHERE content_class = 'junk'")
        # 3. Delete stories left empty.
        empties = conn.execute(
            "SELECT id FROM Story WHERE NOT EXISTS "
            "(SELECT 1 FROM Article WHERE Article.story_id = Story.id)"
        ).fetchall()
        for s in empties:
            conn.execute("DELETE FROM Story WHERE id = ?", (s["id"],))
        log.info(
            "done: %d junk re-embedded, %d unassigned, %d empty stories deleted",
            len(junk), len(junk), len(empties),
        )

    # 4. Refresh stories that lost members (skip ones deleted in step 3).
    refreshed, failed = 0, 0
    for sid in affected:
        with db.get_conn() as conn:
            if conn.execute("SELECT 1 FROM Story WHERE id = ?", (sid,)).fetchone() is None:
                continue
            try:
                pipeline.prompt2_update_story(conn, sid)
                if sid in updated_at:
                    conn.execute(
                        "UPDATE Story SET updated_at = ? WHERE id = ?",
                        (updated_at[sid], sid),
                    )
                conn.commit()
                refreshed += 1
            except Exception:  # noqa: BLE001 - one bad story must not abort the rest
                failed += 1
                log.exception("refresh failed for story %s", sid)
    log.info(
        "refreshed %d stories (%d failed) in %.0fs",
        refreshed, failed, time.time() - t0,
    )


if __name__ == "__main__":
    main()
