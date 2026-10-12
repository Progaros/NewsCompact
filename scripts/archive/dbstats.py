"""Quick DB stats: counts + largest clusters. Run: PYTHONPATH=. uv run python scripts/dbstats.py"""
from app import db


def main():
    with db.get_conn() as conn:
        print("articles:", conn.execute("SELECT COUNT(*) FROM Article").fetchone()[0])
        print("processed:", conn.execute(
            "SELECT COUNT(*) FROM Article WHERE story_id IS NOT NULL").fetchone()[0])
        print("stories:", conn.execute("SELECT COUNT(*) FROM Story").fetchone()[0])
        print("-- top clusters --")
        rows = conn.execute("""
            SELECT s.id, s.headline, s.quality_score, COUNT(a.id) n,
                   COUNT(DISTINCT a.feed_id) f, s.updated_at
            FROM Story s JOIN Article a ON a.story_id = s.id
            GROUP BY s.id ORDER BY n DESC LIMIT 12""").fetchall()
        for r in rows:
            print(dict(r))
        multi = conn.execute("""
            SELECT COUNT(*) FROM (
              SELECT story_id FROM Article GROUP BY story_id HAVING COUNT(*) > 1
            )""").fetchone()[0]
        print("multi-article stories:", multi)


if __name__ == "__main__":
    main()
