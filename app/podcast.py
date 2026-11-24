"""Local AI news podcast generator (show outline -> radio-style script -> TTS audio).

Takes the top ranked stories (up to 50) for a user's personal or public feed, plans a
multi-chapter show scaled to the requested duration (5..120 minutes) with thematic blocks,
transitions, mid-show recaps on longer episodes, and a quick roundup, then synthesizes
each segment via ``app/tts.py`` with pauses and chapter chimes.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import logging
import re
import secrets
import threading
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_esc

from . import config, db, diversity, ollama, scoring, tts

log = logging.getLogger("news.podcast")

WORDS_PER_MINUTE = 130  # natural spoken news pace including pauses
MAX_STORIES_CAP = 50
_ACTIVE_STATUSES = ("queued", "scripting", "synthesizing")

STYLES = {
    "informative": "calm, clear, objective NPR / Deutschlandfunk-style news journalism",
    "entertaining": "lively, warm, witty, and engaging conversational storytelling (while staying strictly factual)",
    "analytical": "thoughtful, in-depth analysis focusing on causes, context, and broader implications",
    "briefing": "crisp, brisk, high-density executive briefing with zero filler or small talk",
}
STYLE_GUIDES_DE = {
    "informative": (
        "STIL-VORGABE (Informativ — Deutschlandfunk / öffentlich-rechtlicher Nachrichtenjournalismus): "
        "Sachlich, ruhig, ausgewogen und präzise. Klare Trennung von Fakten und Einordnung, "
        "seriöse Sprache ohne Floskeln, saubere thematische Übergänge, keine künstliche Aufgeregtheit."
    ),
    "entertaining": (
        "STIL-VORGABE (Unterhaltsam — lebendiger Erzähl- & Gesprächs-Podcast): "
        "Warm, pointiert, geistreich und mitreißend! Nutze anschauliche Bilder, leichte Ironie, "
        "rhetorische Fragen, kurze Pointen und im Dialog echte Schlagfertigkeit — aber bleibe bei allen "
        "Fakten zu 100 % korrekt und erfinde nichts hinzu."
    ),
    "analytical": (
        "STIL-VORGABE (Analytisch — tiefgehende Hintergrund- & Einordnungs-Sendung): "
        "Gehe konsequent über das bloße Nacherzählen von Schlagzeilen hinaus. Erkläre Ursachen, "
        "strukturelle Zusammenhänge, Interessen der Akteure, mögliche Folgeeffekte und was als Nächstes "
        "zu beobachten ist. Weniger Oberfläche, deutlich mehr Tiefe und Kausalität."
    ),
    "briefing": (
        "STIL-VORGABE (Executive Briefing — MAXIMALE Informationsdichte, NULL Geplänkel): "
        "ABSOLUT KEIN Smalltalk, KEIN Geplänkel, KEINE Höflichkeitsfloskeln ('Wie geht's?', "
        "'Schön, dass ihr dabei seid', 'Kommen wir nun zu einem spannenden Thema', 'Was für eine Woche'). "
        "Steige sofort mit harten Fakten, Zahlen, Entscheidungen und konkreten Folgen ein. "
        "Kurze, knappe, messerscharfe Sätze. Direkte, nüchterne Themenwechsel ('Nächstes Thema:', "
        "'Zur Wirtschaft:', 'Aus der Technik:'). Selbst im Dialog: zwei fokussierte Analysten im "
        "schnellen Fakten-Pingpong ohne Plauderei."
    ),
}
STYLE_GUIDES_EN = {
    "informative": (
        "STYLE DIRECTIVE (Informative — NPR / public-radio journalism): "
        "Calm, authoritative, balanced, and clear. Separate facts from context cleanly, "
        "use crisp journalistic transitions, and avoid hype or filler."
    ),
    "entertaining": (
        "STYLE DIRECTIVE (Entertaining — vivid, witty conversational storytelling): "
        "Energetic, warm, and engaging! Use crisp analogies, clever observations, rhetorical questions, "
        "and lively back-and-forth energy in dialogue — while staying 100% faithful to the facts."
    ),
    "analytical": (
        "STYLE DIRECTIVE (Analytical — deep-dive background and implications): "
        "Go well beyond summarizing headlines. Unpack root causes, incentives, competing perspectives, "
        "second-order consequences, and what to watch next. Prioritize depth and causality."
    ),
    "briefing": (
        "STYLE DIRECTIVE (Executive Briefing — MAXIMUM density, ZERO banter or filler): "
        "ABSOLUTELY NO small talk, NO chit-chat, NO pleasantries ('How are you?', 'Great to be here', "
        "'Moving on to another fascinating topic'). Lead immediately with hard facts, numbers, decisions, "
        "and concrete implications. Short, punchy sentences and crisp pivots ('Next:', 'Markets:', 'Tech:'). "
        "Even in dialogue mode: two brisk analysts trading high-signal facts with zero banter."
    ),
}
FORMATS = ("solo", "dialogue")

_gen_lock = threading.Lock()
_stops_lock = threading.Lock()
_stops: dict[int, threading.Event] = {}


def recommended_story_count(duration_minutes: int) -> int:
    """Sensible default story count for a target duration (1..180 min)."""
    if duration_minutes <= 3:
        return 4
    if duration_minutes <= 8:
        return 8
    if duration_minutes <= 20:
        return 16
    if duration_minutes <= 40:
        return 28
    if duration_minutes <= 75:
        return 40
    return MAX_STORIES_CAP


def _language_name(code: str) -> str:
    c = (code or "").strip().lower()
    return {
        "de": "German",
        "en": "English",
        "fr": "French",
        "es": "Spanish",
        "it": "Italian",
        "nl": "Dutch",
    }.get(c, "German" if not c else c)


def _is_german(code: str) -> bool:
    return (code or "de").strip().lower().startswith("de")


def _fmt_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "0m"
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def clean_for_tts(text: str) -> str:
    """Strip thinking tags, markdown, URLs and stage directions so TTS voices read cleanly."""
    if not text:
        return ""
    s = ollama.strip_thinking(text)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"https?://\S+", "", s)
    # Strip bracketed stage directions like [Musik], [Pause], (lacht), (kurze Pause).
    s = re.sub(r"\[(?:musik|jingle|pause|applaus|lacht|intro|outro|music|sound|sfx|chime)[^\]]*\]", "", s, flags=re.I)
    s = re.sub(r"\((?:musik|jingle|pause|kurze pause|lacht|schmunzelt|seufzt|music|laughs|chuckles|pause)\)", "", s, flags=re.I)
    s = re.sub(r"[*_`#>]+", "", s)
    s = re.sub(r"^\s*[-•–—]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def dialogue_chunks(text: str, lang: str, max_chars: int = 600) -> list[dict[str, Any]]:
    """Parse ``HOST1:`` / ``HOST2:`` turns into TTS chunk dicts with speaker host index."""
    turns: list[tuple[int, str]] = []
    cur_host = 1
    buf: list[str] = []
    for raw_line in clean_for_tts(text).splitlines():
        line = raw_line.strip()
        if not line:
            if buf:
                turns.append((cur_host, " ".join(buf)))
                buf = []
            continue
        m = re.match(r"^(?:HOST\s*([12])|Host\s*([12])|Speaker\s*([12])|Moderation|Co-Moderation)\s*:\s*(.*)$", line, re.I)
        if m:
            if buf:
                turns.append((cur_host, " ".join(buf)))
                buf = []
            low = line.lower()
            if low.startswith("co-moderation"):
                cur_host = 2
            elif low.startswith("moderation"):
                cur_host = 1
            else:
                cur_host = int(m.group(1) or m.group(2) or m.group(3) or 1)
            rest = (m.group(4) or "").strip()
            if rest:
                buf.append(rest)
        else:
            buf.append(line)
    if buf:
        turns.append((cur_host, " ".join(buf)))

    out: list[dict[str, Any]] = []
    for host, body in turns:
        for piece in split_paragraphs(body, max_chars=max_chars):
            out.append({"text": piece, "host": 2 if host == 2 else 1})
    return out


def format_dialogue_transcript(text: str, lang: str) -> str:
    """Normalize HOST1/HOST2 prefixes into readable labels for the stored transcript."""
    h1, h2 = ("Moderation", "Co-Moderation") if _is_german(lang) else ("Host 1", "Host 2")
    s = re.sub(r"(?im)^HOST\s*1\s*:", f"{h1}:", clean_for_tts(text))
    s = re.sub(r"(?im)^HOST\s*2\s*:", f"{h2}:", s)
    return s


def split_paragraphs(text: str, max_chars: int = 600) -> list[str]:
    """Split cleaned script text into TTS-sized paragraph/sentence chunks."""
    chunks: list[str] = []
    for raw_para in re.split(r"\n\s*\n", clean_for_tts(text)):
        para = " ".join(raw_para.split()).strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        # Split overly long paragraphs on sentence boundaries.
        sentences = re.split(r"(?<=[.!?])\s+", para)
        cur = ""
        for sent in sentences:
            if not sent:
                continue
            if cur and len(cur) + 1 + len(sent) > max_chars:
                chunks.append(cur)
                cur = sent
            else:
                cur = f"{cur} {sent}".strip() if cur else sent
        if cur:
            chunks.append(cur)
    return chunks


# --- Story gathering & show planning ----------------------------------------
def _fetch_stories(
    user_id: int,
    view: str,
    max_stories: int,
    include_excerpts: bool,
    language_override: str | None = None,
) -> tuple[list[dict[str, Any]], str, str]:
    """Return ``(stories, about_user, language)``.

    For ``view == 'default'`` ("For everyone"), zero user-specific data is read:
    no ``about``, no personal ranking/read-state/exclusions, and server-default language
    when ``language_override`` is ``auto``.
    """
    personal = view == "personal"
    with db.get_conn() as conn:
        override = (language_override or "").strip().lower()
        if personal:
            profile = scoring.get_user_profile(conn, user_id)
            prefs = profile["preferences"]
            lang = (
                override if override and override != "auto"
                else (prefs.get("language") or config.PODCAST_LANGUAGE or config.USER_LANGUAGE or "de").strip().lower()
            )
            about = scoring.about_user(profile)
            ranked = scoring.ranked_stories(conn, personal=True, user_id=user_id, diverse=False)
        else:
            lang = (
                override if override and override != "auto"
                else (config.PODCAST_LANGUAGE or config.USER_LANGUAGE or "de").strip().lower()
            )
            about = ""
            ranked = scoring.ranked_stories(conn, personal=False, user_id=None, diverse=False)
        # Never report satire/parody stories (e.g. Der Postillon) as real news.
        ranked = [item for item in ranked if not db.json_dict(item["story"]["metrics"]).get("satire")]
        top = diversity.rerank(conn, ranked)[:max_stories]
        if not top:
            return [], about, lang

        ids = [int(item["story"]["id"]) for item in top]
        ph = ",".join("?" * len(ids))

        updates_by_story: dict[int, list[str]] = {}
        for sid, facts_raw in conn.execute(
            f"SELECT story_id, facts FROM StoryUpdate WHERE story_id IN ({ph}) ORDER BY created_at DESC",
            ids,
        ).fetchall():
            lst = updates_by_story.setdefault(sid, [])
            if len(lst) < 3:
                for f in db.json_list(facts_raw):
                    if f and len(lst) < 3:
                        lst.append(str(f))

        excerpts_by_story: dict[int, list[str]] = {}
        if include_excerpts:
            for sid, title, content in conn.execute(
                f"SELECT story_id, title, content_raw FROM Article "
                f"WHERE story_id IN ({ph}) AND duplicate_of IS NULL "
                f"ORDER BY quality DESC, published_date DESC",
                ids,
            ).fetchall():
                lst = excerpts_by_story.setdefault(sid, [])
                if len(lst) < 2 and content:
                    snippet = " ".join(str(content).split())[:700]
                    if snippet:
                        lst.append(f"{title}: {snippet}")

    stories: list[dict[str, Any]] = []
    for idx, item in enumerate(top, 1):
        s = item["story"]
        sid = int(s["id"])
        metrics = db.json_dict(s["metrics"])
        stories.append({
            "rank": idx,
            "id": sid,
            "headline": s["headline"] or "",
            "summary": s["summary"] or "",
            "tags": db.json_list(s["tags"])[:5],
            "urgency": metrics.get("urgency_level") or 3,
            "status": metrics.get("status") or "Ongoing",
            "impact": metrics.get("impact_scale") or "National",
            "location": metrics.get("location") or "",
            "updates": updates_by_story.get(sid, []),
            "excerpts": excerpts_by_story.get(sid, []),
        })
    return stories, about, lang


_OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "show_title": {"type": "string"},
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "story_ranks": {"type": "array", "items": {"type": "integer"}},
                    "angle": {"type": "string"},
                },
                "required": ["title", "story_ranks"],
            },
        },
    },
    "required": ["show_title", "blocks"],
}


def _plan_blocks(stories: list[dict[str, Any]], duration_minutes: int, lang: str) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Group top stories into thematic deep-dive blocks + optional quick roundup."""
    if duration_minutes <= 4:
        n_blocks, deep_cap = 1, min(len(stories), 4)
    elif duration_minutes <= 8:
        n_blocks, deep_cap = 2, min(len(stories), 6)
    elif duration_minutes <= 20:
        n_blocks, deep_cap = 3, min(len(stories), 10)
    elif duration_minutes <= 40:
        n_blocks, deep_cap = 4, min(len(stories), 18)
    elif duration_minutes <= 75:
        n_blocks, deep_cap = 5, min(len(stories), 26)
    else:
        n_blocks, deep_cap = 6, min(len(stories), 34)

    deep_stories = stories[:deep_cap]
    roundup_stories = stories[deep_cap:]
    by_rank = {s["rank"]: s for s in deep_stories}
    date_str = dt.datetime.now().strftime("%d.%m.%Y" if _is_german(lang) else "%Y-%m-%d")
    fallback_title = f"Nachrichten-Podcast · {date_str}" if _is_german(lang) else f"News Podcast · {date_str}"

    # Try an LLM outline to cluster related stories into coherent thematic blocks.
    catalog = "\n".join(
        f"{s['rank']}. {s['headline']} [tags: {', '.join(s['tags'])}; {s['status']}]"
        for s in deep_stories
    )
    lang_name = _language_name(lang)
    de = _is_german(lang)
    blocks: list[dict[str, Any]] = []
    show_title = fallback_title
    try:
        outline_sys = (
            f"Du bist Redaktionsleiter für einen {duration_minutes}-minütigen Nachrichten-Podcast auf DEUTSCH. "
            f"Gruppiere die nummerierten Meldungen in {n_blocks} thematisch stimmige Kapitel (2–6 Meldungen pro Kapitel). "
            "Gib der Sendung und jedem Kapitel einen prägnanten, natürlichen DEUTSCHEN Titel (keine englischen Titel!) "
            "und einen kurzen redaktionellen Fokus. Jeder Rang von 1 bis "
            f"{len(deep_stories)} soll genau einmal vorkommen."
        ) if de else (
            f"You are an executive producer planning a {duration_minutes}-minute news podcast in {lang_name}. "
            f"Group the numbered stories into {n_blocks} coherent thematic show blocks (2-6 stories per block). "
            f"Give each block a punchy, natural {lang_name} chapter title and a short editorial angle. "
            f"Every story rank from 1 to {len(deep_stories)} should appear once."
        )
        res = ollama.structured(
            _OUTLINE_SCHEMA,
            [
                {"role": "system", "content": outline_sys},
                {"role": "user", "content": f"Date: {date_str}\nStories:\n{catalog}"},
            ],
            temperature=0.2,
            num_predict=700,
            prose_fallback=True,
            model=config.PODCAST_MODEL,
            role="podcast",
        )
        if isinstance(res, dict):
            if isinstance(res.get("show_title"), str) and res["show_title"].strip():
                show_title = res["show_title"].strip()[:120]
            used: set[int] = set()
            for raw in (res.get("blocks") or [])[:n_blocks + 1]:
                if not isinstance(raw, dict):
                    continue
                picks = []
                for r in raw.get("story_ranks") or []:
                    if isinstance(r, int) and r in by_rank and r not in used:
                        used.add(r)
                        picks.append(by_rank[r])
                if picks:
                    blocks.append({
                        "title": str(raw.get("title") or picks[0]["headline"])[:80].strip(),
                        "angle": str(raw.get("angle") or "").strip(),
                        "stories": picks,
                    })
            # Append any story the outline missed to the last block (or roundup).
            leftover = [s for s in deep_stories if s["rank"] not in used]
            if leftover:
                if blocks:
                    blocks[-1]["stories"].extend(leftover)
                else:
                    roundup_stories = leftover + roundup_stories
    except Exception as e:  # noqa: BLE001
        log.warning("podcast outline fallback: %s", e)

    if not blocks and deep_stories:
        # Deterministic fallback: slice deep_stories evenly into n_blocks.
        size = max(1, (len(deep_stories) + n_blocks - 1) // n_blocks)
        for i in range(0, len(deep_stories), size):
            chunk = deep_stories[i : i + size]
            label = chunk[0]["tags"][0].title() if chunk[0]["tags"] else f"Block {len(blocks) + 1}"
            blocks.append({"title": label, "angle": "", "stories": chunk})

    return show_title, blocks, roundup_stories


def _build_segments(
    stories: list[dict[str, Any]],
    duration_minutes: int,
    lang: str,
    *,
    style: str = "informative",
) -> tuple[str, list[dict[str, Any]]]:
    """Build ordered show segments with per-segment word budgets."""
    show_title, blocks, roundup = _plan_blocks(stories, duration_minutes, lang)
    total_words = max(130, duration_minutes * WORDS_PER_MINUTE)
    de = _is_german(lang)
    brief = style == "briefing"

    if brief:
        intro_words = max(20, min(70, int(total_words * 0.04)))
        outro_words = max(15, min(50, int(total_words * 0.03)))
        recap_words = max(60, min(140, int(total_words * 0.05))) if duration_minutes >= 45 and len(blocks) >= 4 else 0
    else:
        intro_words = max(35, min(260, int(total_words * 0.10)))
        outro_words = max(30, min(220, int(total_words * 0.09)))
        recap_words = max(90, min(240, int(total_words * 0.07))) if duration_minutes >= 30 and len(blocks) >= 3 else 0
    roundup_words = min(int(total_words * 0.22), max(60, len(roundup) * 40)) if roundup else 0
    body_words = max(80, total_words - intro_words - outro_words - recap_words - roundup_words)

    segments: list[dict[str, Any]] = [
        {
            "kind": "intro",
            "title": "Begrüßung & Überblick" if de else "Welcome & Headlines",
            "target_words": intro_words,
            "stories": stories[:5],
            "blocks": [b["title"] for b in blocks],
        }
    ]

    mid_idx = len(blocks) // 2 if recap_words else -1
    total_deep_stories = sum(len(b["stories"]) for b in blocks) or 1
    for i, block in enumerate(blocks):
        share = max(0.12, len(block["stories"]) / total_deep_stories)
        segments.append({
            "kind": "block",
            "title": block["title"],
            "angle": block.get("angle", ""),
            "target_words": max(70, int(body_words * share)),
            "stories": block["stories"],
        })
        if i + 1 == mid_idx:
            segments.append({
                "kind": "recap",
                "title": "Kurzer Rückblick" if de else "Mid-Show Recap",
                "target_words": recap_words,
                "stories": [s for b in blocks[: i + 1] for s in b["stories"][:2]][:6],
                "next_blocks": [b["title"] for b in blocks[i + 1 :]],
            })

    if roundup:
        segments.append({
            "kind": "roundup",
            "title": "Kurz gemeldet" if de else "Quick Roundup",
            "target_words": roundup_words,
            "stories": roundup,
        })

    segments.append({
        "kind": "outro",
        "title": "Zusammenfassung & Ausblick" if de else "Recap & Sign-Off",
        "target_words": outro_words,
        "stories": stories[:4],
    })
    return show_title, segments


def _story_brief_block(stories: list[dict[str, Any]], include_excerpts: bool) -> str:
    lines = []
    for s in stories:
        parts = [f"- [{s['status']}, urgency {s['urgency']}/10, {s['impact']}] {s['headline']}"]
        if s["summary"]:
            parts.append(f"  Summary: {s['summary']}")
        if s["updates"]:
            parts.append("  Recent updates: " + " | ".join(s["updates"]))
        if include_excerpts and s["excerpts"]:
            parts.append("  Source details: " + " / ".join(s["excerpts"]))
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def _chat_with_thinking(
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    max_tokens: int,
    label: str,
) -> str:
    """Call the LLM with thinking enabled, falling back to think=False only if content is empty."""
    text = ollama.chat(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model=config.PODCAST_MODEL,
        role="podcast",
        think=True,
    )
    cleaned = clean_for_tts(text)
    if cleaned:
        return cleaned
    log.warning("podcast %s returned empty content with thinking (max_tokens=%d); retrying without thinking",
                label, max_tokens)
    text = ollama.chat(
        messages,
        temperature=temperature,
        max_tokens=max(1200, min(4096, max_tokens)),
        model=config.PODCAST_MODEL,
        role="podcast",
        think=False,
    )
    cleaned = clean_for_tts(text)
    if not cleaned:
        log.warning("podcast %s returned empty content even without thinking", label)
    return cleaned


_EN_STOP = frozenset({
    "the", "and", "of", "to", "in", "for", "with", "that", "from", "this", "have", "has",
    "was", "were", "will", "would", "which", "their", "about", "after", "while", "more",
    "are", "been", "being", "into", "over", "also", "other", "than", "then", "they", "these",
    "those", "what", "when", "where", "who", "its", "said", "says", "new", "could", "should",
})
_DE_STOP = frozenset({
    "der", "die", "das", "und", "den", "dem", "ein", "eine", "einen", "einer", "nicht",
    "mit", "auf", "für", "von", "ist", "sind", "wird", "haben", "auch", "nach", "über", "bei",
    "sich", "als", "noch", "wie", "aber", "vor", "zur", "zum", "aus", "hat", "wurde", "werden",
})

_MID_GREETING_DE = re.compile(
    r"^(?:"
    r"(?:herzlich(?:en)?\s+)?willkommen(?:\s+(?:zurück|bei|zu|in))?[^.!?\n]{0,80}[.!?]\s*"
    r"|(?:hallo|hi|hey|servus|moin|guten\s+(?:morgen|tag|abend))(?:\s+(?:zusammen|alle|ihr\s+lieben|und\s+willkommen|liebe\s+hörer\w*))?(?:\s*[!.:]\s*|\s*[,–—-]\s*)"
    r"|schön,\s+dass\s+(?:ihr|sie)\s+(?:wieder\s+)?(?:dabei|eingeschaltet)[^.!?\n]{0,60}[.!?]\s*"
    r")+",
    re.I,
)
_MID_GREETING_EN = re.compile(
    r"^(?:"
    r"welcome(?:\s+back)?(?:\s+to)?[^.!?\n]{0,80}[.!?]\s*"
    r"|(?:hello|hi|hey|good\s+(?:morning|afternoon|evening))(?:\s+(?:everyone|folks|listeners|again))?(?:\s*[!.:]\s*|\s*[,–—-]\s*)"
    r")+",
    re.I,
)


def _strip_mid_show_greetings(text: str, *, lang: str, fmt: str) -> str:
    """Remove accidental greetings ('Hallo', 'Willkommen', 'Good morning') at the start of non-intro segments."""
    if not text:
        return text
    pat = _MID_GREETING_DE if _is_german(lang) else _MID_GREETING_EN
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        prefix = ""
        body = stripped
        if fmt == "dialogue":
            m = re.match(r"^((?:HOST\s*[12]|Host\s*[12]|Moderation|Co-Moderation)\s*:\s*)(.*)$", stripped, re.I)
            if m:
                prefix, body = m.group(1), m.group(2)
        cleaned_body = pat.sub("", body).strip()
        if cleaned_body != body:
            # Capitalize first letter if a leading clause ("Hallo zusammen, die ...") was stripped.
            if cleaned_body and cleaned_body[0].islower():
                cleaned_body = cleaned_body[0].upper() + cleaned_body[1:]
            if cleaned_body:
                lines[i] = f"{prefix}{cleaned_body}"
            else:
                lines[i] = ""
        break  # only check the opening utterance of the segment
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _looks_english(text: str) -> bool:
    """True when a supposedly German text/paragraph slipped predominantly into English."""
    words = re.findall(r"[a-zäöüß]{2,}", (text or "").lower())
    if len(words) < 7:
        return False
    en = sum(1 for w in words if w in _EN_STOP)
    de = sum(1 for w in words if w in _DE_STOP)
    return en >= 3 and en > de * 1.5


def _translate_to_german(text: str, *, fmt: str, label: str) -> str:
    fmt_note = " Behalte die Zeilenanfänge 'HOST1:' und 'HOST2:' exakt unverändert bei." if fmt == "dialogue" else ""
    try:
        translated = clean_for_tts(ollama.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Übersetze den folgenden Podcast-Sprechtext vollständig in natürliches, "
                        "flüssiges gesprochenes Deutsch. Behalte alle Fakten und Absatzumbrüche bei, "
                        f"kein Markdown, keine Kommentare.{fmt_note}"
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.1,
            max_tokens=max(1200, min(6144, int(len(text.split()) * 2.5) + 256)),
            model=config.PODCAST_MODEL,
            role="podcast",
            think=False,
        ))
        return translated or text
    except Exception as e:  # noqa: BLE001
        log.warning("podcast German recovery for %s failed: %s", label, e)
        return text


def _ensure_german(text: str, *, fmt: str, label: str) -> str:
    """Translate any English segment or English paragraphs inside a German segment back to German."""
    if not text:
        return text
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) > 1:
        en_indices = [i for i, p in enumerate(paras) if _looks_english(p)]
        if not en_indices:
            return text
        if len(en_indices) == len(paras):
            log.warning("podcast %s slipped into English; translating back to German", label)
            return _translate_to_german(text, fmt=fmt, label=label)
        log.warning("podcast %s has %d/%d English paragraph(s); translating to German", label, len(en_indices), len(paras))
        for idx in en_indices:
            paras[idx] = _translate_to_german(paras[idx], fmt=fmt, label=f"{label}#p{idx}")
        return "\n\n".join(p for p in paras if p.strip())

    if _looks_english(text):
        log.warning("podcast %s slipped into English; translating back to German", label)
        return _translate_to_german(text, fmt=fmt, label=label)
    return text


def _write_segment(
    seg: dict[str, Any],
    *,
    show_title: str,
    duration_minutes: int,
    lang: str,
    about: str,
    prior_summary: str,
    include_excerpts: bool,
    style: str = "informative",
    fmt: str = "solo",
) -> str:
    lang_name = _language_name(lang)
    de = _is_german(lang)
    target = seg["target_words"]
    date_str = dt.datetime.now().strftime("%d.%m.%Y" if de else "%B %d, %Y")
    style_key = style if style in STYLES else "informative"
    style_guide = STYLE_GUIDES_DE[style_key] if de else STYLE_GUIDES_EN[style_key]
    brief = style_key == "briefing"

    if de:
        if fmt == "dialogue":
            system = (
                f"Du schreibst einen täglichen Nachrichten-Podcast auf DEUTSCH für zwei Moderatoren.\n"
                f"{style_guide}\n"
                "FORMAT: HOST1 führt durch die Themen, HOST2 ergänzt, ordnet ein und stellt gezielte Nachfragen. "
                "Jede Sprechzeile beginnt mit 'HOST1:' oder 'HOST2:', getrennt durch eine Leerzeile.\n"
                "SPRACHE & REGELN: Schreibe AUSSCHLIESSLICH auf Deutsch — übersetze ALLE englischen Quellen, "
                "Zitate und Fachbegriffe vollständig ins Deutsche (kein Denglisch, keine englischen Sätze!). "
                "Nur exakter Sprechtext: keine Regieanweisungen wie [Musik] oder (Pause), kein Markdown, "
                "keine Aufzählungen, keine URLs. Bleibe strikt bei den Fakten der gelieferten Meldungen."
            )
        else:
            system = (
                f"Du moderierst einen täglichen Nachrichten-Podcast auf DEUTSCH.\n"
                f"{style_guide}\n"
                "SPRACHE & REGELN: Schreibe AUSSCHLIESSLICH auf Deutsch — übersetze ALLE englischen Quellen, "
                "Zitate und Fachbegriffe vollständig ins Deutsche (kein Denglisch, keine englischen Sätze!). "
                "Nur exakter Sprechtext: keine Regieanweisungen wie [Musik] oder (Pause), keine Sprecher-Labels, "
                "kein Markdown, keine Aufzählungen, keine URLs. "
                "Nutze klare, gut sprechbare Sätze mit Leerzeilen zwischen Gedanken für natürliche Pausen. "
                "Bleibe strikt bei den Fakten der gelieferten Meldungen."
            )
    elif fmt == "dialogue":
        system = (
            f"You are writing a two-host daily news podcast in {lang_name}.\n"
            f"{style_guide}\n"
            "FORMAT: HOST1 is the main anchor who frames the news; HOST2 adds sharp questions and context. "
            "Format EVERY turn on its own line starting with 'HOST1:' or 'HOST2:' and separate turns with a blank line.\n"
            "RULES: Write ONLY the exact spoken words — no stage directions, no markdown, no bullet points, no URLs, no hashtags. "
            "Stay strictly factual based on the provided stories."
        )
    else:
        system = (
            f"You are the solo host of a daily news podcast in {lang_name}.\n"
            f"{style_guide}\n"
            "RULES: Write ONLY the exact spoken words for this segment — no stage directions, no speaker labels, "
            "no markdown, no bullet points, no URLs, no hashtags. "
            "Separate distinct thoughts with blank lines so the audio engine inserts brief natural pauses. "
            "Stay strictly factual based on the provided stories."
        )
    if about:
        system += (
            "\nListener context (use only to choose tone/emphasis, never invent facts from it):\n"
            + about
        )

    kind = seg["kind"]
    if kind == "intro":
        topics = ", ".join(seg.get("blocks") or [])
        if brief:
            user_msg = (
                (
                    f"Schreibe auf DEUTSCH einen extrem knappen Einstieg (~{target} Wörter, max. 2–3 Sätze) "
                    f"für das Executive Briefing '{show_title}' ({date_str}). "
                    f"KEIN Geplänkel, KEIN Smalltalk: nenne Datum und die Hauptthemen ({topics}) und starte sofort.\n\n"
                    f"Top-Meldungen:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
                ) if de else (
                    f"Write an ultra-crisp opening (~{target} words, 2-3 sentences max) for the executive briefing "
                    f"'{show_title}' ({date_str}). ZERO small talk or banter: state the date and main topics "
                    f"({topics}) and go straight into the news.\n\n"
                    f"Top stories:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
                )
            )
        else:
            user_msg = (
                (
                    f"Schreibe auf DEUTSCH die Begrüßung (~{target} Wörter) für die heutige {duration_minutes}-Minuten-Sendung "
                    f"'{show_title}' ({date_str}). Begrüße die Hörer passend zum gewählten Stil ({style_key}), "
                    f"kündige die Hauptthemen an ({topics}) und leite flüssig in das erste Kapitel über.\n\n"
                    f"Top-Meldungen:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
                ) if de else (
                    f"Write the opening segment (~{target} words) for today's {duration_minutes}-minute show "
                    f"'{show_title}' ({date_str}). Welcome the listener in the requested {style_key} style, "
                    f"briefly tease the main topics ({topics}), and smoothly lead into the first chapter.\n\n"
                    f"Top stories:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
                )
            )
    elif kind == "block":
        angle = f" Blickwinkel: {seg['angle']}." if (de and seg.get("angle")) else (f" Editorial angle: {seg['angle']}." if seg.get("angle") else "")
        prev = (
            (f"\nBereits behandelt (nichts wiederholen): {prior_summary}" if de
             else f"\nAlready covered earlier in the show (don't repeat verbatim): {prior_summary}")
            if prior_summary else ""
        )
        user_msg = (
            (
                f"Schreibe auf DEUTSCH das Themenkapitel '{seg['title']}' (~{target} Wörter) streng im Stil '{style_key}'.{angle} "
                "WICHTIG: Starte direkt ins Thema — KEINE Begrüßung ('Hallo', 'Guten Tag', 'Willkommen' o. Ä.), "
                "wir sind mitten in der Sendung! Übersetze alle englischen Quellen komplett ins Deutsche.{prev}\n\n"
                f"Meldungen für dieses Kapitel:\n{_story_brief_block(seg['stories'], include_excerpts=include_excerpts)}"
            ).format(prev=prev) if de else (
                f"Write the thematic chapter '{seg['title']}' (~{target} words) strictly in the '{style_key}' style.{angle} "
                "IMPORTANT: Dive straight into the topic — NO greeting ('Hello', 'Welcome back', 'Good morning'), "
                f"we are mid-episode!{prev}\n\n"
                f"Stories for this chapter:\n{_story_brief_block(seg['stories'], include_excerpts=include_excerpts)}"
            )
        )
    elif kind == "recap":
        nxt = ", ".join(seg.get("next_blocks") or [])
        user_msg = (
            (
                f"Schreibe auf DEUTSCH einen kurzen Zwischenrückblick (~{target} Wörter) im Stil '{style_key}' über das bisher "
                f"Besprochene ({prior_summary}) mit Ausblick auf das nächste Kapitel ({nxt}). "
                "KEINE erneute Begrüßung ('Hallo', 'Willkommen zurück').\n\n"
                f"Bisherige Hauptmeldungen:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
            ) if de else (
                f"Write a brief mid-show recap (~{target} words) in the '{style_key}' style summarizing the key points covered so far "
                f"({prior_summary}) and teasing what comes next ({nxt}). NO greeting or 'welcome back'.\n\n"
                f"Key stories covered:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
            )
        )
    elif kind == "roundup":
        user_msg = (
            (
                f"Schreibe auf DEUTSCH den schnellen Nachrichtenüberblick 'Kurz gemeldet' (~{target} Wörter) im Stil '{style_key}'. "
                "KEINE Begrüßung ('Hallo', 'Willkommen'). Übersetze ALLE englischen Meldungen vollständig ins Deutsche — "
                "kein einziger englischer Satz! Decke jede Meldung in 1–2 prägnanten gesprochenen Sätzen ab.\n\n"
                f"Meldungen:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
            ) if de else (
                f"Write a brisk 'Quick Roundup' segment (~{target} words) in the '{style_key}' style covering the remaining stories in "
                "1-2 crisp spoken sentences each. NO greeting.\n\n"
                f"Stories:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
            )
        )
    else:  # outro
        if brief:
            user_msg = (
                (
                    f"Schreibe auf DEUTSCH eine ultra-knappe Abmoderation (~{target} Wörter, max. 2 Sätze) "
                    f"für das Executive Briefing '{show_title}'. Kein Geplänkel — kurz abschließen.\n\n"
                    f"Top-Meldungen:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
                ) if de else (
                    f"Write an ultra-brief closing (~{target} words, 2 sentences max) for the executive briefing "
                    f"'{show_title}'. Zero small talk — wrap cleanly.\n\n"
                    f"Top stories:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
                )
            )
        else:
            user_msg = (
                (
                    f"Schreibe auf DEUTSCH die Abmoderation (~{target} Wörter) im Stil '{style_key}' für '{show_title}'. "
                    "KEINE Begrüßung am Anfang. Fasse die 2–3 wichtigsten Punkte des Tages kurz zusammen und verabschiede dich passend zum Stil.\n\n"
                    f"Top-Meldungen:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
                ) if de else (
                    f"Write the closing segment (~{target} words) in the '{style_key}' style for '{show_title}'. "
                    "NO greeting at the start. Briefly recap the 2-3 biggest takeaways and close in style.\n\n"
                    f"Top stories:\n{_story_brief_block(seg['stories'], include_excerpts=False)}"
                )
            )

    # Quick roundups are 1-2 crisp sentences per item and routinely exhaust thinking budgets;
    # run them directly with think=False to save 2-3 minutes per episode.
    temp = 0.2 if brief else (0.5 if style_key == "entertaining" else 0.35)
    if kind == "roundup":
        out = clean_for_tts(ollama.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            temperature=min(temp, 0.3),
            max_tokens=max(1200, min(4096, int(target * 2.5) + 256)),
            model=config.PODCAST_MODEL,
            role="podcast",
            think=False,
        ))
    else:
        # Leave generous headroom for Qwen3 thinking tokens plus the spoken segment itself.
        max_toks = min(16384, max(config.PODCAST_MAX_TOKENS, int(target * 3.0) + 3072))
        out = _chat_with_thinking(
            [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            temperature=temp,
            max_tokens=max_toks,
            label=f"segment '{seg['title']}' ({kind})",
        )
    if de:
        out = _ensure_german(out, fmt=fmt, label=f"segment '{seg['title']}'")
    if kind != "intro":
        out = _strip_mid_show_greetings(out, lang=lang, fmt=fmt)
    return out


def _fact_check_segment(
    draft: str,
    seg: dict[str, Any],
    *,
    lang: str,
    include_excerpts: bool,
    fmt: str = "solo",
) -> str:
    """Verify a drafted spoken segment against its source stories and fix unsupported claims."""
    lang_name = _language_name(lang)
    de = _is_german(lang)
    kind = seg.get("kind", "block")
    if de:
        fmt_note = (
            " Behalte die exakten Sprecher-Präfixe 'HOST1:' und 'HOST2:' vor jeder Sprechzeile bei."
            if fmt == "dialogue" else ""
        )
        no_greet = " Entferne jede Begrüßung ('Hallo', 'Willkommen', 'Guten Tag') — wir sind mitten in der Sendung." if kind != "intro" else ""
        system = (
            "Du bist ein präziser Nachrichten-Redakteur und prüfst ein DEUTSCHES Podcast-Skript "
            "gegen die gelieferten Quellen. Prüfe Namen, Zahlen, Daten, Orte, Zitate und Kausalbehauptungen. "
            "Entferne oder entschärfe unbelegte Aussagen, korrigiere sachliche Fehler und entferne eventuelles "
            "Markdown, URLs oder Regieanweisungen. Übersetze eventuell verbliebene englische Sätze vollständig ins Deutsche. "
            f"Behalte den gewählten Stil, Absatzumbrüche und die ungefähre Länge bei.{no_greet}{fmt_note} "
            "Gib AUSSCHLIESSLICH das korrigierte deutsche Sprechskript aus — keine Kommentare, keine Notizen."
        )
        user_msg = (
            f"Kapitel: {seg['title']}\n\n"
            f"Quellmeldungen:\n{_story_brief_block(seg.get('stories') or [], include_excerpts=include_excerpts)}\n\n"
            f"Entwurf:\n{draft}"
        )
    else:
        fmt_note = (
            " Preserve the exact 'HOST1:' and 'HOST2:' speaker prefixes on each turn."
            if fmt == "dialogue" else ""
        )
        no_greet = " Remove any mid-show greeting ('Hello', 'Welcome back') — we are mid-episode." if kind != "intro" else ""
        system = (
            f"You are a meticulous news editor fact-checking a spoken {lang_name} podcast script against "
            "its source stories. Check every name, number, date, location, quote, and causal claim. "
            "Remove or soften anything not supported by the provided sources, fix any factual errors, "
            "and remove any remaining markdown, URLs, or stage directions. "
            f"Preserve the chosen style, paragraph breaks, and approximate length.{no_greet}{fmt_note} "
            "Output ONLY the final corrected spoken script — no commentary, no notes, no preamble."
        )
        user_msg = (
            f"Segment: {seg['title']}\n\n"
            f"Source stories:\n{_story_brief_block(seg.get('stories') or [], include_excerpts=include_excerpts)}\n\n"
            f"Draft spoken script:\n{draft}"
        )
    try:
        if kind == "roundup":
            # Roundups are short bullet summaries that repeatedly exhaust thinking budgets.
            checked = clean_for_tts(ollama.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
                temperature=0.1,
                max_tokens=max(1200, min(4096, int(len(draft.split()) * 2.4) + 256)),
                model=config.PODCAST_MODEL,
                role="podcast",
                think=False,
            ))
        else:
            max_toks = min(16384, max(config.PODCAST_MAX_TOKENS, int(len(draft.split()) * 3.0) + 3072))
            checked = _chat_with_thinking(
                [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
                temperature=0.1,
                max_tokens=max_toks,
                label=f"fact-check '{seg['title']}'",
            )
        if checked:
            if de:
                checked = _ensure_german(checked, fmt=fmt, label=f"fact-check '{seg['title']}'")
            if kind != "intro":
                checked = _strip_mid_show_greetings(checked, lang=lang, fmt=fmt)
        return checked or draft
    except Exception as e:  # noqa: BLE001
        log.warning("podcast fact-check for '%s' failed (%s); keeping draft", seg["title"], e)
        return draft


# --- DB helpers & lifecycle --------------------------------------------------
def _row_to_dict(row, *, include_script: bool = False) -> dict[str, Any]:
    keys = set(row.keys())
    out = {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "status": row["status"],
        "progress": row["progress"] or "",
        "error": row["error"],
        "duration_target": row["duration_target"],
        "duration_sec": row["duration_sec"],
        "language": row["language"],
        "view": row["view"],
        "style": row["style"] if "style" in keys else "informative",
        "format": row["format"] if "format" in keys else "solo",
        "story_count": row["story_count"],
        "voice": row["voice"],
        "has_audio": bool(row["audio_path"]),
        "audio_mime": row["audio_mime"],
        "file_size": row["file_size"],
        "chapters": db.json_list(row["chapters"]),
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }
    if include_script:
        out["script"] = row["script"] or ""
    return out


# --- Private RSS feed (token-authenticated for podcast apps) -----------------
def get_or_create_feed_token(user_id: int) -> str:
    with db.get_conn() as conn:
        row = conn.execute("SELECT token FROM PodcastFeed WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return str(row["token"])
        token = secrets.token_urlsafe(24)
        conn.execute("INSERT INTO PodcastFeed (user_id, token) VALUES (?, ?)", (user_id, token))
        return token


def rotate_feed_token(user_id: int) -> str:
    token = secrets.token_urlsafe(24)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO PodcastFeed (user_id, token) VALUES (?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET token = excluded.token, created_at = datetime('now')",
            (user_id, token),
        )
    return token


def user_for_feed_token(token: str) -> int | None:
    if not token:
        return None
    with db.get_conn() as conn:
        row = conn.execute("SELECT user_id FROM PodcastFeed WHERE token = ?", (token,)).fetchone()
    return int(row["user_id"]) if row else None


def rss_xml(user_id: int, token: str, base_url: str) -> str:
    """Build a valid RSS 2.0 + iTunes podcast feed for the user's ready audio episodes."""
    base = base_url.rstrip("/")
    with db.get_conn() as conn:
        user_row = conn.execute("SELECT name FROM AppUser WHERE id = ?", (user_id,)).fetchone()
        rows = conn.execute(
            "SELECT * FROM PodcastEpisode WHERE user_id = ? AND status = 'ready' "
            "AND audio_path IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT 50",
            (user_id,),
        ).fetchall()

    uname = user_row["name"] if user_row else "News"
    feed_url = f"{base}/api/podcasts/feed/{token}.xml"
    items: list[str] = []
    for r in rows:
        ep = _row_to_dict(r)
        audio_url = f"{base}/api/podcasts/feed/{token}/audio/{ep['id']}"
        ts = db.parse_ts(ep["completed_at"] or ep["created_at"]) or dt.datetime.now(dt.UTC)
        pub = email.utils.format_datetime(ts.replace(tzinfo=dt.UTC))
        dur = int(round(ep["duration_sec"] or 0))
        chapters_txt = " · ".join(c.get("title", "") for c in ep["chapters"] if c.get("title"))
        desc = f"{ep['story_count']} stories ({ep['style']}, {ep['format']})"
        if chapters_txt:
            desc += f" — {chapters_txt}"
        items.append(
            "    <item>\n"
            f"      <title>{xml_esc(ep['title'])}</title>\n"
            f"      <guid isPermaLink=\"false\">newscompact-podcast-{ep['id']}</guid>\n"
            f"      <pubDate>{xml_esc(pub)}</pubDate>\n"
            f"      <description>{xml_esc(desc)}</description>\n"
            f"      <enclosure url=\"{xml_esc(audio_url)}\" length=\"{int(ep['file_size'] or 0)}\" "
            f"type=\"{xml_esc(ep['audio_mime'] or 'audio/mpeg')}\" />\n"
            f"      <itunes:duration>{dur}</itunes:duration>\n"
            "    </item>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
        "  <channel>\n"
        f"    <title>{xml_esc(f'NewsCompact Podcast ({uname})')}</title>\n"
        f"    <link>{xml_esc(base + '/podcasts')}</link>\n"
        "    <description>Personal AI-generated news podcasts from NewsCompact</description>\n"
        f"    <itunes:author>{xml_esc(uname)}</itunes:author>\n"
        f"    <itunes:image href=\"{xml_esc(base + '/static/logo.svg')}\" />\n"
        f"    <atom:link xmlns:atom=\"http://www.w3.org/2005/Atom\" href=\"{xml_esc(feed_url)}\" rel=\"self\" type=\"application/rss+xml\" />\n"
        + ("\n".join(items) + "\n" if items else "")
        + "  </channel>\n"
        "</rss>\n"
    )


def is_generating() -> bool:
    """True while any podcast episode is actively generating (used to avoid LLM contention)."""
    with _stops_lock:
        return bool(_stops)


def stop_generation(episode_id: int, user_id: int) -> bool:
    """Request cancellation of a queued or running episode owned by ``user_id``."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM PodcastEpisode WHERE id = ? AND user_id = ?",
            (episode_id, user_id),
        ).fetchone()
        if not row or row["status"] not in _ACTIVE_STATUSES:
            return False
    with _stops_lock:
        ev = _stops.get(episode_id)
        if ev is not None:
            ev.set()
        else:
            # Episode was queued or orphaned without an active event; cancel directly.
            _update_episode(episode_id, status="cancelled", progress="", error=None)
            return True
    _update_episode(episode_id, progress="Stopping…")
    return True


def list_episodes(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM PodcastEpisode WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _public_row_to_dict(row, *, include_script: bool = False) -> dict[str, Any]:
    """Serialize a public (view='default') episode with zero user-identifying fields."""
    d = _row_to_dict(row, include_script=include_script)
    d.pop("user_id", None)
    return d


def list_public_episodes(limit: int = 50) -> list[dict[str, Any]]:
    """Ready admin-published episodes generated for 'everyone' (view='default') — no personal info."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT p.* FROM PodcastEpisode p JOIN AppUser u ON u.id = p.user_id "
            "WHERE p.view = 'default' AND p.status = 'ready' AND u.is_admin = 1 "
            "ORDER BY p.created_at DESC, p.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_public_row_to_dict(r) for r in rows]


def get_public_episode(episode_id: int, *, include_script: bool = True) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT p.* FROM PodcastEpisode p JOIN AppUser u ON u.id = p.user_id "
            "WHERE p.id = ? AND p.view = 'default' AND p.status = 'ready' AND u.is_admin = 1",
            (episode_id,),
        ).fetchone()
    return _public_row_to_dict(row, include_script=include_script) if row else None


def public_audio_file_for(episode_id: int) -> tuple[Path, str] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT p.audio_path, p.audio_mime FROM PodcastEpisode p JOIN AppUser u ON u.id = p.user_id "
            "WHERE p.id = ? AND p.view = 'default' AND p.status = 'ready' AND u.is_admin = 1",
            (episode_id,),
        ).fetchone()
    if not row or not row["audio_path"]:
        return None
    path = config.PODCAST_DIR / Path(row["audio_path"]).name
    if not path.is_file():
        return None
    return path, row["audio_mime"] or "audio/wav"


def public_rss_xml(base_url: str) -> str:
    """Public RSS 2.0 + iTunes feed containing ONLY ready admin 'For everyone' (view='default') episodes."""
    base = base_url.rstrip("/")
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT p.* FROM PodcastEpisode p JOIN AppUser u ON u.id = p.user_id "
            "WHERE p.view = 'default' AND p.status = 'ready' AND u.is_admin = 1 "
            "AND p.audio_path IS NOT NULL ORDER BY p.created_at DESC, p.id DESC LIMIT 50"
        ).fetchall()

    feed_url = f"{base}/api/podcasts/public/feed.xml"
    items: list[str] = []
    for r in rows:
        ep = _public_row_to_dict(r)
        audio_url = f"{base}/api/podcasts/public/{ep['id']}/audio"
        ts = db.parse_ts(ep["completed_at"] or ep["created_at"]) or dt.datetime.now(dt.UTC)
        pub = email.utils.format_datetime(ts.replace(tzinfo=dt.UTC))
        dur = int(round(ep["duration_sec"] or 0))
        chapters_txt = " · ".join(c.get("title", "") for c in ep["chapters"] if c.get("title"))
        desc = f"{ep['story_count']} stories ({ep['style']}, {ep['format']})"
        if chapters_txt:
            desc += f" — {chapters_txt}"
        items.append(
            "    <item>\n"
            f"      <title>{xml_esc(ep['title'])}</title>\n"
            f"      <guid isPermaLink=\"false\">newscompact-public-podcast-{ep['id']}</guid>\n"
            f"      <pubDate>{xml_esc(pub)}</pubDate>\n"
            f"      <description>{xml_esc(desc)}</description>\n"
            f"      <enclosure url=\"{xml_esc(audio_url)}\" length=\"{int(ep['file_size'] or 0)}\" "
            f"type=\"{xml_esc(ep['audio_mime'] or 'audio/mpeg')}\" />\n"
            f"      <itunes:duration>{dur}</itunes:duration>\n"
            "    </item>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
        "  <channel>\n"
        "    <title>NewsCompact Public Podcast</title>\n"
        f"    <link>{xml_esc(base + '/podcasts')}</link>\n"
        "    <description>Public AI-generated news podcasts from NewsCompact</description>\n"
        "    <itunes:author>NewsCompact</itunes:author>\n"
        f"    <itunes:image href=\"{xml_esc(base + '/static/logo.svg')}\" />\n"
        f"    <atom:link xmlns:atom=\"http://www.w3.org/2005/Atom\" href=\"{xml_esc(feed_url)}\" rel=\"self\" type=\"application/rss+xml\" />\n"
        + ("\n".join(items) + "\n" if items else "")
        + "  </channel>\n"
        "</rss>\n"
    )


def get_episode(episode_id: int, user_id: int, *, include_script: bool = True) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM PodcastEpisode WHERE id = ? AND user_id = ?",
            (episode_id, user_id),
        ).fetchone()
    return _row_to_dict(row, include_script=include_script) if row else None


def audio_file_for(episode_id: int, user_id: int) -> tuple[Path, str] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT audio_path, audio_mime FROM PodcastEpisode WHERE id = ? AND user_id = ?",
            (episode_id, user_id),
        ).fetchone()
    if not row or not row["audio_path"]:
        return None
    # Prevent path traversal; audio_path is always a filename inside PODCAST_DIR.
    path = config.PODCAST_DIR / Path(row["audio_path"]).name
    if not path.is_file():
        return None
    return path, row["audio_mime"] or "audio/wav"


def delete_episode(episode_id: int, user_id: int) -> bool:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT audio_path FROM PodcastEpisode WHERE id = ? AND user_id = ?",
            (episode_id, user_id),
        ).fetchone()
        if not row:
            return False
        # Cancel any queued or running background job before removing the row.
        with _stops_lock:
            ev = _stops.get(episode_id)
            if ev is not None:
                ev.set()
        conn.execute("DELETE FROM PodcastEpisode WHERE id = ? AND user_id = ?", (episode_id, user_id))
    if row["audio_path"]:
        (config.PODCAST_DIR / Path(row["audio_path"]).name).unlink(missing_ok=True)
    return True


def _prune_old(user_id: int) -> None:
    cap = config.PODCAST_MAX_EPISODES
    if cap <= 0:
        return
    with db.get_conn() as conn:
        old = conn.execute(
            "SELECT id, audio_path FROM PodcastEpisode WHERE user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?",
            (user_id, cap),
        ).fetchall()
        if not old:
            return
        ids = [r["id"] for r in old]
        conn.execute(f"DELETE FROM PodcastEpisode WHERE id IN ({','.join('?' * len(ids))})", ids)
    for r in old:
        if r["audio_path"]:
            (config.PODCAST_DIR / Path(r["audio_path"]).name).unlink(missing_ok=True)


def cleanup_stale() -> None:
    """Mark episodes interrupted by a server restart as failed and unlink orphan audio files."""
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE PodcastEpisode SET status = 'failed', error = 'Server restarted during generation', "
            "progress = '' WHERE status IN ('queued', 'scripting', 'synthesizing')"
        )
        known = {
            Path(r[0]).name
            for r in conn.execute("SELECT audio_path FROM PodcastEpisode WHERE audio_path IS NOT NULL").fetchall()
        }
    if config.PODCAST_DIR.is_dir():
        removed = 0
        for f in config.PODCAST_DIR.iterdir():
            if f.is_file() and f.suffix.lower() in (".mp3", ".wav", ".ogg") and f.name not in known:
                f.unlink(missing_ok=True)
                removed += 1
        if removed:
            log.info("podcast cleanup: removed %d orphaned audio file(s)", removed)


def _episode_exists(episode_id: int) -> bool:
    with db.get_conn() as conn:
        return conn.execute("SELECT 1 FROM PodcastEpisode WHERE id = ?", (episode_id,)).fetchone() is not None


def _update_episode(episode_id: int, **fields: Any) -> int:
    if not fields:
        return 0
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.get_conn() as conn:
        return conn.execute(f"UPDATE PodcastEpisode SET {cols} WHERE id = ?", (*fields.values(), episode_id)).rowcount


def _notify_ready(title: str, duration_sec: float | None, story_count: int, script_only: bool) -> None:
    if not config.HEADS_UP_NTFY_URL:
        return
    from . import reports

    dur = _fmt_duration(duration_sec) if duration_sec else ("script only" if script_only else "ready")
    url = f"{config.PUBLIC_BASE.rstrip('/')}/podcasts"
    body = f"{title}\n{dur} · {story_count} stories\n{url}"
    reports.push_ntfy(body, "Podcast ready", click_url=url)


# --- Generation pipeline -----------------------------------------------------
def run_generation(
    episode_id: int,
    user_id: int,
    *,
    duration_minutes: int,
    view: str,
    max_stories: int,
    notify: bool,
    script_only: bool,
    fact_check: bool = True,
    language: str = "auto",
    style: str = "informative",
    fmt: str = "solo",
    stop_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Execute podcast scripting, fact-checking, and TTS synthesis for an existing episode row."""
    stop = stop_event or threading.Event()
    with _stops_lock:
        _stops[episode_id] = stop

    def _check_stop() -> None:
        if stop.is_set() or not _episode_exists(episode_id):
            raise InterruptedError("cancelled")

    try:
        with _gen_lock:
            _check_stop()
            _update_episode(episode_id, status="scripting", progress="Selecting top stories…")
            include_excerpts = duration_minutes >= 20
            stories, about, lang = _fetch_stories(
                user_id, view, max_stories, include_excerpts, language_override=language
            )
            if not stories:
                raise RuntimeError("No active stories available for this feed.")

            _check_stop()
            _update_episode(episode_id, story_count=len(stories), language=lang,
                            progress="Planning show structure…")
            show_title, segments = _build_segments(stories, duration_minutes, lang, style=style)
            _update_episode(episode_id, title=show_title)

            scripted: list[dict[str, Any]] = []
            prior_topics: list[str] = []
            for idx, seg in enumerate(segments, 1):
                _check_stop()
                _update_episode(episode_id, progress=f"Writing segment {idx}/{len(segments)}: {seg['title']}…")
                text = _write_segment(
                    seg,
                    show_title=show_title,
                    duration_minutes=duration_minutes,
                    lang=lang,
                    about=about,
                    prior_summary="; ".join(prior_topics[-4:]),
                    include_excerpts=include_excerpts,
                    style=style,
                    fmt=fmt,
                )
                if not text:
                    continue
                seg_stories = seg.get("stories") or []
                scripted.append({
                    "title": seg["title"],
                    "story_ids": [s["id"] for s in seg_stories],
                    "stories": [{"id": s["id"], "headline": s["headline"]} for s in seg_stories],
                    "text": text,
                    "seg": seg,
                })
                if seg["kind"] == "block":
                    prior_topics.append(seg["title"])

            if not scripted:
                raise RuntimeError("LLM returned an empty podcast script.")

            if fact_check:
                for idx, item in enumerate(scripted, 1):
                    _check_stop()
                    _update_episode(
                        episode_id,
                        progress=f"Fact-checking {idx}/{len(scripted)}: {item['title']}…",
                    )
                    item["text"] = _fact_check_segment(
                        item["text"],
                        item["seg"],
                        lang=lang,
                        include_excerpts=include_excerpts and item["seg"]["kind"] == "block",
                        fmt=fmt,
                    )

            _check_stop()
            transcript_parts = [
                f"=== {c['title']} ===\n\n"
                + (format_dialogue_transcript(c["text"], lang) if fmt == "dialogue" else c["text"])
                for c in scripted
            ]
            full_script = "\n\n".join(transcript_parts)

            if script_only:
                chapters = [
                    {
                        "title": c["title"],
                        "start_sec": 0.0,
                        "end_sec": 0.0,
                        "story_ids": c["story_ids"],
                        "stories": c["stories"],
                    }
                    for c in scripted
                ]
                _update_episode(
                    episode_id,
                    status="ready",
                    progress="",
                    script=full_script,
                    chapters=json.dumps(chapters),
                    completed_at=db.utcnow(),
                )
                _prune_old(user_id)
                if notify:
                    _notify_ready(show_title, None, len(stories), script_only=True)
                return get_episode(episode_id, user_id)

            _update_episode(episode_id, status="synthesizing", script=full_script,
                            progress=f"Synthesizing audio (0/{len(scripted)})…")

            chapter_inputs: list[dict[str, Any]] = []
            for ch in scripted:
                _check_stop()
                chapter_inputs.append({
                    "title": ch["title"],
                    "story_ids": ch["story_ids"],
                    "stories": ch["stories"],
                    "chunks": (
                        dialogue_chunks(ch["text"], lang)
                        if fmt == "dialogue"
                        else split_paragraphs(ch["text"])
                    ),
                })

            tts_chapters, voice_label = tts.synthesize_chapters(
                chapter_inputs,
                lang=lang,
                on_progress=lambda i, n, title: _update_episode(
                    episode_id, progress=f"Synthesizing audio {i}/{n}: {title}…"
                ),
                stop_event=stop,
            )

            _check_stop()
            _update_episode(episode_id, progress="Encoding audio…")
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
            wav_path = config.PODCAST_DIR / f"podcast-{user_id}-{episode_id}-{stamp}.wav"
            duration_sec, chapter_marks = tts.stitch_wav_segments(tts_chapters, wav_path)
            final_path, mime = tts.encode_audio(wav_path)
            size = final_path.stat().st_size if final_path.is_file() else 0

            if stop.is_set() or not _update_episode(
                episode_id,
                status="ready",
                progress="",
                duration_sec=duration_sec,
                voice=voice_label,
                audio_path=final_path.name,
                audio_mime=mime,
                file_size=size,
                chapters=json.dumps(chapter_marks),
                completed_at=db.utcnow(),
            ):
                final_path.unlink(missing_ok=True)
                raise InterruptedError("cancelled")
            _prune_old(user_id)
            if notify:
                _notify_ready(show_title, duration_sec, len(stories), script_only=False)
            return get_episode(episode_id, user_id)
    except InterruptedError:
        log.info("podcast episode %s cancelled", episode_id)
        _update_episode(episode_id, status="cancelled", progress="", error=None)
        return get_episode(episode_id, user_id)
    except Exception as e:  # noqa: BLE001
        log.exception("podcast episode %s failed", episode_id)
        _update_episode(episode_id, status="failed", error=str(e)[:500], progress="")
        return get_episode(episode_id, user_id)
    finally:
        with _stops_lock:
            _stops.pop(episode_id, None)


def start_generation(
    user_id: int,
    *,
    duration_minutes: int = 15,
    view: str = "personal",
    max_stories: int | None = None,
    notify: bool = True,
    script_only: bool = False,
    fact_check: bool | None = None,
    language: str = "auto",
    style: str = "informative",
    fmt: str = "solo",
    background: bool = True,
) -> dict[str, Any]:
    """Create a PodcastEpisode row and start generation (background thread by default)."""
    duration_minutes = max(1, min(180, int(duration_minutes)))
    view = "default" if view == "default" else "personal"
    style = style if style in STYLES else "informative"
    fmt = fmt if fmt in FORMATS else "solo"
    lang_req = (language or "auto").strip().lower()
    stories_n = max(3, min(MAX_STORIES_CAP, int(max_stories or recommended_story_count(duration_minutes))))
    do_fact_check = config.PODCAST_FACT_CHECK if fact_check is None else bool(fact_check)
    if not script_only and not tts.status()["configured"]:
        raise RuntimeError(f"TTS is not configured: {tts.status()['detail']}")

    with db.get_conn() as conn:
        active = conn.execute(
            "SELECT id FROM PodcastEpisode WHERE user_id = ? AND status IN ('queued', 'scripting', 'synthesizing') LIMIT 1",
            (user_id,),
        ).fetchone()
        if active is not None:
            raise RuntimeError("A podcast is already generating — stop it first or wait for it to finish.")
        if view == "personal":
            profile = scoring.get_user_profile(conn, user_id)
            lang = (
                lang_req if lang_req and lang_req != "auto"
                else (profile["preferences"].get("language") or config.PODCAST_LANGUAGE or config.USER_LANGUAGE or "de").strip().lower()
            )
        else:
            lang = (
                lang_req if lang_req and lang_req != "auto"
                else (config.PODCAST_LANGUAGE or config.USER_LANGUAGE or "de").strip().lower()
            )
        date_str = dt.datetime.now().strftime("%d.%m.%Y" if _is_german(lang) else "%Y-%m-%d")
        initial_title = f"Nachrichten-Podcast · {date_str}" if _is_german(lang) else f"News Podcast · {date_str}"
        cur = conn.execute(
            "INSERT INTO PodcastEpisode "
            "(user_id, title, status, progress, duration_target, language, view, style, format) "
            "VALUES (?, ?, 'queued', 'Queued…', ?, ?, ?, ?, ?)",
            (user_id, initial_title, duration_minutes, lang, view, style, fmt),
        )
        ep_id = int(cur.lastrowid)

    stop_ev = threading.Event()
    with _stops_lock:
        _stops[ep_id] = stop_ev

    kwargs = {
        "duration_minutes": duration_minutes,
        "view": view,
        "max_stories": stories_n,
        "notify": notify,
        "script_only": script_only,
        "fact_check": do_fact_check,
        "language": lang,
        "style": style,
        "fmt": fmt,
        "stop_event": stop_ev,
    }
    if background:
        threading.Thread(
            target=run_generation,
            args=(ep_id, user_id),
            kwargs=kwargs,
            name=f"podcast-{ep_id}",
            daemon=True,
        ).start()
        return get_episode(ep_id, user_id, include_script=False)  # type: ignore[return-value]
    return run_generation(ep_id, user_id, **kwargs)  # type: ignore[return-value]
