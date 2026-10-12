"""Background monitor: appends DB stats to reprocess_monitor.log every 15 min.
Run: nohup env PYTHONPATH=. uv run python scripts/monitor.py > /dev/null 2>&1 &
"""
import time

from app import db


def snap() -> str:
    with db.get_conn() as conn:
        a = conn.execute("SELECT COUNT(*) FROM Article").fetchone()[0]
        p = conn.execute("SELECT COUNT(*) FROM Article WHERE story_id IS NOT NULL").fetchone()[0]
        s = conn.execute("SELECT COUNT(*) FROM Story").fetchone()[0]
        multi = conn.execute(
            "SELECT COUNT(*) FROM ("
            " SELECT story_id FROM Article WHERE story_id IS NOT NULL "
            " GROUP BY story_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        biggest = conn.execute(
            "SELECT COUNT(*) FROM Article WHERE story_id IS NOT NULL "
            "GROUP BY story_id ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()[0]
        return f"articles={a} processed={p} stories={s} multi={multi} biggest={biggest}"


def main():
    with open("reprocess_monitor.log", "a") as f:
        f.write(snap() + "\n")
    while True:
        time.sleep(900)
        try:
            with open("reprocess_monitor.log", "a") as f:
                f.write(snap() + "\n")
        except Exception as e:  # keep monitor alive
            with open("reprocess_monitor.log", "a") as f:
                f.write(f"monitor error: {e}\n")


if __name__ == "__main__":
    main()
