"""Anil Singhvi (Zee Business) — the single highest-signal named voice in this market.

Discovery runs through three independent routes (see search.py): zeebiz.com's own search,
Google News RSS, then DuckDuckGo. Whichever answers first wins.

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

from datetime import datetime, timezone
from typing import Optional, Sequence
from urllib.parse import quote_plus, urljoin

from ..config import SOURCE_SINGHVI, SOURCE_URLS
from ..models import IPO, IPOStatus, Segment, SourceCall, Stance
from .base import Source, classify_stance, clean_text, get_soup, name_similarity, snippet
from .search import SearchHit, best_match, news_search, web_search

# --------------------------------------------------------------------------------------
# SELECTORS — patch here when a site changes
# --------------------------------------------------------------------------------------

SELECTORS: dict[str, str] = {
    "zee_result": "div.searchlist li, div.search-list li, ul.search-result li, div.result-item, article",
    "zee_link": "a[href]",
    "zee_title": "h2, h3, a",
    "article_body": "div.article-content p, div.articleBody p, div.content p, article p, p",
}

QUERY_TEMPLATES: dict[str, str] = {
    "zee": "Anil Singhvi {name} IPO",
    "news": '"Anil Singhvi" {name} IPO',
    "web": "site:zeebiz.com Anil Singhvi {name} IPO review subscribe",
}

ANCHOR_TOKEN = "singhvi"  # a result without this is not his call
MAX_ARTICLE_PARAGRAPHS = 6


class SinghviSource(Source):
    """Mainboard-focused. Emits NO_VIEW rather than guessing."""

    name = "singhvi"
    segment_coverage = (Segment.MAINBOARD, Segment.SME)  # SME queried too, usually NO_VIEW

    def fetch(self, ipos: Sequence[IPO]) -> list[SourceCall]:
        now = datetime.now(timezone.utc).isoformat()
        calls: list[SourceCall] = []
        any_route_answered = False

        for ipo in self._targets(ipos):
            hit, body, reachable = self._find(ipo)
            any_route_answered = any_route_answered or reachable
            if hit is None:
                stance, url, rationale = Stance.NO_VIEW, None, "No Zee Business call found for this issue."
            else:
                stance = classify_stance(hit.title)
                if stance is Stance.NO_VIEW and hit.summary:
                    stance = classify_stance(hit.summary)
                if stance is Stance.NO_VIEW and body:
                    stance = classify_stance(body)
                url = hit.url
                rationale = self._rationale(stance, hit, body)

            calls.append(
                SourceCall(
                    source_name=SOURCE_SINGHVI,
                    ipo_slug=ipo.slug,
                    ipo_name=ipo.name,
                    stance=stance,
                    rationale=rationale,
                    url=url,
                    captured_at=now,
                    segment=ipo.segment,
                )
            )

        # Only a total discovery outage is a failure. NO_VIEW everywhere is normal.
        if calls and not any_route_answered:
            self.fail("singhvi: every discovery route (zeebiz, Google News, DuckDuckGo) returned nothing")
        return calls

    # -- internals ---------------------------------------------------------------------

    def _targets(self, ipos: Sequence[IPO]) -> list[IPO]:
        """Only issues a view can still act on."""
        return [i for i in self.relevant(ipos) if i.status in (IPOStatus.OPEN, IPOStatus.UPCOMING)]

    def _find(self, ipo: IPO) -> tuple[Optional[SearchHit], str, bool]:
        """-> (best hit, article body if fetchable, whether any route responded at all)."""
        reachable = False

        zee_hit = self._search_zeebiz(ipo)
        if zee_hit is not None:
            reachable = True
            return zee_hit, self._article_body(zee_hit.url), reachable

        news_hits = news_search(QUERY_TEMPLATES["news"].format(name=ipo.name))
        if news_hits:
            reachable = True
            hit = best_match(news_hits, ipo.name, require=ANCHOR_TOKEN)
            if hit is not None:
                # Google News links are wrappers; the body is not fetchable server-side.
                return hit, "", reachable

        web_hits = web_search(QUERY_TEMPLATES["web"].format(name=ipo.name))
        if web_hits:
            reachable = True
            hit = best_match(web_hits, ipo.name, require=ANCHOR_TOKEN)
            if hit is not None:
                return hit, self._article_body(hit.url), reachable

        return None, "", reachable

    def _search_zeebiz(self, ipo: IPO) -> Optional[SearchHit]:
        query = quote_plus(QUERY_TEMPLATES["zee"].format(name=ipo.name))
        soup, used_url = self.first_working_soup(SOURCE_URLS["singhvi_search"], query=query)
        if soup is None:
            return None
        best: Optional[tuple[float, SearchHit]] = None
        for node in soup.select(SELECTORS["zee_result"]):
            link = node.select_one(SELECTORS["zee_link"])
            if link is None or not link.get("href"):
                continue
            headline = clean_text(link) or clean_text(node.select_one(SELECTORS["zee_title"]))
            blob = clean_text(node).lower()
            if ANCHOR_TOKEN not in blob:
                continue
            score = name_similarity(ipo.name, headline)
            if score < 0.55:
                if ipo.name.split()[0].lower() not in blob:
                    continue
                score = 0.55
            href = str(link["href"])
            url = href if href.startswith("http") else urljoin(used_url or "", href)
            hit = SearchHit(title=headline, url=url, publisher="zeebiz.com", summary=blob[:300])
            if best is None or score > best[0]:
                best = (score, hit)
        return best[1] if best else None

    def _article_body(self, url: str) -> str:
        """Opening paragraphs, so a vague headline still yields a stance."""
        article = get_soup(url)
        if article is None:
            return ""
        paragraphs = [clean_text(p) for p in article.select(SELECTORS["article_body"])]
        return " ".join(p for p in paragraphs[:MAX_ARTICLE_PARAGRAPHS] if p)

    @staticmethod
    def _rationale(stance: Stance, hit: SearchHit, body: str) -> str:
        publisher = hit.publisher or "Zee Business"
        headline = snippet(hit.title, 160)
        if stance is Stance.NO_VIEW:
            return snippet(headline or "Coverage found but no explicit call.", 160)
        if headline:
            return f"{publisher}: “{headline}”"
        return snippet(body or hit.summary, 180)
