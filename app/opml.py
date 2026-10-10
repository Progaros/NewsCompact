"""OPML → Feed rows.

OPML folders (nested <outline> without xmlUrl) become category prefixes;
leaf outlines with type="rss" + xmlUrl become Feed records. Upserted idempotently.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Iterable

from . import config, db


def _walk(outlines, category: str) -> Iterable[tuple[str, str, str, str]]:
    for o in outlines:
        xml_url = o.get("xmlUrl")
        if xml_url:
            yield (category, o.get("title", ""), xml_url, o.get("htmlUrl", ""))
        else:
            children = o.findall("outline")
            if children:
                sub = o.get("title", "")
                newcat = f"{category}/{sub}" if category else sub
                yield from _walk(children, newcat)


def parse_opml(path: Path = config.OPML_PATH):
    """Return a list of (category, title, xml_url, site_url) tuples."""
    root = ET.parse(str(path)).getroot()
    body = root.find("body")
    if body is None:
        raise ValueError("OPML has no <body>")
    return list(_walk(body.findall("outline"), ""))


def import_opml(opml_path: Path = config.OPML_PATH) -> int:
    """Upsert all feeds from the OPML file. Returns number of feeds."""
    items = parse_opml(opml_path)
    with db.get_conn() as conn:
        for category, title, xml_url, site_url in items:
            conn.execute(
                """
                INSERT INTO Feed (title, xml_url, site_url, category)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (xml_url) DO UPDATE SET
                    title = excluded.title,
                    site_url = excluded.site_url,
                    category = excluded.category
                """,
                (title, xml_url, site_url, category),
            )
        conn.commit()
    return len(items)
