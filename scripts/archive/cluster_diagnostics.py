"""Clustering diagnostics: similarity distribution of clustered articles.

For every clustered article, compute:
- centroid_sim: cosine to its story centroid (article excluded)
- member_sim:   cosine to the best other member of its story

Prints percentiles so a join threshold can be calibrated from real data
instead of simulation. Usage: PYTHONPATH=. uv run python scripts/cluster_diagnostics.py
"""
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, db  # noqa: E402


def unpack(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def main() -> None:
    with db.get_conn() as c:
        # one embedding per article (first row wins; dedup keeps 1 per article)
        embs: dict[int, list[float]] = {}
        for r in c.execute("SELECT article_id, embedding FROM Article_Embeddings"):
            embs.setdefault(r["article_id"], unpack(bytes(r["embedding"])))
        arts = {
            r["id"]: {"story": r["story_id"], "cls": r["content_class"]}
            for r in c.execute(
                "SELECT id, story_id, content_class FROM Article"
            )
        }
        stories: dict[int, list[int]] = {}
        for aid, a in arts.items():
            if a["story"] is not None and aid in embs:
                stories.setdefault(a["story"], []).append(aid)

        cent_sims: list[tuple[float, str]] = []
        mem_sims: list[tuple[float, str]] = []
        for sid, members in stories.items():
            members = [m for m in members if m in embs]
            if len(members) < 2:
                continue
            norms = [norm(embs[m]) for m in members]
            n = len(norms)
            acc = [0.0] * config.EMBED_DIM
            for v in norms:
                for i in range(len(v)):
                    acc[i] += v[i] / n
            centroid = norm(acc)
            for idx, aid in enumerate(members):
                v = norms[idx]
                cls = arts[aid]["cls"] or "?"
                cs = sum(x * y for x, y in zip(v, centroid))
                ms = max(sum(x * y for x, y in zip(v, norms[j])) for j in range(n) if j != idx)
                cent_sims.append((cs, cls))
                mem_sims.append((ms, cls))

        for name, xs in (("centroid", cent_sims), ("best-member", mem_sims)):
            vals = sorted(v for v, _ in xs)
            n = len(vals)
            pct = lambda p: vals[min(n - 1, int(n * p))]  # noqa: E731
            print(
                f"{name}: n={n} min={vals[0]:.3f} p10={pct(.1):.3f} "
                f"p25={pct(.25):.3f} p50={pct(.5):.3f} p75={pct(.75):.3f} max={vals[-1]:.3f}"
            )
        # split by content class to see if broken joins sit in a different range
        for name, xs in (("centroid", cent_sims), ("best-member", mem_sims)):
            for cls in ("ok", "thin", "broken"):
                vals = sorted(v for v, k in xs if k == cls)
                if not vals:
                    continue
                n = len(vals)
                pct = lambda p, v=vals: v[min(n - 1, int(n * p))]  # noqa: E731
                print(
                    f"  {name} [{cls}] n={n} p10={pct(.1):.3f} p50={pct(.5):.3f} p90={pct(.9):.3f}"
                )


if __name__ == "__main__":
    main()
