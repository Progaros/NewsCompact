"""Parse `perf article=...` lines from server.log → load stats.

Run: PYTHONPATH=. uv run python scripts/perf_stats.py
"""
import re

LINE = re.compile(
    r"perf article=(\d+) story=(\d+) created=(\w+) wall=([\d.]+)s "
    r"cpu=([\d.]+)s embed=([\d.]+)s knn=([\d.]+)s harness=([\d.]+)s "
    r"p2=([\d.]+)s p2chars=(\d+)"
)

rows = []
for path in ("server.log",):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE.search(line)
            if m:
                rows.append(m.groups())

def pct(vals, p):
    s = sorted(vals)
    if not s:
        return 0.0
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)

n = len(rows)
print(f"samples: {n}")
for name, idx in [("wall", 3), ("cpu", 4), ("embed", 5), ("knn", 6),
                  ("harness", 7), ("p2", 8), ("p2chars", 9)]:
    vals = [float(r[idx]) for r in rows]
    print(f"{name:8} avg={sum(vals)/n:8.2f} p50={pct(vals, .5):8.2f} "
          f"p90={pct(vals, .9):8.2f} max={max(vals):8.2f}")

created = sum(1 for r in rows if r[2] == "True")
print(f"new stories created: {created}/{n}")

# p2 wall vs payload size (scaling)
pairs = sorted(((float(r[9]), float(r[8])) for r in rows))
for chars, wall in pairs:
    print(f"  p2chars={chars:8.0f} p2={wall:5.1f}s")

# story sizes (p2chars proxy for cluster size growth)
sizes = sorted(float(r[9]) for r in rows)
print(f"p2 payload: min={sizes[0]:.0f} median={sizes[len(sizes)//2]:.0f} max={sizes[-1]:.0f}")
