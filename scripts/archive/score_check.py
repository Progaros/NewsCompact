"""Independent Display_Score verification.

Recomputes the score for every story the API returned, from raw DB values,
without importing app.scoring (avoids tautology):
    relevance_base = 10 * (WQ*quality/10 + WC*credibility + WI*impact)
    display = relevance_base + urgency*W1 + interest*W3 - decay
where credibility = clamp(0.25 + 0.2*(sources-1)), impact = 0.5 baseline
(+0.25 per matching region from preferences), and
decay = DECAY_MAX * (1 - exp(-age_h / DECAY_HALF_LIFE)).
Compares against the API value and checks sort order.

Run: PYTHONPATH=. uv run python scripts/score_check.py
"""
import datetime
import json
import math
import urllib.request

from app import config, db

API = "http://127.0.0.1:8787/api/stories?limit=25"


def main():
    api = json.loads(urllib.request.urlopen(API, timeout=60).read().decode())
    items = api["stories"] if isinstance(api, dict) else api
    with db.get_conn() as conn:
        profile_row = conn.execute(
            "SELECT tag_weights, preferences FROM UserProfile WHERE id=1").fetchone()
        tag_weights = json.loads(profile_row["tag_weights"] or "{}")
        prefs = json.loads(profile_row["preferences"] or "{}")
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM Story")}
        src = {r["story_id"]: r["c"] for r in conn.execute(
            "SELECT story_id, COUNT(DISTINCT feed_id) c FROM Article "
            "WHERE story_id IS NOT NULL GROUP BY story_id")}

    def clamp01(x):
        return max(0.0, min(1.0, x))

    api_scores = []
    mismatches = 0
    for s in items:
        sid = s["id"]
        story = rows[sid]
        tags = [t for t in json.loads(story["tags"] or "[]") if isinstance(t, str)]
        metrics = json.loads(story["metrics"] or "{}")
        urgency = float(metrics.get("urgency_level", 5))
        quality_n = clamp01(float(story["quality_score"] or 5.0) / 10.0)
        cred_n = clamp01(0.25 + 0.20 * max(0, src.get(sid, 0) - 1))
        impact_n = 0.5
        for key in ("interest_regions", "locations"):
            for v in prefs.get(key) or []:
                v = str(v).lower().strip()
                if v and any(v in t.lower() for t in tags):
                    impact_n += 0.25
        impact_n = clamp01(impact_n)
        base = 10.0 * (config.W_RELEVANCE_QUALITY * quality_n
                        + config.W_RELEVANCE_CREDIBILITY * cred_n
                        + config.W_RELEVANCE_IMPACT * impact_n)
        interest = sum(tag_weights.get(t, 0.0) for t in tags)
        ts = story["updated_at"]
        try:
            age_h = (datetime.datetime.utcnow()
                     - datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
        except (ValueError, TypeError):
            age_h = 0.0
        decay = config.TIME_DECAY_MAX * (1.0 - math.exp(-max(0.0, age_h) / config.TIME_DECAY_HALF_LIFE_HOURS))
        expected = base + urgency * config.W1_URGENCY + interest * config.W3_INTEREST - decay
        got = s["display_score"]
        diff = abs(expected - got)
        api_scores.append(got)
        flag = "OK" if diff < 0.05 else "MISMATCH"
        if diff >= 0.05:
            mismatches += 1
        print(f"story {sid}: api={got:.3f} expected={expected:.3f} diff={diff:.4f} {flag} "
              f"(base={base:.2f} urg={urgency} q={quality_n:.2f} cred={cred_n:.2f} "
              f"imp={impact_n:.2f} int={interest:.2f} decay={decay:.2f})")
    order_ok = api_scores == sorted(api_scores, reverse=True)
    print(f"\nscores: {mismatches} mismatches / {len(items)} checked")
    print(f"api sorted desc: {order_ok}")


if __name__ == "__main__":
    main()
