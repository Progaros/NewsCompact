"""Quick check whether an LLM / embedding model is good enough for this app (needs Ollama, no database).

Runs the production prompts (app/harness.py, app/story.py) on the fictional cases in
model_check_cases.json and scores each role:

  verify  event verifier (clustering, recall merge): merge-decision F1, class accuracy
  assess  article quality + breaking flag: high-vs-low quality ordering, breaking accuracy
  story   Prompt 2: schema-valid replies, summary length, language/location/metrics, update significance
  embed   embedding model: dimension, AUC of same-event vs other-event pairs, share of
          same-event pairs above the retrieval threshold (the thresholds were tuned on qwen3-embedding)

It is a smoke test, not a calibration: a model that fails here won't work, one that passes
should still be compared on your own data (benchmark_clustering.py --llm). Run it with the
current models first and --save the result, then compare a candidate with --baseline.

Examples:
    uv run python scripts/benchmark/model_check.py --save current.json
    uv run python scripts/benchmark/model_check.py --model qwen3:8b --baseline current.json
    uv run python scripts/benchmark/model_check.py --verify-model gemma3:12b --only verify
    uv run python scripts/benchmark/model_check.py --embed-model bge-m3 --only embed
    NEWS_EMBED_BASE_URL=http://127.0.0.1:8081/v1 NEWS_EMBED_MODEL=qwen3-embedding \
      uv run python scripts/benchmark/model_check.py --embed-vs-ollama qwen3-embedding:0.6b --only embed
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx  # noqa: E402

from app import config, harness, ollama, story  # noqa: E402

CASES = Path(__file__).with_name("model_check_cases.json")
ROLES = ("verify", "assess", "story", "embed")
# Minimum scores (0..1). The production models reach these with margin; WARN within 0.1 below.
BARS = {
    "verify.merge_f1": 0.85, "verify.merge_precision": 0.90, "verify.class_acc": 0.70, "verify.valid": 1.0,
    "assess.order_acc": 0.85, "assess.breaking_acc": 0.80, "assess.valid": 1.0,
    "story.valid": 1.0, "story.checks": 0.80, "story.significance_acc": 0.75,
    "embed.auc": 0.90, "embed.recall_at_retrieve": 0.90, "embed.dim_ok": 1.0,
}


def timed(fn, *args, **kwargs) -> tuple[Any, float, str]:
    """``(result | None, seconds, error)``."""
    start = time.monotonic()
    try:
        return fn(*args, **kwargs), time.monotonic() - start, ""
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        return None, time.monotonic() - start, f"{type(e).__name__}: {e}"[:200]


def ratio(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def check_verify(cases: list[dict]) -> tuple[dict, list[float], list[str]]:
    tp = fp = fn = correct = total = valid = 0
    times, notes = [], []
    for case in cases:
        cands = [{"id": i, "headline": c["headline"], "summary": c["summary"]}
                 for i, c in enumerate(case["candidates"])]
        got, secs, err = timed(harness.classify_event_candidates, case["article"]["title"],
                               case["article"]["text"], cands)
        times.append(secs)
        got = got or {}
        for i, c in enumerate(case["candidates"]):
            total += 1
            answer = got.get(i)
            valid += answer is not None
            expected_merge = c["expected"][0] in harness.MERGE_CLASSES
            merge = answer in harness.MERGE_CLASSES
            tp += merge and expected_merge
            fp += merge and not expected_merge
            fn += not merge and expected_merge
            correct += answer in c["expected"]
            if answer not in c["expected"]:
                notes.append(f"{case['id']}#{i}: got {answer or err or 'nothing'}, want {'/'.join(c['expected'])}")
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ({"merge_f1": f1, "merge_precision": precision, "merge_recall": recall,
             "class_acc": ratio(correct, total), "valid": ratio(valid, total)}, times, notes)


def check_assess(cases: list[dict]) -> tuple[dict, list[float], list[str]]:
    scores: dict[str, tuple[str, int]] = {}
    breaking_ok = valid = 0
    times, notes = [], []
    for case in cases:
        got, secs, err = timed(harness.assess_article, case["title"], case["text"])
        times.append(secs)
        if got is None:
            notes.append(f"{case['id']}: {err}")
            continue
        valid += 1
        scores[case["id"]] = (case["band"], got["quality"])
        breaking_ok += got["breaking"] == case["breaking"]
        if got["breaking"] != case["breaking"]:
            notes.append(f"{case['id']}: breaking={got['breaking']}, want {case['breaking']}")
        band_ok = {"high": got["quality"] >= 6, "low": got["quality"] <= 4}.get(case["band"], True)
        if not band_ok:
            notes.append(f"{case['id']}: quality {got['quality']} for a {case['band']}-quality article")
    high = [q for band, q in scores.values() if band == "high"]
    low = [q for band, q in scores.values() if band == "low"]
    pairs = [(h, lo) for h in high for lo in low]
    order = ratio(sum(1.0 if h > lo else 0.5 if h == lo else 0.0 for h, lo in pairs), len(pairs))
    return ({"order_acc": order, "breaking_acc": ratio(breaking_ok, len(cases)),
             "valid": ratio(valid, len(cases))}, times, notes)


def _story_call(case: dict) -> dict:
    arts = [{"id": i + 1, "title": a["title"], "content_raw": a["text"]} for i, a in enumerate(case["articles"])]
    if case["mode"] == "incremental":
        prev = {"headline": case["story"]["headline"], "summary": case["story"]["summary"]}
        arts.insert(0, {"id": 0, "title": prev["headline"], "content_raw": prev["summary"]})
        system, user = story._build_prompt(prev, arts, arts[-1]["id"], force_full=False)
        schema = story._RATED_SCHEMA
    else:
        system, user = story._build_prompt(None, arts, None, force_full=True)
        schema = story._SCHEMA
    out = ollama.structured(schema, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                            temperature=0.2, num_predict=1536, prose_fallback=True, model=config.STORY_MODEL)
    if isinstance(out, list):
        out = next((x for x in out if isinstance(x, dict)), None)
    if not isinstance(out, dict) or any(k not in out for k in schema["required"]):
        raise ValueError(f"reply misses required fields: {str(out)[:200]}")
    return out


def check_story(cases: list[dict]) -> tuple[dict, list[float], list[str]]:
    valid = checks = passed = sig_total = sig_ok = 0
    times, notes = [], []
    for case in cases:
        out, secs, err = timed(_story_call, case)
        times.append(secs)
        if out is None:
            notes.append(f"{case['id']}: {err}")
            continue
        valid += 1
        exp, metrics = case["expect"], out.get("metrics") or {}
        summary = (out.get("summary") or "").strip()
        results = {
            "headline": bool((out.get("headline") or "").strip()) and ";" not in out["headline"],
            "summary length": 0 < len(summary) <= config.SUMMARY_MAX_CHARS,
        }
        if "language" in exp:
            results["language"] = str(out.get("language", "")).lower() in exp["language"]
        if "location" in exp:
            results["location"] = any(p in str(out.get("location", "")).lower() for p in exp["location"])
        if "status" in exp:
            results["status"] = metrics.get("status") in exp["status"]
        if "impact_scale" in exp:
            results["impact_scale"] = metrics.get("impact_scale") in exp["impact_scale"]
        if "urgency" in exp:
            lo, hi = exp["urgency"]
            results["urgency"] = isinstance(metrics.get("urgency_level"), int) and lo <= metrics["urgency_level"] <= hi
        for name, ok in results.items():
            checks += 1
            passed += ok
            if not ok:
                notes.append(f"{case['id']}: {name} off ({json.dumps(out, ensure_ascii=False)[:160]}…)")
        if "significance" in exp:
            sig_total += 1
            sig = out.get("update_significance")
            lo, hi = exp["significance"]
            ok = isinstance(sig, int) and lo <= sig <= hi
            sig_ok += ok
            if not ok:
                notes.append(f"{case['id']}: update_significance {sig}, want {lo}-{hi}")
    result = {"valid": ratio(valid, len(cases)), "checks": ratio(passed, checks)}
    if sig_total:
        result["significance_acc"] = ratio(sig_ok, sig_total)
    return result, times, notes


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1.0)


def _embed_cases(cases: list[dict]) -> tuple[list[str], list[tuple[int, int, bool, str]]]:
    texts, pairs = [], []
    for case in cases:
        texts.append(f"{case['article']['title']} {case['article']['text']}")
        left = len(texts) - 1
        for c in case["candidates"]:
            texts.append(f"{c['headline']}. {c['summary']}")
            pairs.append((left, len(texts) - 1, c["expected"][0] in harness.MERGE_CLASSES, case["id"]))
    return texts, pairs


def compare_embed(cases: list[dict], ollama_model: str) -> None:
    """Same texts via NEWS_EMBED_BASE_URL and via Ollama native: are stored vectors still comparable?"""
    texts, _ = _embed_cases(cases)
    new = ollama.embed_texts(texts, model=config.EMBED_MODEL)
    base, config.EMBED_BASE_URL = config.EMBED_BASE_URL, ""
    try:
        old = ollama.embed_texts(texts, model=ollama_model)
    finally:
        config.EMBED_BASE_URL = base
    sims = [_cos(a, b) for a, b in zip(new, old)]
    advice = ("same vectors: switch without re-embedding" if min(sims) >= 0.99 else
              "close: thresholds still fit, old and new vectors mix slightly" if min(sims) >= 0.95 else
              "different: re-embed all articles before switching")
    print(f"\n== embed {config.EMBED_MODEL} @ {base or config.OLLAMA_BASE_URL} vs Ollama {ollama_model}\n"
          f"   dims {len(new[0])}/{len(old[0])}, cosine min {min(sims):.4f} mean {statistics.mean(sims):.4f}: {advice}")


def check_embed(cases: list[dict]) -> tuple[dict, list[float], list[str]]:
    texts, pairs = _embed_cases(cases)
    vecs, secs, err = timed(ollama.embed_texts, texts, model=config.EMBED_MODEL)
    if vecs is None:
        return {"auc": 0.0, "recall_at_retrieve": 0.0}, [secs], [err]
    notes = []
    dim = len(vecs[0])
    if dim != config.EMBED_DIM:
        notes.append(f"dimension {dim} != NEWS_EMBED_DIM {config.EMBED_DIM}: set NEWS_EMBED_DIM and re-embed")
    pos = [_cos(vecs[a], vecs[b]) for a, b, same, _ in pairs if same]
    neg = [_cos(vecs[a], vecs[b]) for a, b, same, _ in pairs if not same]
    auc = ratio(sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg), len(pos) * len(neg))
    retrieve = config.CLUSTER_VERIFY_RETRIEVE_THRESHOLD
    notes.append(f"same event cos {min(pos):.2f}-{max(pos):.2f} (mean {statistics.mean(pos):.2f}), "
                 f"other {min(neg):.2f}-{max(neg):.2f} (mean {statistics.mean(neg):.2f}); "
                 f"retrieval threshold {retrieve}, cosine join {config.CLUSTER_SIM_THRESHOLD}")
    return ({"auc": auc, "recall_at_retrieve": ratio(sum(p >= retrieve for p in pos), len(pos)),
             "dim_ok": float(dim == config.EMBED_DIM)}, [secs / len(texts)], notes)


def verdict(key: str, value: float) -> str:
    bar = BARS.get(key)
    if bar is None:
        return ""
    return "PASS" if value >= bar else "WARN" if value >= bar - 0.1 else "FAIL"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", help="LLM for every role (overrides NEWS_CHAT_MODEL and the role models)")
    p.add_argument("--verify-model")
    p.add_argument("--assess-model")
    p.add_argument("--story-model")
    p.add_argument("--embed-model")
    p.add_argument("--embed-dim", type=int, help="expected embedding dimension (default NEWS_EMBED_DIM)")
    p.add_argument("--embed-vs-ollama", metavar="OLLAMA_MODEL",
                   help="compare NEWS_EMBED_BASE_URL vectors with this Ollama embedding model (migration check)")
    p.add_argument("--only", default=",".join(ROLES), help=f"comma-separated subset of {','.join(ROLES)}")
    p.add_argument("--save", type=Path, help="write the scores as JSON (baseline for later runs)")
    p.add_argument("--baseline", type=Path, help="compare with scores saved by --save")
    p.add_argument("-v", "--verbose", action="store_true", help="list every miss")
    args = p.parse_args()

    if args.model:
        config.CHAT_MODEL = config.VERIFY_MODEL = config.ASSESS_MODEL = config.STORY_MODEL = args.model
    for role in ("verify", "assess", "story", "embed"):
        if value := getattr(args, f"{role}_model"):
            setattr(config, f"{role.upper()}_MODEL", value)
    if args.embed_dim:
        config.EMBED_DIM = args.embed_dim
    only = [r.strip() for r in args.only.split(",") if r.strip() in ROLES]
    if not ollama.available():
        print(f"Model server not reachable at {ollama.endpoint()} (OLLAMA_BASE_URL / NEWS_LLM_BASE_URL)")
        return 2

    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if args.embed_vs_ollama:
        compare_embed(cases["verify"], args.embed_vs_ollama)
    checks = {"verify": (check_verify, cases["verify"], config.VERIFY_MODEL),
              "assess": (check_assess, cases["assess"], config.ASSESS_MODEL),
              "story": (check_story, cases["story"], config.STORY_MODEL),
              "embed": (check_embed, cases["verify"], config.EMBED_MODEL)}
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else {}
    report: dict[str, Any] = {}
    worst = "PASS"
    for role in only:
        fn, data, model = checks[role]
        print(f"\n== {role} · {model}")
        scores, times, notes = fn(data)
        unit = "s/text" if role == "embed" else "s/call"
        print(f"   median {statistics.median(times):.2f} {unit}"
              + (f" (baseline {baseline[role]['median_s']:.2f})" if role in baseline else ""))
        for key, value in scores.items():
            v = verdict(f"{role}.{key}", value)
            if v == "FAIL" or (v == "WARN" and worst == "PASS"):
                worst = v
            base = baseline.get(role, {}).get("scores", {}).get(key)
            delta = f"  ({value - base:+.2f} vs baseline)" if base is not None else ""
            print(f"   {key:<20} {value:5.2f}  {v}{delta}")
        for note in notes if args.verbose else notes[:5]:
            print(f"   - {note}")
        if not args.verbose and len(notes) > 5:
            print(f"   - … {len(notes) - 5} more (-v)")
        report[role] = {"model": model, "scores": scores, "median_s": statistics.median(times)}
    if args.save:
        args.save.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nOverall: {worst}")
    return 1 if worst == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
