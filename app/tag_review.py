"""Hand review of pending TagAlias proposals, applied once by migration v11 (app/db.py).

Pairs are ``(alias, canonical)`` in the wanted direction (English canonical where one exists,
the tag on the user's tag list otherwise). Approvals touch only rows still pending, rejections
also revoke auto-approved rows, so DBs that never proposed a pair are unaffected. Approved
aliases that involve an ambiguous tag (tags.AMBIGUOUS_TAGS, e.g. the auto-approved
"sicherheit" -> "safety") are rejected too.
"""
from __future__ import annotations

import sqlite3

from . import tags

APPROVE_2026_10_28 = (
    ("benchmarking", "benchmarks"), ("preiserhohung", "price increase"), ("wien", "vienna"),
    ("stocks", "stock market"), ("australien", "australia"), ("global warming", "climate change"),
    ("medical study", "health study"), ("stadtentwicklung", "urban development"),
    ("deutsche telekom", "telekom"), ("jugend", "youth"), ("economics", "economy"),
    ("start ups", "start up"), ("arts", "art"), ("transgender rights", "trans rights"),
    ("celebrity news", "celebrity"), ("steuer", "steuern"), ("digital transformation", "digitalization"),
    ("turkei", "turkey"), ("home automation", "smart home"), ("law enforcement", "police"),
    ("koalition", "coalition"), ("tech news", "tech"), ("quantencomputer", "quantum computing"),
    ("cafes", "cafe"), ("seniors", "elderly"), ("diebstahl", "theft"), ("humor", "comedy"),
    ("banken", "banking"), ("philanthropy", "charity"), ("wirtschaftspolitik", "economic policy"),
    ("entertainment news", "entertainment"), ("medical research", "health research"),
    ("nachhaltigkeit", "sustainability"), ("steuerpolitik", "tax policy"),
    ("automotive", "automotive industry"), ("arzte", "doctors"), ("grammatik", "grammar"),
    ("bildungspolitik", "education policy"), ("harvesting", "harvest"), ("produktivitat", "productivity"),
    ("stadtplanung", "urban planning"), ("elektroautos", "electric vehicle"), ("diagnose", "diagnostics"),
    ("klimakrise", "climate change"), ("rassismus", "racism"), ("pharmaceutical industry", "pharma"),
    ("movies", "film"), ("justiz", "judiciary"), ("homelab", "home lab"), ("gesetzgebung", "legislation"),
    ("kulturpolitik", "cultural politics"), ("medizintechnik", "medical technology"),
    ("arbeitsmarkt", "labor market"), ("medienkritik", "media criticism"),
    ("personal experience", "personal anecdote"), ("supercomputing", "high performance computing"),
    ("deutsch", "german"), ("iran war", "iran conflict"), ("emulator", "emulation"),
    ("soziale ungleichheit", "inequality"),
)
# Related or narrower, not the same concept ("everything smart home" is a YouTube channel).
REJECT_2026_10_28 = (
    ("publishing", "publication"), ("cardiovascular health", "cardiovascular disease"),
    ("gesundheitswesen", "health"), ("medicine", "medication"), ("strafverfolgung", "strafprozess"),
    ("journalist", "journalism"), ("wahlkampf", "election"), ("wedding", "marriage"),
    ("budget", "budgeting"), ("rental", "rent"), ("storage", "data storage"),
    ("beziehungen", "relationship"), ("bayern", "bayern munich"), ("telekom", "telecom"),
    ("netzwerk", "networking"), ("smart home", "everything smart home"),
)


def apply(conn: sqlite3.Connection, approve=APPROVE_2026_10_28, reject=REJECT_2026_10_28) -> int:
    """Set the reviewed status on matching rows (either direction); returns rows changed."""
    changed = 0
    for pairs, status, open_status in ((approve, "approved", "('pending')"),
                                       (reject, "rejected", "('pending', 'approved')")):
        for alias, canonical in pairs:
            row = conn.execute(
                f"SELECT alias, canonical FROM TagAlias WHERE status IN {open_status} "
                "AND ((alias = ? AND canonical = ?) OR (alias = ? AND canonical = ?))",
                (alias, canonical, canonical, alias)).fetchone()
            if row is None:
                continue
            conn.execute("UPDATE OR REPLACE TagAlias SET alias = ?, canonical = ?, status = ?, "
                         "reviewed_at = datetime('now') WHERE alias = ? AND canonical = ?",
                         (alias, canonical, status, row[0], row[1]))
            if status == "approved":  # one target per alias, no reversed twin (as tag_merge.review)
                conn.execute("UPDATE TagAlias SET status = 'rejected', reviewed_at = datetime('now') "
                             "WHERE status = 'approved' AND ((alias = ? AND canonical != ?) "
                             "OR (alias = ? AND canonical = ?))", (alias, canonical, canonical, alias))
            changed += 1
    marks = ", ".join("?" * len(tags.AMBIGUOUS_TAGS))
    changed += conn.execute(
        f"UPDATE TagAlias SET status = 'rejected', reviewed_at = datetime('now') WHERE status = 'approved' "
        f"AND (alias IN ({marks}) OR canonical IN ({marks}))", (*tags.AMBIGUOUS_TAGS, *tags.AMBIGUOUS_TAGS)).rowcount
    return changed
