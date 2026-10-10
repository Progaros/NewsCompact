"""One-off: normalize stored Story.tags and consolidate UserProfile.tag_weights.

Pre-fix, the LLM's free-form tag spellings (AI/ai/a.i., machine-learning/
machine learning, ...) were stored as-is, splitting interest weights across
variants. New writes are normalized at storage (app/tags.py); this script
fixes the rows already in the DB.

Safe to re-run: normalizing an already-normalized value is a no-op, and the
weight consolidation is idempotent (merging by max).

Run: PYTHONPATH=. uv run python scripts/normalize_tags.py
"""
from __future__ import annotations

import json

from app import db
from app.tags import normalize_tags


def main():
    with db.get_conn() as conn:
        # --- Story.tags ------------------------------------------------------
        stories = conn.execute("SELECT id, tags FROM Story").fetchall()
        changed = 0
        for s in stories:
            try:
                tags = json.loads(s["tags"] or "[]")
            except json.JSONDecodeError:
                tags = []
            cleaned = normalize_tags([t for t in tags if isinstance(t, str)])
            new = json.dumps(cleaned)
            if new != (s["tags"] or "[]"):
                changed += 1
                conn.execute("UPDATE Story SET tags = ? WHERE id = ?", (new, s["id"]))

        # --- UserProfile.tag_weights -----------------------------------------
        row = conn.execute("SELECT tag_weights FROM UserProfile WHERE id=1").fetchone()
        if row:
            tw = json.loads(row["tag_weights"] or "{}")
            merged: dict[str, float] = {}
            for k, v in tw.items():
                key = normalize_tags([k])
                key = key[0] if key else k  # keep as-is if normalization empties it
                try:
                    val = float(v)
                except (TypeError, ValueError):
                    val = 0.0
                # same concept reached via two spellings: keep the stronger nudge
                merged[key] = max(merged.get(key, val), val)
            conn.execute(
                "UPDATE UserProfile SET tag_weights = ? WHERE id=1",
                (json.dumps(merged),),
            )
            print(f"tag_weights keys: {len(tw)} -> {len(merged)}")
            print(json.dumps(merged, indent=2))
        conn.commit()
        print(f"stories retagged: {changed}/{len(stories)}")


if __name__ == "__main__":
    main()
