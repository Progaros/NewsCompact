"""Quick DB state inspection (read-only). Usage: uv run python scripts/db_check.py"""
import sys

sys.path.insert(0, ".")
from app import db  # noqa: E402

with db.get_conn() as conn:
    arts = conn.execute("SELECT count(*) c FROM Article").fetchone()["c"]
    sts = conn.execute("SELECT count(*) c FROM Story").fetchone()["c"]
    emb = conn.execute("SELECT count(*) c FROM Article_Embeddings").fetchone()["c"]
    pend = conn.execute("SELECT count(*) c FROM Article WHERE story_id IS NULL").fetchone()["c"]
    print(f"articles={arts} stories={sts} embedded={emb} pending={pend}")

    multi = conn.execute(
        "SELECT story_id, count(*) n, count(distinct feed_id) feeds "
        "FROM Article WHERE story_id IS NOT NULL GROUP BY story_id HAVING count(*) > 1"
    ).fetchall()
    multi.sort(key=lambda r: -r["n"])
    print(f"multi-article stories: {len(multi)} (multi-source: {sum(1 for m in multi if m['feeds'] > 1)})")
    for m in multi[:8]:
        print(f"  story {m['story_id']}: {m['n']} arts / {m['feeds']} feeds")

    rows = conn.execute(
        "SELECT count(*) c FROM Article WHERE length(coalesce(content_raw,'')) < 200"
    ).fetchone()
    print(f"articles with <200 char body: {rows['c']}")

    for sid in (44, 222):
        print(f"\n--- story {sid} ---")
        s = conn.execute("SELECT * FROM Story WHERE id=?", (sid,)).fetchone()
        if s:
            print("headline:", s["headline"])
            print("quality:", s["quality_score"], "updated:", s["updated_at"])
        arts = conn.execute(
            "SELECT id, feed_id, title, length(content_raw) L FROM Article WHERE story_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        for a in arts[:20]:
            t = a["title"] or "(no title)"
            print(f"  art {a['id']} (feed {a['feed_id']}, {a['L']} chars): {t[:90]}")
