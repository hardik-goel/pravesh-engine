"""Anil Singhvi (Zee Business) — one of the two named voices this engine tracks by name.

All the machinery lives in expert.NamedExpertSource; this file is the constants block for
him and nothing else. Discovery runs through three independent routes (see search.py):
zeebiz.com's own search, Google News RSS, then DuckDuckGo. Whichever answers first wins.

STATUS NOTE (verified 2026-07-31): zeebiz.com returns HTTP 403 to this crawler and
DuckDuckGo's HTML endpoint serves a captcha, so in practice Google News RSS carries this
source. That means the stance is usually classified from the headline plus the RSS summary
rather than the article body — which is honest, since the headline is what he leads with.

NO_VIEW is first-class and non-penalising — he simply does not cover most SME issues. That
absence is information, not a failure, and is never written to `sources_failed`.

WHEN THIS BREAKS: patch SELECTORS / QUERY_TEMPLATES below, or the search endpoints in
config.SOURCE_URLS.
"""

from __future__ import annotations

from ..config import SOURCE_SINGHVI
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
    "site": "Anil Singhvi {name} IPO",
    "news": '"Anil Singhvi" {name} IPO',
    "web": "site:zeebiz.com Anil Singhvi {name} IPO review subscribe",
}

ANCHOR_TOKEN = "singhvi"  # a result without this is not his call
MAX_ARTICLE_PARAGRAPHS = 6


class SinghviSource(NamedExpertSource):
    """Mainboard-focused. Emits NO_VIEW rather than guessing."""

    name = "singhvi"
    expert_name = SOURCE_SINGHVI
    anchor_token = ANCHOR_TOKEN
    search_urls_key = "singhvi_search"
    default_publisher = "Zee Business"
    site_publisher = "zeebiz.com"
    discovery_label = "zeebiz"
    selectors = SELECTORS
    query_templates = QUERY_TEMPLATES
    max_article_paragraphs = MAX_ARTICLE_PARAGRAPHS
