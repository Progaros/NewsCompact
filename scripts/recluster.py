"""Reset clustering state and re-run the pipeline over all articles.

Steps:
1. Delete all Story rows, NULL out Article.story_id.
2. Clear Article_Embeddings; seed from Experiment_Embeddings (title-first
   vectors) so the worker doesn't pay the ~1s-per-article embed cost.
3. Loop process_new_articles until nothing is pending (server must be
   stopped so two processes don't double-process the same articles).

Usage (server stopped):
  uv run python scripts/recluster.py
"""
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, pipeline  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("recluster")


def reset() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM Story")
        conn.execute("UPDATE Article SET story_id = NULL")
        conn.execute("DELETE FROM Article_Embeddings")
        seeded = conn.execute(
            """
            INSERT INTO Article_Embeddings (article_id, embedding)
            SELECT e.article_id, e.embedding
            FROM Experiment_Embeddings e
            JOIN Article a ON a.id = e.article_id
            """
        ).rowcount
        conn.commit()
        arts = conn.execute("SELECT count(*) c FROM Article").fetchone()["c"]
        log.info("reset done: seeded %d embeddings (of %d articles)", seeded, arts)


def main() -> None:
    reset()
    t0 = time.time()
    total = 0
    while True:
        n = pipeline.process_new_articles(limit=200)
        total += n
        log.info(
            "processed %d (total %d, %.0fs elapsed)",
            n, total, time.time() - t0,
        )
        if n == 0:
            break
    with db.get_conn() as conn:
        stories = conn.execute("SELECT count(*) c FROM Story").fetchone()["c"]
        multi = conn.execute(
            "SELECT count(*) c FROM (SELECT 1 FROM Article WHERE story_id IS NOT NULL "
            "GROUP BY story_id HAVING count(*) > 1)"
        ).fetchone()["c"]
        multisrc = conn.execute(
            "SELECT count(*) c FROM (SELECT story_id FROM Article WHERE story_id IS NOT NULL "
            "GROUP BY story_id HAVING count(DISTINCT feed_id) > 1)"
        ).fetchone()["c"]
    log.info("final: stories=%d multi-article=%d multi-source=%d", stories, multi, multisrc)


if __name__ == "__main__":
    main()
