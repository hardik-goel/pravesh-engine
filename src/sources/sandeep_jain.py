"""Sandeep Jain (Zee Business / business media) — the second named expert this engine tracks.

Mirrors singhvi.py exactly: same machinery (expert.NamedExpertSource), same three discovery
routes (zeebiz.com search → Google News RSS → DuckDuckGo), same stance vocabulary, same
ledger treatment. This file is the constants block for him and nothing else.

He is a normal row in the evidence table with his own accuracy and n, and he carries the
same veto as Singhvi: an AVOID raises a hard red banner without touching the score. The two
experts SPLIT the expert weight (config.BASE_WEIGHTS) rather than adding to it — a second
voice on the panel must not mean more influence for opinion over the numbers.

STATUS NOTE (as of 2026-08-03): zeebiz.com returns HTTP 403 to this crawler and DuckDuckGo's
HTML endpoint serves a captcha, so in practice Google News RSS carries this source too — the
stance is classified from the headline plus the RSS summary rather than the article body.

NO_VIEW is first-class and non-penalising — he will not cover every SME issue. That absence
is information, not a failure, and is never written to `sources_failed`.

WHEN THIS BREAKS: patch SELECTORS / QUERY_TEMPLATES below, or the search endpoints in
config.SOURCE_URLS.
"""

from __future__ import annotations

from ..config import SOURCE_SANDEEP_JAIN
from .expert import NamedExpertSource

# --------------------------------------------------------------------------------------
# SELECTORS — patch here when a site changes
# --------------------------------------------------------------------------------------

SELECTORS: dict[str, str] = {
    "result": "div.searchlist li, div.search-list li, ul.search-result li, div.result-item, article",
    "link": "a[href]",
    "title": "h2, h3, a",
    "article_body": "div.article-content p, div.articleBody p, div.content p, article p, p",
}

QUERY_TEMPLATES: dict[str, str] = {
    "site": "Sandeep Jain {name} IPO",
    "news": '"Sandeep Jain" {name} IPO',
    "web": "site:zeebiz.com Sandeep Jain {name} IPO review subscribe",
}

# A result without this is not his call. "jain" alone is far too common a surname to anchor
# on, so the full name is required — a near-miss costs a NO_VIEW, which is the safe failure.
ANCHOR_TOKEN = "sandeep jain"
MAX_ARTICLE_PARAGRAPHS = 6


class SandeepJainSource(NamedExpertSource):
    """Covers mainboard and the larger SME issues. Emits NO_VIEW rather than guessing."""

    name = "sandeep_jain"
    expert_name = SOURCE_SANDEEP_JAIN
    anchor_token = ANCHOR_TOKEN
    search_urls_key = "sandeep_jain_search"
    default_publisher = "Zee Business"
    site_publisher = "zeebiz.com"
    discovery_label = "zeebiz"
    selectors = SELECTORS
    query_templates = QUERY_TEMPLATES
    max_article_paragraphs = MAX_ARTICLE_PARAGRAPHS
