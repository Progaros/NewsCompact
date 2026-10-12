"""Compare body-based vs title-based embedding similarity inside clusters.

Usage: uv run python scripts/embed_compare.py [story_ids...]
Default: the largest multi-article stories.
"""
import math
import struct
import sys

sys.path.insert(0, ".")
from app import db, ollama  # noqa: E402


def norm(v):
    s = math.sqrt(sum(x * x for x in v))
    return [x / s for x in v] if s else v


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def main():
    with db.get_conn() as conn:
        if len(sys.argv) > 1:
            sids = [int(x) for x in sys.argv[1:]]
        else:
            rows = conn.execute(
                "SELECT story_id FROM Article WHERE story_id IS NOT NULL "
                "GROUP BY story_id HAVING count(*) > 3 ORDER BY count(*) DESC LIMIT 4"
            )
            sids = [r["story_id"] for r in rows]

        for sid in sids:
            arts = conn.execute(
                "SELECT id, title, content_raw FROM Article WHERE story_id=?", (sid,)
            ).fetchall()
            print(f"\n=== story {sid} ({len(arts)} articles) ===")

            body_vecs, labels = {}, []
            for a in arts:
                labels.append(a["title"][:60] or f"(no title) art {a['id']}")
                row = conn.execute(
                    "SELECT embedding FROM Article_Embeddings WHERE article_id=?", (a["id"],)
                ).fetchone()
                if row:
                    body_vecs[a["id"]] = norm(unpack(bytes(row["embedding"])))

            texts = [(a["title"] or "").strip() or "(no title)" for a in arts]
            try:
                tvecs = {a["id"]: norm(v) for a, v in zip(arts, ollama.embed_texts(texts))}
            except Exception as e:  # noqa: BLE001
                print(f"  title embed failed: {e}")
                tvecs = {}

            def dump(prefix, vecs):
                ids = [a["id"] for a in arts]
                pairs = []
                for i in range(len(ids)):
                    for j in range(i + 1, len(ids)):
                        if ids[i] in vecs and ids[j] in vecs:
                            pairs.append((cosine(vecs[ids[i]], vecs[ids[j]]), i, j))
                pairs.sort(reverse=True)
                if not pairs:
                    return
                sims = [p[0] for p in pairs]
                print(f"  {prefix}: p50={sims[len(sims)//2]:.3f} max={sims[0]:.3f} "
                      f">=0.62: {sum(1 for s in sims if s >= 0.62)}/{len(sims)}")
                for s, i, j in pairs[:4]:
                    print(f"    {s:.3f}  {labels[i]}  <->  {labels[j]}")

            dump("body  ", body_vecs)
            dump("title ", tvecs)


if __name__ == "__main__":
    main()
