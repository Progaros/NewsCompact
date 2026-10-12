"""Quick snapshot of DB state for verifying the feedback/score work."""
import json
import sqlite3

conn = sqlite3.connect("news.db")
conn.row_factory = sqlite3.Row

print("=== tables ===")
for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print(" ", r["name"])

print("=== FeedbackLog ===")
print("count:", conn.execute("SELECT COUNT(*) c FROM FeedbackLog").fetchone()["c"])
for r in conn.execute(
    "SELECT story_id, kind, created_at FROM FeedbackLog ORDER BY created_at DESC LIMIT 8"
):
    print("  ", dict(r))

print("=== stories / articles ===")
print("stories:", conn.execute("SELECT COUNT(*) c FROM Story").fetchone()["c"])
print("articles w/ story:", conn.execute("SELECT COUNT(*) c FROM Article WHERE story_id IS NOT NULL").fetchone()["c"])
print("unassigned:", conn.execute("SELECT COUNT(*) c FROM Article WHERE story_id IS NULL").fetchone()["c"])

print("=== multi-source stories ===")
print(conn.execute(
    "SELECT COUNT(*) c FROM (SELECT story_id FROM Article WHERE story_id IS NOT NULL "
    "GROUP BY story_id HAVING COUNT(DISTINCT feed_id)>=2)"
).fetchone()["c"])

print("=== quality distribution ===")
for r in conn.execute(
    "SELECT CAST(quality_score AS INT) q, COUNT(*) c FROM Story GROUP BY q ORDER BY q"
):
    print("  ", dict(r))

# the ICC story the user flagged
print("=== ICC sanctions story ===")
for r in conn.execute(
    "SELECT id, headline, quality_score, metrics, updated_at FROM Story "
    "WHERE headline LIKE '%ICC%' ORDER BY updated_at DESC LIMIT 3"
):
    print("  ", dict(r))

conn.close()
