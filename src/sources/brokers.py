"""Named brokerages, lifted out of "should you subscribe?" roundups.

Moneycontrol / Economic Times / Livemint publish a roundup per issue quoting each
brokerage by name. We extract the firm, its stance and a one-line rationale snippet, then
normalise the firm name through config.BROKER_ALIASES so "Anand Rathi Research" and
"Anand Rathi" share one accuracy ledger identity.

Firms not yet in the alias map are still admitted (title-cased) — the ledger grows on its
own, which is the point of a generic engine.

WHEN THIS BREAKS: patch SELECTORS / FIRM_PATTERN / QUERY_TEMPLATE below.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence
from urllib.parse import urlparse

from ..config import BROKER_ALIASES, BROKER_NAME_BLOCKLIST, SOURCE_URLS
from ..models import IPO, IPOStatus, Segment, SourceCall, Stance
from .base import Source, classify_stance, clean_text, get_soup, name_similarity, snippet
from .search import topic_search, unwrap_ddg, web_search

# --------------------------------------------------------------------------------------
# SELECTORS — patch here when a site changes
# --------------------------------------------------------------------------------------

SELECTORS: dict[str, str] = {
    "ddg_result": "div.result, div.web-result",
    "ddg_link": "a.result__a",
    "article_body": (
        "div.content_wrapper p, div.article-content p, div.artText p, "
        "div.Normal, div.storyContent p, section.storyPage p, article p, p"
    ),
    "roundup_link": "a[href]",
}

QUERY_TEMPLATE = "{name} IPO should you subscribe brokerages recommendation review"

#: Domains we trust for roundups. Add one here to widen coverage.
ALLOWED_DOMAINS: tuple[str, ...] = (
    "moneycontrol.com",
    "economictimes.indiatimes.com",
    "livemint.com",
    "business-standard.com",
    "financialexpress.com",
)

#: "SBI Securities", "Anand Rathi Research", "Choice Broking", "Angel One"...
FIRM_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z&.\-']+(?:\s+[A-Z][A-Za-z&.\-']+){0,3}\s+"
    r"(?:Securities|Broking|Broker(?:s|age)?|Capital|Research|Financial\s+Services|"
    r"Investmart|Equities|Wealth|Finance|Direct|One|Bang|Rathi|FinServ|Institutional\s+Equities))\b"
)

MAX_ARTICLES_PER_IPO = 3
MAX_PARAGRAPHS = 60
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def canonical_firm(raw: str) -> Optional[str]:
    """Alias-map normalisation. Returns None for junk."""
    cleaned = clean_text(raw).strip(" .,:;-")
    if not cleaned or len(cleaned) < 3:
        return None
    key = cleaned.lower()
    if key in BROKER_NAME_BLOCKLIST:
        return None
    if key in BROKER_ALIASES:
        return BROKER_ALIASES[key]
    # Progressively shorter prefixes, down to the bare house name, so
    # "Anand Rathi Share and Stock" and "Swastika Research" both collapse onto the one
    # ledger identity their track record belongs to.
    tokens = cleaned.split()
    for length in range(len(tokens) - 1, 0, -1):
        prefix = " ".join(tokens[:length]).lower()
        if prefix in BROKER_ALIASES:
            return BROKER_ALIASES[prefix]
    if len(tokens) > 5:
        return None
    return " ".join(w if w.isupper() else w.capitalize() for w in tokens)


class BrokersSource(Source):
    """One SourceCall per named brokerage per IPO."""

    name = "brokers"
    segment_coverage = (Segment.MAINBOARD, Segment.SME)

    def fetch(self, ipos: Sequence[IPO]) -> list[SourceCall]:
        now = datetime.now(timezone.utc).isoformat()
        targets = [i for i in self.relevant(ipos) if i.status in (IPOStatus.OPEN, IPOStatus.UPCOMING)]
        if not targets:
            return []

        calls: list[SourceCall] = []
        any_article = False
        for ipo in targets:
            articles = self._find_articles(ipo)
            if articles:
                any_article = True
            found: dict[str, SourceCall] = {}
            for url in articles[:MAX_ARTICLES_PER_IPO]:
                for firm, stance, rationale in self._extract(url, ipo):
                    if firm in found:
                        continue
                    found[firm] = SourceCall(
                        source_name=firm,
                        ipo_slug=ipo.slug,
                        ipo_name=ipo.name,
                        stance=stance,
                        rationale=rationale,
                        url=url,
                        captured_at=now,
                        segment=ipo.segment,
                    )
            if not found:
                self.log.info("no named brokerage view found for %s", ipo.name)
            calls.extend(found.values())

        if not any_article:
            self.fail("brokers: no roundup article reachable for any open issue")
        return calls

    # -- discovery ---------------------------------------------------------------------

    def _find_articles(self, ipo: IPO) -> list[str]:
        """Publisher topic pages first — they hand back direct, fetchable article URLs."""
        urls: list[str] = []
        for hit in topic_search(ipo.name):
            if self._allowed(hit.url) and hit.url not in urls:
                urls.append(hit.url)
        if urls:
            return urls
        urls = self._search_articles(ipo)
        if urls:
            return urls
        return self._scan_roundup_indexes(ipo)

    def _search_articles(self, ipo: IPO) -> list[str]:
        urls: list[str] = []
        for hit in web_search(QUERY_TEMPLATE.format(name=ipo.name)):
            if self._allowed(hit.url) and hit.url not in urls:
                urls.append(hit.url)
        return urls

    def _scan_roundup_indexes(self, ipo: IPO) -> list[str]:
        """Fallback: walk the IPO index pages and pick links naming this company."""
        urls: list[str] = []
        for index_url in SOURCE_URLS["broker_roundups"]:
            soup = get_soup(index_url)
            if soup is None:
                continue
            for link in soup.select(SELECTORS["roundup_link"]):
                href = str(link.get("href") or "")
                text = clean_text(link)
                if not href or len(text) < 15:
                    continue
                if name_similarity(ipo.name, text) < 0.45:
                    continue
                url = unwrap_ddg(href)
                if url and self._allowed(url) and url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _allowed(url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(domain in host for domain in ALLOWED_DOMAINS)

    # -- extraction --------------------------------------------------------------------

    def _extract(self, url: str, ipo: IPO) -> list[tuple[str, Stance, str]]:
        soup = get_soup(url)
        if soup is None:
            return []
        paragraphs = [clean_text(p) for p in soup.select(SELECTORS["article_body"])][:MAX_PARAGRAPHS]
        text = " ".join(p for p in paragraphs if p)
        if not text:
            return []
        # Guard against a search result about a different company.
        first_token = ipo.name.split()[0]
        if first_token.lower() not in text.lower():
            return []
        return list(self._firms_in(text))

    def _firms_in(self, text: str) -> Iterable[tuple[str, Stance, str]]:
        sentences = [s for s in SENTENCE_SPLIT.split(text) if s]
        seen: set[str] = set()
        for index, sentence in enumerate(sentences):
            window = " ".join(sentences[index : index + 2])
            stance = classify_stance(window)
            if stance is Stance.NO_VIEW:
                continue
            for raw in FIRM_PATTERN.findall(sentence):
                firm = canonical_firm(raw)
                if firm is None or firm in seen:
                    continue
                seen.add(firm)
                yield firm, stance, snippet(window, 200)
