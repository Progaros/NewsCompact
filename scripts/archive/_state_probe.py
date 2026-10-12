"""Throwaway: print clustering state for golden-set design. Run, then delete."""
import sqlite3

c = sqlite3.connect("news.db")
c.row_factory = sqlite3.Row

print("== top story sizes ==")
for r in c.execute(
    "SELECT s.id, s.headline, COUNT(a.id) n FROM Article a JOIN Story s ON s.id=a.story_id "
    "GROUP BY s.id ORDER BY n DESC LIMIT 14"
):
    print(r["n"], r["id"], (r["headline"] or "")[:75])

print("\n== content_class ==")
for r in c.execute("SELECT content_class, COUNT(*) n FROM Article GROUP BY content_class"):
    print(repr(r["content_class"]), r["n"])

# Find a strong coherent multi-feed story (candidate positive cluster):
# members from >=2 distinct feeds, all content_class ok/thin.
print("\n== coherent multi-feed stories (>=3 members, >=2 feeds, no broken) ==")
for r in c.execute(
    "SELECT s.id, s.headline, COUNT(a.id) n, COUNT(DISTINCT a.feed_id) feeds "
    "FROM Article a JOIN Story s ON s.id=a.story_id "
    "WHERE a.content_class IN ('ok','thin') GROUP BY s.id "
    "HAVING feeds >= 2 AND n >= 3 ORDER BY n DESC LIMIT 12"
):
    print(r["n"], "feeds=", r["feeds"], r["id"], (r["headline"] or "")[:65])

# Topically-similar but distinct-event candidates: OpenAI / cyber / math, from
# different stories, for the negative set.
print("\n== distinct single-topic stories (for negative set) ==")
for r in c.execute(
    "SELECT s.id, s.headline, COUNT(a.id) n FROM Article a JOIN Story s ON s.id=a.story_id "
    "WHERE a.content_class IN ('ok','thin') GROUP BY s.id ORDER BY n DESC LIMIT 30"
):
    h = (r["headline"] or "")
    if any(k in h.lower() for k in ("openai", "ai ", "anthropic", "cyber", "security", "vulnerab", "math", "google", "apple", "musk", "eliot", "federal", "senat", "congress")):
        print(r["n"], r["id"], h[:80])
