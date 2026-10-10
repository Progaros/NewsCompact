"""Check display-score distribution, quality calibration, and article loss."""
import statistics

from app import db, scoring

with db.get_conn() as conn:
    counts = {}
    for r in conn.execute(
        "SELECT story_id, COUNT(DISTINCT feed_id) c FROM Article "
        "WHERE story_id IS NOT NULL GROUP BY story_id"
    ):
        counts[r["story_id"]] = r["c"]
    stories = [dict(r) for r in conn.execute("SELECT * FROM Story").fetchall()]
    profile = scoring.get_user_profile()
    tw, prefs = profile["tag_weights"], profile["preferences"]

disp = []
for s in stories:
    sc = counts.get(s["id"], 0)
    disp.append((scoring.display_score(s, tw, sc, prefs), s["id"], s.get("quality_score")))
disp.sort(reverse=True)

vals = [d[0] for d in disp]
print("=== display_score distribution ===")
print("n:", len(vals))
if vals:
    q = statistics.quantiles(vals, n=4)
    print("min %.1f  p25 %.1f  median %.1f  p75 %.1f  max %.1f" % (vals[-1], q[0], statistics.median(vals), q[2], vals[0]))
print("top10:")
for d in disp[:10]:
    print("  display=%.2f story=%s quality=%s" % (d[0], d[1], d[2]))
for floor in (25, 20, 15, 10, 5):
    print("  display>=%d: %d stories" % (floor, sum(1 for v in vals if v >= floor)))

print("=== quality vs source count (multi-source = corroborated) ===")
for qv in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
    n = sum(1 for s in stories if (s.get("quality_score") or 0) >= qv - 0.5 and (s.get("quality_score") or 0) < qv + 0.5)
    if n:
        print("  q=%d -> %d stories" % (qv, n))
