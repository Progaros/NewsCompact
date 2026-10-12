"""Analyze stored content_raw: how often is it boilerplate/social junk?

Everything runs inside a single `with db.get_conn()` block because get_conn
is a contextmanager that closes the connection on exit.
"""
import sys

sys.path.insert(0, ".")
from app import db  # noqa: E402

# Signatures of stored bodies that are NOT the article text: bot-block pages,
# paywalls, social/YT chrome. Lowercased + deduped.
SIGS = [
    "post permalink", "top comments", "m.youtube", "youtube",
    "wurde blockiert", "zugriffe", "please try again later",
    "access denied", "unusual traffic", "captcha",
    "enable javascript", "javascript seems disabled", "cookie",
    "subscribe for free", "advertisement", "advertiser", "sponsored",
    "one small step", "continue reading", "are we the baddies",
]
SIGS = [s.lower() for s in set(SIGS)]

with db.get_conn() as conn:
    rows = conn.execute("SELECT id, story_id, content_raw, length(content_raw) l FROM Article").fetchall()

    total = len(rows)
    empty = [r for r in rows if not (r["content_raw"] or "").strip()]
    short = [r for r in rows if len(r["content_raw"] or "") < 300]

    def sigs_of(text: str):
        t = (text or "").lower()
        return [s for s in SIGS if s in t]

    junk = [(r["id"], sigs_of(r["content_raw"])) for r in rows if sigs_of(r["content_raw"])]

    print("total articles:", total)
    print("empty content:", len(empty))
    print("short (<300 chars):", len(short))
    print("with a boilerplate signature:", len(junk))
    print("\nsample junk (id -> signatures):")
    for i, h in junk[:12]:
        print("  ", i, h)

    # Cross-tab: does the *story* quality track the junkiness of member articles?
    # Map each article to its story, bucket articles by content state.
    art2story = {r["id"]: r["story_id"] for r in rows}
    ids_empty = {r["id"] for r in empty}
    ids_short = {r["id"] for r in short}
    ids_junk = {i for i, _ in junk}
    ids_all = {r["id"] for r in rows}
    ids_clean = ids_all - ids_empty - ids_short - ids_junk

    def bucket_quality(ids):
        story_ids = {art2story[i] for i in ids if art2story.get(i)}
        if not story_ids:
            return 0, 0.0
        q = [r["quality_score"] for r in conn.execute(
            "SELECT quality_score FROM Story WHERE id IN (%s)" %
            ",".join(str(i) for i in story_ids))]
        return len(q), (sum(q) / len(q) if q else 0.0)

    print("\n=== content state -> story quality (last-writer) ===")
    for label, ids in (("clean", ids_clean), ("short", ids_short),
                       ("junk", ids_junk), ("empty", ids_empty)):
        n, mean = bucket_quality(ids)
        print(f"  {label:6}: {len(ids):5} articles -> {n:4} stories, mean story quality {mean:.2f}")
