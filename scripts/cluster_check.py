import sys
from app import db

ids = sys.argv[1:] if len(sys.argv) > 1 else ["15", "7", "27"]
with db.get_conn() as conn:
    for sid in ids:
        print(f"=== story {sid} ===")
        s = conn.execute("SELECT headline, quality_score, tags FROM Story WHERE id=?", (sid,)).fetchone()
        print("headline:", s["headline"], "| q:", s["quality_score"], "| tags:", s["tags"])
        for r in conn.execute("SELECT id, title, feed_id, created_at FROM Article WHERE story_id=? ORDER BY created_at", (sid,)):
            print(" ", r["id"], r["feed_id"], r["title"][:110])
