"""Experimental media / outlet analysis (quality metrics + budgeted LLM bias sampling).

Designed as a self-contained, toggleable module (``NEWS_MEDIA_ANALYSIS_ENABLED=1``):
- Objective SQL metrics (0 LLM cost): article volume, full-text ratio, average quality on
  full-text articles, first-to-report rate on multi-outlet stories, duplicate/copy ratio,
  exclusive story share, top topics, and "blind spots" (major stories covered by >=4 other
  outlets that this outlet missed).
- Budgeted LLM sampling (capped below ``NEWS_MEDIA_ANALYSIS_MAX_LLM_SHARE``, default 8% of
  7-day LLM tokens): analyzes a balanced sample of full-text articles *without* revealing the
  outlet/domain name in the prompt, recording political compass coordinates (economic &
  social axes, only when political), tone, clickbait score (0..10), and favored/criticized
  people or groups into ``ArticleBias``.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable
from typing import Any

from . import config, db, ollama, scoring

log = logging.getLogger("news.media-analysis")

TONES = ("factual", "emotional", "alarmist", "opinionated")
_MAX_WORDS = 550
# Non-editorial aggregators / academic preprints / encyclopedias are excluded from LLM bias sampling.
_NON_PUBLISHER_CATEGORIES = frozenset({"reddit", "hacker news", "journals", "wikipedia"})

_SCHEMA = {
    "type": "object",
    "properties": {
        "political": {"type": "boolean"},
        "economic_axis": {"type": ["number", "null"], "minimum": -1.0, "maximum": 1.0},
        "social_axis": {"type": ["number", "null"], "minimum": -1.0, "maximum": 1.0},
        "tone": {"type": "string", "enum": list(TONES)},
        "clickbait": {"type": "integer", "minimum": 0, "maximum": 10},
        "favored": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "criticized": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["political", "tone", "clickbait", "favored", "criticized"],
}

_SYSTEM = (
    "You are an objective media analyst evaluating a single news article (source anonymized). "
    "Judge ONLY from the article text and headline — do not guess the publisher.\n"
    "- political: true if the article has political, economic policy, or societal framing; "
    "false for purely technical, sports, weather, science, or neutral factual notices.\n"
    "- economic_axis: if political, rate the article's framing from -1.0 (strongly left / "
    "state-interventionist / redistributive) through 0.0 (balanced/neutral) to +1.0 (strongly "
    "market-liberal / deregulatory / pro-business). Set null if not political.\n"
    "- social_axis: if political, rate from -1.0 (strongly progressive / civil-libertarian) "
    "through 0.0 (balanced/neutral) to +1.0 (strongly conservative / law-and-order / traditional). "
    "Set null if not political.\n"
    "- tone: 'factual' (neutral reporting), 'emotional', 'alarmist', or 'opinionated'.\n"
    "- clickbait: integer 0-10 judging headline manipulation vs the body (do NOT penalize factual "
    "reporting of genuinely dramatic/urgent real events):\n"
    "  0-1 = plain, accurate headline stating the core fact directly;\n"
    "  2-3 = mild teaser, emotional color, or punchy phrasing that still matches the body;\n"
    "  4-6 = curiosity-gap withholding the key fact ('Darum...', 'Dieses Detail...', 'Was jetzt droht', "
    "teasing a mystery/pattern without naming it), question-bait, or noticeable exaggeration vs body;\n"
    "  7-8 = heavy sensationalism/hype ('SCHOCK', 'Hammer', ALL-CAPS, '?!') or headline overpromising "
    "what the body hedges/buries;\n"
    "  9-10 = blatantly misleading or contradicted by the body.\n"
    "- favored: up to 3 specific people, parties, companies, or groups clearly portrayed "
    "favorably or strengthened by the framing (empty list if neutral).\n"
    "- criticized: up to 3 specific people, parties, companies, or groups clearly criticized "
    "or portrayed negatively (empty list if neutral). Use concise canonical names."
)


def _outlet_of_feeds(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(fid): scoring.outlet_key(site, xml) or f"feed:{fid}"
        for fid, site, xml in conn.execute("SELECT id, site_url, xml_url FROM Feed")
    }


def budget_status(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    """Current 7-day LLM token share spent on ``role='analysis'`` vs the configured cap."""
    since = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days - 1)).isoformat()
    row = conn.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN role = 'analysis' THEN in_tokens + out_tokens ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN role != 'embedding' THEN in_tokens + out_tokens ELSE 0 END), 0) "
        "FROM ModelTokenStats WHERE day >= ?",
        (since,),
    ).fetchone()
    analysis_tok = int(row[0] or 0)
    total_llm_tok = int(row[1] or 0)
    share = (analysis_tok / total_llm_tok) if total_llm_tok > 0 else 0.0
    total_analyzed = int(conn.execute("SELECT count(*) FROM ArticleBias").fetchone()[0])
    cap = max(0.0, min(1.0, config.MEDIA_ANALYSIS_MAX_LLM_SHARE))
    within = True if cap <= 0 else ((share < cap) if total_llm_tok > 0 else (total_analyzed < 5))
    return {
        "enabled": config.MEDIA_ANALYSIS_ENABLED,
        "analysis_tokens_7d": analysis_tok,
        "total_llm_tokens_7d": total_llm_tok,
        "share": round(share, 4),
        "max_share": cap,
        "within_budget": within,
        "total_analyzed": total_analyzed,
    }


def _clean_entities(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = " ".join(str(item or "").split()).strip(" ,.;:-")[:60]
        if not name or len(name) < 2:
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append(name)
        if len(out) >= 3:
            break
    return out


def _clamp_axis(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return round(max(-1.0, min(1.0, float(val))), 3)
    except (TypeError, ValueError):
        return None


def analyze_article(conn: sqlite3.Connection, article: dict[str, Any], outlet: str) -> dict[str, Any] | None:
    """Run one anonymized structured LLM call on ``article`` and persist into ``ArticleBias``."""
    title = (article.get("title") or "").strip()
    body = " ".join(str(article.get("content_raw") or "").split()[:_MAX_WORDS])
    if not title or len(body) < 180:
        return None

    res = ollama.structured(
        _SCHEMA,
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Headline: {title}\n\nArticle:\n{body}"},
        ],
        temperature=0.0,
        num_predict=300,
        timeout=config.HARNESS_TIMEOUT,
        prose_fallback=True,
        model=config.MEDIA_ANALYSIS_MODEL,
        role="analysis",
    )
    if not isinstance(res, dict):
        return None

    political = bool(res.get("political"))
    econ = _clamp_axis(res.get("economic_axis")) if political else None
    soc = _clamp_axis(res.get("social_axis")) if political else None
    tone = res.get("tone") if res.get("tone") in TONES else "factual"
    try:
        clickbait = max(0, min(10, int(round(float(res.get("clickbait") or 0)))))
    except (TypeError, ValueError):
        clickbait = 0
    favored = _clean_entities(res.get("favored"))
    criticized = _clean_entities(res.get("criticized"))

    conn.execute(
        "INSERT OR REPLACE INTO ArticleBias "
        "(article_id, outlet, political, economic_axis, social_axis, tone, clickbait, favored, criticized) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            article["id"],
            outlet,
            int(political),
            econ,
            soc,
            tone,
            clickbait,
            json.dumps(favored, ensure_ascii=False),
            json.dumps(criticized, ensure_ascii=False),
        ),
    )
    return {
        "article_id": article["id"],
        "outlet": outlet,
        "political": political,
        "economic_axis": econ,
        "social_axis": soc,
        "tone": tone,
        "clickbait": clickbait,
        "favored": favored,
        "criticized": criticized,
    }


def _pick_candidates(conn: sqlite3.Connection, limit: int) -> list[tuple[dict[str, Any], str]]:
    """Pick unanalyzed full-text articles balanced across news publishers with fewest samples first."""
    outlet_of = _outlet_of_feeds(conn)
    skip_feeds = {
        int(fid) for fid, cat in conn.execute("SELECT id, COALESCE(category, '') FROM Feed")
        if cat.strip().lower() in _NON_PUBLISHER_CATEGORIES
    }
    counts = dict(conn.execute("SELECT outlet, count(*) FROM ArticleBias GROUP BY outlet").fetchall())
    rows = conn.execute(
        "SELECT a.id, a.feed_id, a.title, a.content_raw, a.created_at "
        "FROM Article a "
        "LEFT JOIN ArticleBias b ON b.article_id = a.id "
        "WHERE b.article_id IS NULL "
        "  AND a.content_class = 'ok' "
        "  AND a.duplicate_of IS NULL "
        "  AND a.story_id IS NOT NULL "
        "  AND length(COALESCE(a.content_raw, '')) >= 250 "
        "  AND a.created_at >= datetime('now', '-14 days') "
        "ORDER BY a.created_at DESC LIMIT 200"
    ).fetchall()

    # Prefer outlets with fewer existing samples so small/medium publishers catch up quickly.
    by_outlet: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        fid = int(r["feed_id"])
        if fid in skip_feeds:
            continue
        out = outlet_of.get(fid)
        if not out:
            continue
        by_outlet.setdefault(out, []).append(dict(r))

    picked: list[tuple[dict[str, Any], str]] = []
    local_counts = dict(counts)
    while len(picked) < limit and by_outlet:
        best_outlet = min(by_outlet, key=lambda o: (local_counts.get(o, 0), o))
        art = by_outlet[best_outlet].pop(0)
        if not by_outlet[best_outlet]:
            del by_outlet[best_outlet]
        picked.append((art, best_outlet))
        local_counts[best_outlet] = local_counts.get(best_outlet, 0) + 1
    return picked


def run(
    max_articles: int | None = None,
    *,
    respect_budget: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Analyze a small budgeted batch of unanalyzed full-text articles."""
    limit = max(1, int(max_articles if max_articles is not None else config.MEDIA_ANALYSIS_MAX_PER_RUN))
    analyzed = 0
    skipped_budget = False
    with db.get_conn() as conn:
        b = budget_status(conn)
        if respect_budget and (not config.MEDIA_ANALYSIS_ENABLED or not b["within_budget"]):
            return {"analyzed": 0, "skipped_budget": True, "budget": b}
        candidates = _pick_candidates(conn, limit)

    for art, outlet in candidates:
        if should_stop and should_stop():
            break
        with db.get_conn() as conn:
            if respect_budget and not budget_status(conn)["within_budget"]:
                skipped_budget = True
                break
            try:
                if analyze_article(conn, art, outlet) is not None:
                    analyzed += 1
            except Exception as e:  # noqa: BLE001
                log.warning("media analysis failed for article %s (%s): %s", art["id"], outlet, e)
                break
    with db.get_conn() as conn:
        b = budget_status(conn)
    return {"analyzed": analyzed, "skipped_budget": skipped_budget, "budget": b}


# --- Aggregations & SQL quality metrics --------------------------------------
def _compute_outlet_metrics(conn: sqlite3.Connection, days: int) -> dict[str, dict[str, Any]]:
    cutoff = f"-{int(days)} days"
    outlet_of = _outlet_of_feeds(conn)
    feed_titles: dict[str, list[str]] = {}
    for fid, title in conn.execute("SELECT id, title FROM Feed ORDER BY title"):
        o = outlet_of.get(int(fid))
        if o:
            feed_titles.setdefault(o, []).append(title)

    # 1. Article-level objective stats.
    rows = conn.execute(
        "SELECT id, feed_id, story_id, quality, content_class, duplicate_of, "
        "COALESCE(published_date, created_at) AS pub "
        "FROM Article WHERE created_at >= datetime('now', ?)",
        (cutoff,),
    ).fetchall()

    stats: dict[str, dict[str, Any]] = {}
    story_outlets: dict[int, list[tuple[str, str]]] = {}
    for r in rows:
        o = outlet_of.get(int(r["feed_id"]))
        if not o:
            continue
        st = stats.setdefault(o, {
            "outlet": o,
            "feeds": feed_titles.get(o, []),
            "articles": 0,
            "fulltext": 0,
            "duplicates": 0,
            "ok_qualities": [],
            "all_qualities": [],
            "stories": set(),
            "first_hits": 0,
            "multi_stories": 0,
            "exclusive_stories": 0,
        })
        st["articles"] += 1
        if r["content_class"] == "ok":
            st["fulltext"] += 1
            if r["quality"] is not None:
                st["ok_qualities"].append(float(r["quality"]))
        if r["quality"] is not None:
            st["all_qualities"].append(float(r["quality"]))
        if r["duplicate_of"] is not None:
            st["duplicates"] += 1
        if r["story_id"] is not None:
            sid = int(r["story_id"])
            st["stories"].add(sid)
            story_outlets.setdefault(sid, []).append((o, str(r["pub"] or "")))

    # First-to-report & exclusive story shares.
    for sid, entries in story_outlets.items():
        distinct = {}
        for o, pub in entries:
            if o not in distinct or (pub and (not distinct[o] or pub < distinct[o])):
                distinct[o] = pub
        if len(distinct) == 1:
            only = next(iter(distinct))
            stats[only]["exclusive_stories"] += 1
        elif len(distinct) >= 2:
            earliest = min(distinct.items(), key=lambda kv: kv[1] or "9999")[0]
            for o in distinct:
                stats[o]["multi_stories"] += 1
            stats[earliest]["first_hits"] += 1

    # Top tags per outlet (from their stories).
    all_sids = list(story_outlets)
    tag_map: dict[int, list[str]] = {}
    if all_sids:
        for sid, raw_tags in conn.execute(
            "SELECT id, tags FROM Story WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(all_sids),),
        ):
            tag_map[int(sid)] = db.json_list(raw_tags)

    # 2. LLM ArticleBias aggregates.
    bias_rows = conn.execute(
        "SELECT outlet, political, economic_axis, social_axis, tone, clickbait, favored, criticized "
        "FROM ArticleBias WHERE analyzed_at >= datetime('now', ?)",
        (cutoff,),
    ).fetchall()
    bias_by_outlet: dict[str, dict[str, Any]] = {}
    for r in bias_rows:
        o = r["outlet"]
        b = bias_by_outlet.setdefault(o, {
            "analyzed": 0,
            "political": 0,
            "econ": [],
            "soc": [],
            "tones": Counter(),
            "clickbaits": [],
            "favored": Counter(),
            "criticized": Counter(),
        })
        b["analyzed"] += 1
        b["tones"][r["tone"]] += 1
        b["clickbaits"].append(int(r["clickbait"] or 0))
        if r["political"]:
            b["political"] += 1
            if r["economic_axis"] is not None:
                b["econ"].append(float(r["economic_axis"]))
            if r["social_axis"] is not None:
                b["soc"].append(float(r["social_axis"]))
        for name in db.json_list(r["favored"]):
            if name:
                b["favored"][str(name)] += 1
        for name in db.json_list(r["criticized"]):
            if name:
                b["criticized"][str(name)] += 1

    out: dict[str, dict[str, Any]] = {}
    for o, st in stats.items():
        n = st["articles"] or 1
        quals = st["ok_qualities"] or st["all_qualities"]
        tags_c: Counter[str] = Counter()
        for sid in st["stories"]:
            for t in tag_map.get(sid, []):
                tags_c[t] += 1
        b = bias_by_outlet.get(o)
        avg_econ = round(sum(b["econ"]) / len(b["econ"]), 2) if b and b["econ"] else None
        avg_soc = round(sum(b["soc"]) / len(b["soc"]), 2) if b and b["soc"] else None
        avg_cb = round(sum(b["clickbaits"]) / len(b["clickbaits"]), 1) if b and b["clickbaits"] else None
        out[o] = {
            "outlet": o,
            "feeds": st["feeds"],
            "articles": st["articles"],
            "stories": len(st["stories"]),
            "fulltext_pct": round(100 * st["fulltext"] / n),
            "duplicate_pct": round(100 * st["duplicates"] / n),
            "avg_quality": round(sum(quals) / len(quals), 1) if quals else None,
            "first_pct": round(100 * st["first_hits"] / st["multi_stories"]) if st["multi_stories"] else None,
            "exclusive_pct": round(100 * st["exclusive_stories"] / len(st["stories"])) if st["stories"] else None,
            "top_tags": [t for t, _ in tags_c.most_common(6)],
            "analyzed": b["analyzed"] if b else 0,
            "political_n": b["political"] if b else 0,
            "economic_axis": avg_econ,
            "social_axis": avg_soc,
            "avg_clickbait": avg_cb,
            "tones": dict(b["tones"]) if b else {},
            "favored": [{"name": k, "count": c} for k, c in b["favored"].most_common(6)] if b else [],
            "criticized": [{"name": k, "count": c} for k, c in b["criticized"].most_common(6)] if b else [],
        }
    return out


def outlets_overview(days: int = 30) -> dict[str, Any]:
    with db.get_conn() as conn:
        metrics = _compute_outlet_metrics(conn, days)
        budget = budget_status(conn)
    outlets = sorted(metrics.values(), key=lambda x: (-x["articles"], x["outlet"]))
    return {"days": days, "budget": budget, "outlets": outlets}


def outlet_detail(outlet: str, days: int = 30) -> dict[str, Any] | None:
    cutoff = f"-{int(days)} days"
    with db.get_conn() as conn:
        metrics = _compute_outlet_metrics(conn, days)
        summary = metrics.get(outlet)
        if summary is None:
            return None

        outlet_of = _outlet_of_feeds(conn)
        my_feed_ids = [fid for fid, o in outlet_of.items() if o == outlet]

        # Blind spots: major stories (>=4 other outlets) that this outlet did not cover.
        blind_spots: list[dict[str, Any]] = []
        story_rows = conn.execute(
            "SELECT s.id, s.headline, s.quality_score, s.news_at, s.metrics "
            "FROM Story s WHERE s.headline IS NOT NULL AND s.news_at >= datetime('now', ?) "
            "ORDER BY s.news_at DESC LIMIT 300",
            (cutoff,),
        ).fetchall()
        if story_rows:
            sids = [int(r["id"]) for r in story_rows]
            outlets_by_story: dict[int, set[str]] = {}
            for sid, fid in conn.execute(
                "SELECT DISTINCT story_id, feed_id FROM Article "
                "WHERE story_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sids),),
            ):
                o = outlet_of.get(int(fid))
                if o:
                    outlets_by_story.setdefault(int(sid), set()).add(o)
            for r in story_rows:
                sid = int(r["id"])
                outs = outlets_by_story.get(sid, set())
                if len(outs) >= 4 and outlet not in outs:
                    m = db.json_dict(r["metrics"])
                    blind_spots.append({
                        "id": sid,
                        "headline": r["headline"],
                        "outlets": len(outs),
                        "urgency": m.get("urgency_level") or 5,
                        "news_at": r["news_at"],
                    })
            blind_spots.sort(key=lambda x: (-x["outlets"], -x["urgency"]))
            blind_spots = blind_spots[:10]

        # Recent analyzed articles for this outlet.
        samples = [
            {
                "article_id": r["article_id"],
                "story_id": r["story_id"],
                "title": r["title"],
                "url": r["url"],
                "political": bool(r["political"]),
                "economic_axis": r["economic_axis"],
                "social_axis": r["social_axis"],
                "tone": r["tone"],
                "clickbait": r["clickbait"],
                "favored": db.json_list(r["favored"]),
                "criticized": db.json_list(r["criticized"]),
                "analyzed_at": r["analyzed_at"],
            }
            for r in conn.execute(
                "SELECT b.*, a.title, a.url, a.story_id FROM ArticleBias b "
                "JOIN Article a ON a.id = b.article_id "
                "WHERE b.outlet = ? ORDER BY b.analyzed_at DESC LIMIT 15",
                (outlet,),
            ).fetchall()
        ]

        feeds_list = [
            {"id": r["id"], "title": r["title"], "xml_url": r["xml_url"]}
            for r in conn.execute(
                "SELECT id, title, xml_url FROM Feed WHERE id IN (SELECT value FROM json_each(?)) ORDER BY title",
                (json.dumps(my_feed_ids),),
            ).fetchall()
        ] if my_feed_ids else []

    return {
        "days": days,
        "summary": summary,
        "feeds": feeds_list,
        "blind_spots": blind_spots,
        "samples": samples,
    }


def feed_summaries(conn: sqlite3.Connection, days: int = 30) -> dict[str, dict[str, Any]]:
    """Compact per-outlet summary for badges on the /feeds page."""
    metrics = _compute_outlet_metrics(conn, days)
    return {
        o: {
            "outlet": o,
            "avg_quality": m["avg_quality"],
            "analyzed": m["analyzed"],
            "economic_axis": m["economic_axis"],
            "social_axis": m["social_axis"],
            "avg_clickbait": m["avg_clickbait"],
        }
        for o, m in metrics.items()
    }
