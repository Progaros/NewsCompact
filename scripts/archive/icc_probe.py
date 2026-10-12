"""Probe the ICC story: what does the harness quality scorer give each article?"""
import sys

sys.path.insert(0, ".")
from app import db  # noqa: E402
from app.harness import LocalJevHarness  # noqa: E402

h = LocalJevHarness()

with db.get_conn() as conn:
    arts = conn.execute(
        "SELECT id, title, url, content_raw FROM Article WHERE story_id=5 "
        "ORDER BY published_date"
    ).fetchall()

for a in arts:
    text = a["content_raw"] or ""
    res = h.assess_article(a["title"] or "", text)
    print(
        "art %s  quality=%s  breaking=%s  len=%d  title=%s"
        % (a["id"], res["quality"], res["breaking"], len(text), (a["title"] or "")[:45])
    )
    # show whether the stored text looks like boilerplate
    first = " ".join(text.split())[:120]
    print("    head:", first)
