"""Cluster experiment: pick an embed-text strategy + clustering mode + threshold.

1. Build candidate embeddings (title-first: title + first 100 words of body;
   title-only when the body is too short to carry signal) — persistent in the
   vec0 table `Experiment_Embeddings` (resumable; reused by the rebuild).
2. Simulate the pipeline's greedy clustering (articles in id order) in pure
   Python for two modes × several thresholds:
     - chain: article joins any member's story (greedy chaining)
     - star : article joins only if similar to the story centroid (running mean)

SQL KNN on vec0 with ORDER BY is ~550ms/query in this build (full-scan cost),
so the simulation keeps everything in memory.

Usage:
  uv run python scripts/cluster_experiment.py            # embed missing + simulate
  uv run python scripts/cluster_experiment.py --fresh    # drop table, re-embed all
"""
import math
import struct
import sys
import time
from operator import mul

sys.path.insert(0, ".")
from app import config, db, ollama  # noqa: E402

TABLE = "Experiment_Embeddings"
MIN_BODY_WORDS = 20
BODY_WORDS = 100
THRESHOLDS = [0.40, 0.45, 0.50, 0.55, 0.60, 0.62, 0.65]


def embed_text(title: str, content: str) -> str:
    body_words = (content or "").split()
    if len(body_words) < MIN_BODY_WORDS:
        return (title or "").strip() or "news"
    return f"{title} {' '.join(body_words[:BODY_WORDS])}".strip() or "news"


def simulate(ids, feed_ids, vecs):
    """Returns per (mode, threshold) stats + article→story assignments."""
    chain_of = {t: {} for t in THRESHOLDS}
    chain_st = {t: {} for t in THRESHOLDS}    # story -> [count, {feeds}]
    star_of = {t: {} for t in THRESHOLDS}
    star_st = {t: {} for t in THRESHOLDS}
    centroids = {t: {} for t in THRESHOLDS}
    sims = []
    for i, a in enumerate(ids):
        va = vecs[a]
        best_sim, best_art = -1.0, None
        for p in ids[:i]:
            s = sum(map(mul, va, vecs[p]))
            if s > best_sim:
                best_sim, best_art = s, p
        if i:
            sims.append(best_sim)
        fid = feed_ids[a]
        for t in THRESHOLDS:
            # chain mode
            if best_art is not None and best_sim >= t:
                sid = chain_of[t][best_art]
                chain_st[t][sid][0] += 1
                chain_st[t][sid][1].add(fid)
            else:
                sid = len(chain_st[t]) + 1
                chain_st[t][sid] = [1, {fid}]
            chain_of[t][a] = sid

            # star mode: best match against all centroids
            bs, bsid = -1.0, None
            for sid2, c in centroids[t].items():
                s2 = sum(map(mul, va, c))
                if s2 > bs:
                    bs, bsid = s2, sid2
            if bs >= t:
                star_of[t][a] = bsid
                n = star_st[t][bsid][0] + 1
                star_st[t][bsid][0] = n
                star_st[t][bsid][1].add(fid)
                c = centroids[t][bsid]
                centroids[t][bsid] = [(c[k] * (n - 1) + va[k]) / n
                                       for k in range(len(va))]
            else:
                sid3 = len(star_st[t]) + 1
                star_st[t][sid3] = [1, {fid}]
                centroids[t][sid3] = list(va)
                star_of[t][a] = sid3

    def stats(st):
        out = {}
        for t in THRESHOLDS:
            s = st[t]
            out[t] = {
                "stories": len(s),
                "multi": sum(1 for x in s.values() if x[0] > 1),
                "multisrc": sum(1 for x in s.values() if len(x[1]) > 1),
                "biggest": max((x[0] for x in s.values()), default=0),
            }
        return out

    return ({"chain": stats(chain_st), "star": stats(star_st)},
            sims, chain_of, chain_st, star_of, star_st)


def main() -> None:
    fresh = "--fresh" in sys.argv
    with db.get_conn() as conn:
        if fresh:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        if not exists:
            conn.execute(
                f"CREATE VIRTUAL TABLE {TABLE} USING vec0("
                f"article_id INTEGER, embedding FLOAT[{config.EMBED_DIM}])"
            )

        have = {r["article_id"] for r in conn.execute(f"SELECT article_id FROM {TABLE}")}
        arts = conn.execute(
            "SELECT id, title, content_raw, feed_id FROM Article ORDER BY id"
        ).fetchall()
        missing = [a for a in arts if a["id"] not in have]
        print(f"articles={len(arts)} have={len(have)} missing={len(missing)}", flush=True)

        t0 = time.time()
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            texts = [embed_text(a["title"] or "", a["content_raw"] or "") for a in chunk]
            ev = ollama.embed_texts(texts)
            for a, v in zip(chunk, ev):
                conn.execute(
                    f"INSERT INTO {TABLE} (article_id, embedding) VALUES (?, ?)",
                    (a["id"], db.pack_floats(v)),
                )
            print(f"  embedded {i + len(chunk)}/{len(missing)} "
                  f"({time.time() - t0:.0f}s)", flush=True)

    # load + normalize vectors (cosine = dot on unit vectors)
    vecs = {}
    with db.get_conn() as conn:
        for r in conn.execute(f"SELECT article_id, embedding FROM {TABLE}"):
            b = bytes(r["embedding"])
            v = list(struct.unpack(f"<{len(b) // 4}f", b))
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs[r["article_id"]] = [x / n for x in v]
    ids = [a["id"] for a in arts]
    feed_ids = {a["id"]: a["feed_id"] for a in arts}

    print("simulating (pure python, ~3 min)…", flush=True)
    t0 = time.time()
    results, sims, chain_of, chain_st, star_of, star_st = simulate(ids, feed_ids, vecs)
    print(f"done in {time.time() - t0:.0f}s", flush=True)

    print("mode     t=   stories  multi  multisrc  biggest")
    for label in ("chain", "star"):
        for t in THRESHOLDS:
            r = results[label][t]
            print(f"  {label:5s} {t:.2f}  {r['stories']:6d}  {r['multi']:5d}  "
                  f"{r['multisrc']:8d}  {r['biggest']:6d}")

    sims.sort()
    n = len(sims)
    print(f"best-earlier sim: p50={sims[n//2]:.3f} p75={sims[n*3//4]:.3f} "
          f"p90={sims[n*9//10]:.3f} p95={sims[n*95//100]:.3f} p99={sims[n*99//100]:.3f}")

    # --- biggest stories for manual junk check ----------------------------
    with db.get_conn() as conn:
        for label, assign, st in (("chain", chain_of, chain_st),
                                   ("star", star_of, star_st)):
            for t in (0.50, 0.55):
                top = sorted(st[t].items(),
                              key=lambda kv: (-kv[1][0], -len(kv[1][1])))[:2]
                print(f"\n{label} t={t} biggest stories:")
                for sid, (cnt, feeds) in top:
                    aids = [k for k, v in assign[t].items() if v == sid][:6]
                    rows = conn.execute(
                        "SELECT title FROM Article WHERE id IN (%s)"
                        % ",".join("?" * len(aids)), tuple(aids)
                    ).fetchall()
                    print(f"  story {sid}: {cnt} arts, {len(feeds)} feeds")
                    for row in rows:
                        print(f"    - {(row['title'] or '(no title)')[:70]}")


if __name__ == "__main__":
    main()
