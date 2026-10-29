"""Feed categories and source trust of 2026-10-29, applied once by migration v12 (app/db.py).

Trust used to be per user (the owner's ⚙ category sliders, NEWS_SOURCE_TRUST otherwise); it
now applies to everyone and is edited on the Feeds page: FeedCategory holds each category's
trust, Feed.trust the feeds that differ from it. Users can still override both in ⚙
Preferences; the old profile values are dropped because these replace them.

"Zu viele News", "Relevant" and "J" are split into topic categories. Feeds are matched by URL,
so databases without them (tests, fresh installs) only get the category defaults.
Trust scale: 1.0 official source or top newsroom, 0.9 established newsroom, 0.7-0.85 trade
press / advocacy / project blogs, 0.5-0.6 blogs, aggregators, partisan or non-news feeds,
<= 0.35 user-generated.
"""
from __future__ import annotations

import sqlite3

CATEGORY_TRUST = {
    "News DACH": 0.9, "News World": 0.9, "Business & Finance": 0.85, "Institutions": 1.0, "Tech": 0.8,
    "Security & Privacy": 0.9, "Science & Health": 0.85, "Culture & Society": 0.7, "Gaming": 0.7,
    "Munich & Bavaria": 0.8, "Journals": 0.9, "Wikipedia": 0.8, "Hacker News": 0.5, "Reddit": 0.2,
    "Reviews": 0.5,
}

# (xml_url, category, trust); trust None = the category's.
FEEDS = (
    # News DACH
    ("https://rss.dw.com/xml/rss-de-top", "News DACH", 1.0),
    ("https://rss.sueddeutsche.de/rss/Eilmeldungen", "News DACH", None),
    ("https://rss.sueddeutsche.de/rss/Topthemen", "News DACH", None),
    ("https://taz.de/!p4608;rss/", "News DACH", 0.85),
    ("https://www.deutschlandfunk.de/europa-112.rss", "News DACH", None),
    ("https://www.deutschlandfunk.de/gesellschaft-106.rss", "News DACH", None),
    ("https://www.deutschlandfunk.de/politikportal-100.rss", "News DACH", 1.0),
    ("https://www.deutschlandfunkkultur.de/politik-114.rss", "News DACH", 0.8),
    ("https://www.faz.net/rss/aktuell/", "News DACH", None),
    ("https://www.jungewelt.de/feeds/newsticker.rss", "News DACH", 0.5),
    ("https://www.n-tv.de/politik/rss", "News DACH", 0.8),
    ("https://www.spiegel.de/index.rss", "News DACH", None),
    ("https://www.srf.ch/news/bnf/rss/1646", "News DACH", 1.0),
    ("https://www.tagesschau.de/index~rss2.xml", "News DACH", 1.0),
    # News World
    ("https://feeds.bbci.co.uk/news/world/europe/rss.xml", "News World", 1.0),
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "News World", 1.0),
    ("https://feeds.feedburner.com/IntelligenceOnline", "News World", 0.6),
    ("https://feeds.npr.org/1001/rss.xml", "News World", None),
    ("https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "News World", 0.95),
    ("https://www.aljazeera.com/xml/rss/all.xml", "News World", 0.75),
    ("https://www.economist.com/the-world-this-week/rss.xml", "News World", 0.5),
    ("https://www.france24.com/en/europe/rss", "News World", 0.85),
    ("https://www.lemonde.fr/en/rss/une.xml", "News World", None),
    ("https://www.propublica.org/feeds/propublica/main", "News World", 1.0),
    ("https://www.theguardian.com/world/rss", "News World", 0.95),
    # Business & Finance
    ("https://feeds.cms.handelsblatt.com/schlagzeilen", "Business & Finance", 0.9),
    ("https://www.deutschlandfunk.de/wirtschaft-106.rss", "Business & Finance", 0.9),
    ("https://www.ecb.europa.eu/rss/press.html", "Business & Finance", 1.0),
    ("https://www.finanznachrichten.de/rss-aktien-nachrichten", "Business & Finance", 0.4),
    ("https://www.mahnerfolg.de/urteile/index.php/feed/", "Business & Finance", 0.5),
    # Institutions: parliament, ministries, courts, EU, NGOs
    ("https://www.amnesty.de/rss/themen/all", "Institutions", 0.8),
    ("https://www.bmi.bund.de/DE/service/rss-newsfeed/function/rssnewsfeed-pressemitteilungen.xml", "Institutions", 0.9),
    ("https://www.bundesgesundheitsministerium.de/meldungen.xml", "Institutions", 0.9),
    ("https://www.bundestag.de/static/appdata/includes/rss/aktuellethemen.rss", "Institutions", None),
    ("https://www.bundestag.de/static/appdata/includes/rss/digitales.rss", "Institutions", None),
    ("https://www.bundestag.de/static/appdata/includes/rss/gesundheit.rss", "Institutions", None),
    ("https://www.bundestag.de/static/appdata/includes/rss/hib.rss", "Institutions", None),
    ("https://www.bundestag.de/static/appdata/includes/rss/pressemitteilungen.rss", "Institutions", None),
    ("https://www.bundestag.de/static/appdata/includes/rss/wirtschaft.rss", "Institutions", None),
    ("https://www.bundestag.de/static/appdata/includes/rss/wissenschaftlichedienste.rss", "Institutions", 0.8),
    ("https://www.bverwg.de/rss/pressemitteilungen.rss", "Institutions", None),
    ("https://www.europarl.europa.eu/rss/committee/itre/en.xml", "Institutions", 0.8),
    ("https://www.europarl.europa.eu/rss/committee/libe/en.xml", "Institutions", 0.8),
    ("https://www.europarl.europa.eu/rss/doc/texts-adopted/en.xml", "Institutions", None),
    ("https://www.foerderdatenbank.de/FDB/DE/Service/RSS/Functions/foerderprogram_rssnewsfeed.xml", "Institutions", 0.6),
    ("https://www.foerderdatenbank.de/FDB/DE/Service/RSS/Functions/pressemitteilung_rssnewsfeed.xml", "Institutions", 0.7),
    # Tech
    ("http://stadt-bremerhaven.de/feed", "Tech", 0.65),
    ("https://feeds.arstechnica.com/arstechnica/features", "Tech", 0.9),
    ("https://feeds.feedburner.com/netzwelt", "Tech", 0.6),
    ("https://futo.tech/blog/rss.xml", "Tech", 0.7),
    ("https://nextcloud.com/feed", "Tech", 0.7),
    ("https://whatmodel.sh/rss/hardware.xml?ram=32&gpu=card&vram=16&max=0", "Tech", 0.6),
    ("https://www.heise.de/rss/heise-atom.xml", "Tech", 0.95),
    ("https://www.home-assistant.io/atom.xml", "Tech", None),
    ("https://www.spiegel.de/netzwelt/index.rss", "Tech", 0.9),
    ("https://www.truenas.com/docs/index.xml", "Tech", 0.7),
    # Security & Privacy
    ("https://act.eff.org/action.atom", "Security & Privacy", 0.4),
    ("https://krebsonsecurity.com/feed/", "Security & Privacy", 0.95),
    ("https://netzpolitik.org/feed/", "Security & Privacy", 0.85),
    ("https://netzpolitik.org/ticker/feed/", "Security & Privacy", 0.8),
    ("https://privacyinternational.org/rss.xml", "Security & Privacy", 0.8),
    ("https://tarnkappe.info/feed", "Security & Privacy", 0.6),
    ("https://wid.cert-bund.de/content/public/buergercert/rss", "Security & Privacy", 1.0),
    ("https://www.bsi.bund.de/SiteGlobals/Functions/RSSFeed/RSSNewsfeed/RSSNewsfeed_CSW.xml", "Security & Privacy", 1.0),
    ("https://www.ccc.de/de/rss/updates.xml", "Security & Privacy", 0.85),
    ("https://www.cert.europa.eu/publications/security-advisories-rss", "Security & Privacy", 1.0),
    ("https://www.cisa.gov/cybersecurity-advisories/alerts.xml", "Security & Privacy", 1.0),
    ("https://www.eff.org/rss/updates.xml", "Security & Privacy", 0.8),
    ("https://www.kb.cert.org/vuls/atomfeed/", "Security & Privacy", 1.0),
    ("https://www.schneier.com/feed/atom/", "Security & Privacy", 0.8),
    ("https://www.theregister.com/security/headlines.atom", "Security & Privacy", 0.85),
    # Science & Health
    ("https://cen.acs.org/feeds/rss/latestnews.xml", "Science & Health", 0.9),
    ("https://edoc.rki.de/feed/atom_1.0/site", "Science & Health", 0.9),
    ("https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "Science & Health", 1.0),
    ("https://www.aerzteblatt.de/rss/news.asp", "Science & Health", 0.95),
    ("https://www.aerztezeitung.de/News.rss", "Science & Health", 0.7),
    ("https://www.chemistryworld.com/413.rss", "Science & Health", 0.9),
    ("https://www.tagesschau.de/wissen/gesundheit/index~rss2.xml", "Science & Health", 1.0),
    # Culture & Society
    ("https://rss.sueddeutsche.de/rss/Reise", "Culture & Society", 0.5),
    ("https://www.deutschlandfunk.de/kulturportal-100.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/buehne-100.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/film-serie-100.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/geschichte-136.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/leben-108.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/meinung-debatte-100.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/musikportal-100.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/philosophie-104.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/psychologie-100.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/umwelt-104.rss", "Culture & Society", None),
    ("https://www.deutschlandfunkkultur.de/wissenschaft-108.rss", "Culture & Society", None),
    # Gaming
    ("https://feeds.arstechnica.com/arstechnica/gaming", "Gaming", 0.85),
    ("https://www.gamesindustry.biz/feed", "Gaming", 0.8),
    ("https://www.rockpapershotgun.com/feed/", "Gaming", None),
    # Munich & Bavaria
    ("https://nachrichtenfeeds.br.de/rss/nachrichten/seiten/QXAPkQJ", "Munich & Bavaria", 0.95),
    ("https://rss.sueddeutsche.de/rss/Muenchen", "Munich & Bavaria", 0.9),
    ("https://www.mucbook.de/feed", "Munich & Bavaria", 0.6),
    ("https://www.muenchen-sehen.de/feed/", "Munich & Bavaria", 0.5),
    # Journals (arXiv preprints are not peer-reviewed; ScienceDaily republishes press releases)
    *((f"https://rss.arxiv.org/rss/cs.{c}", "Journals", 0.7)
      for c in ("AI", "CL", "CR", "DC", "ET", "IR", "LG", "LO", "MA", "NI", "RO", "SE")),
    ("https://www.nature.com/nmat.rss", "Journals", 1.0),
    ("https://www.pnas.org/action/showFeed?type=etoc&feed=rss&jc=PNAS", "Journals", 1.0),
    ("https://www.science.org/rss/news_current.xml", "Journals", 1.0),
    ("https://www.sciencedaily.com/rss/computers_math.xml", "Journals", 0.85),
    ("https://www.sciencedaily.com/rss/top.xml", "Journals", 0.85),
    # Wikipedia (evergreen, not news)
    ("http://en.wikipedia.org/w/api.php?action=featuredfeed&feed=featured", "Wikipedia", 0.5),
    ("https://en.wikipedia.org/w/api.php?action=featuredfeed&feed=onthisday", "Wikipedia", 0.6),
)

# Subreddits (upvoterss URLs ``...subreddit=<name>&...``) whose trust differs from Reddit's 0.2:
# news-link subs count a bit more, personal/help/social ones (and the disabled ones) less.
REDDIT_TRUST = {
    0.35: ("worldnews", "europe", "de", "Nachrichten", "USA_de", "technology", "technologie", "science",
           "UpliftingNews", "Futurology"),
    0.1: ("AskReddit", "DIY", "LifeProTips", "buildapc", "explainlikeimfive", "lifehacks", "pcmasterrace"),
}


def apply(conn: sqlite3.Connection) -> int:
    """Seed category trust, recategorize / set trust of known feeds, drop profile trust; returns feeds changed."""
    conn.executemany("INSERT OR IGNORE INTO FeedCategory (name, trust) VALUES (?, ?)", CATEGORY_TRUST.items())
    changed = 0
    for url, category, trust in FEEDS:
        changed += conn.execute("UPDATE Feed SET category = ?, trust = ? WHERE xml_url = ?",
                                (category, trust, url)).rowcount
    for trust, subs in REDDIT_TRUST.items():
        for sub in subs:
            changed += conn.execute("UPDATE Feed SET trust = ? WHERE category = 'Reddit' AND instr(xml_url, ?) > 0",
                                    (trust, f"subreddit={sub}&")).rowcount
    conn.execute("UPDATE UserProfile SET source_weights = '{}', feed_weights = '{}'")
    return changed
