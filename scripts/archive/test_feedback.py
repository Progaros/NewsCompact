"""Offline personalization check (Phase 5, zero-UI).

Proof that the interest path works directly against the DB: apply 5 upvotes
to a high-quality story and show the user_interest / display_score lift,
then roll the profile + FeedbackLog back so the live DB stays pristine.

Run: PYTHONUNBUFFERED=1 uv run python scripts/test_feedback.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, scoring  # noqa: E402


def _tag_weights(profile: dict) -> dict:
    tw = profile.get("tag_weights") or {}
    return tw if isinstance(tw, dict) else json.loads(tw or "{}")


def main() -> None:
    with db.get_conn() as conn:
        story = conn.execute(
            "SELECT * FROM Story WHERE quality_score > 7 "
            "ORDER BY quality_score DESC LIMIT 1"
        ).fetchone()
        assert story is not None, "no Story with quality_score > 7"
        story_id = story["id"]

        profile_before = _tag_weights(scoring.get_user_profile())
        rel_before = scoring.relevance_score(story, profile_before, source_count=1)
        disp_before = scoring.display_score(story, profile_before, source_count=1)
        print(f"Story {story_id}: user_interest={rel_before['user_interest']:.2f} "
              f"display={disp_before:.2f}")

        print(f"Applying 5 upvotes to Story {story_id} ...")
        for _ in range(5):
            scoring.apply_feedback(story_id, "up")

        profile_after = _tag_weights(scoring.get_user_profile())
        rel_after = scoring.relevance_score(story, profile_after, source_count=1)
        disp_after = scoring.display_score(story, profile_after, source_count=1)
        print(f"Story {story_id}: user_interest={rel_after['user_interest']:.2f} "
              f"display={disp_after:.2f}")
        print(f"Breakdown after feedback: {rel_after}")

        assert rel_after["user_interest"] > rel_before["user_interest"], (
            "user_interest did not rise after 5 upvotes"
        )
        assert disp_after > disp_before, "display score did not rise"

    # Roll back so the live DB stays clean: restore tag weights, delete the
    # test votes.
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE UserProfile SET tag_weights=? WHERE id=1",
            (json.dumps(profile_before,),
            )
        )
        conn.execute(
            "DELETE FROM FeedbackLog WHERE story_id=?", (story_id,)
        )
        conn.commit()

    print("OK: personalization math works against the DB (profile rolled back).")


if __name__ == "__main__":
    main()
