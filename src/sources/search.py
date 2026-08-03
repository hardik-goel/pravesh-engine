"""Discovery layer — how singhvi.py and brokers.py find what was written about an IPO.

Search engines block datacentre IPs aggressively (verified 2026-07-31: zeebiz.com returns
403, and DuckDuckGo's HTML endpoint serves a captcha). So discovery is a chain of
independent routes, each returning whatever it can:

  1. news_search()  — Google News RSS. Unblocked, server-rendered XML, carries the
                      headline and publisher. Its article links are Google wrappers that
                      cannot be de-referenced server-side, so it is used for headline-level
                      evidence (Singhvi) rather than full-text extraction.
  2. topic_search() — publisher topic/tag pages (Economic Times, Moneycontrol, Livemint).
                      Server-rendered and give direct article URLs, which is what the
                      broker extractor needs.
  3. web_search()   — DuckDuckGo HTML. Kept because it works from some networks.

WHEN THIS BREAKS: patch config.SOURCE_URLS["news_search" / "topic_search" /
"broker_search_fallback"] and the SELECTORS block below.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Optional, Sequence
from urllib.parse import parse_qs, quote_plus, urlparse

from bs4 import BeautifulSoup

from ..config import SOURCE_URLS
from .base import clean_text, get_soup, http_get, name_similarity

log = logging.getLogger(__name__)

SELECTORS: dict[str, str] = {
    "rss_item": "item",
    "rss_title": "title",
    "rss_link": "link",
    "rss_source": "source",
    "rss_description": "description",
    "rss_published": "pubDate",
    "ddg_result": "div.result, div.web-result",
    "ddg_link": "a.result__a",
    "topic_link": "a[href]",
}

MIN_ANCHOR_LEN = 25  # topic pages are full of nav links; real headlines are longer


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    publisher: str = ""
    summary: str = ""
    #: When the item was PUBLISHED (ISO 8601), when the feed says so. Never the scrape time —
    #: staleness downstream is measured from publication, and defaulting to "now" would make
    #: a two-month-old call look fresh.
    published_at: Optional[str] = None

    @property
    def blob(self) -> str:
        return f"{self.title} {self.summary}"


def _rss_published(raw: str) -> Optional[str]:
    """RFC 822 pubDate -> ISO 8601. None when absent or unparseable — never guessed."""
    text = clean_text(raw)
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def slugify_for_topic(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", clean_text(name).lower()).strip("-")


# --------------------------------------------------------------------------------------
# 1. Google News RSS
# --------------------------------------------------------------------------------------


def news_search(query: str, *, limit: int = 10) -> list[SearchHit]:
    for template in SOURCE_URLS.get("news_search", []):
        response = http_get(template.format(query=quote_plus(query)))
        if response is None:
            continue
        try:
            soup = BeautifulSoup(response.text, "xml")
        except Exception:  # noqa: BLE001 — lxml-xml missing; the html parser still finds items
            soup = BeautifulSoup(response.text, "html.parser")
        hits: list[SearchHit] = []
        for item in soup.select(SELECTORS["rss_item"])[:limit]:
            title = clean_text(item.find(SELECTORS["rss_title"]))
            link = clean_text(item.find(SELECTORS["rss_link"]))
            if not title or not link:
                continue
            source = item.find(SELECTORS["rss_source"])
            description = clean_text(item.find(SELECTORS["rss_description"]))
            # lxml-xml preserves tag case; the html.parser fallback lowercases it.
            published = item.find(SELECTORS["rss_published"]) or item.find(
                SELECTORS["rss_published"].lower()
            )
            hits.append(
                SearchHit(
                    title=title,
                    url=link,
                    publisher=clean_text(source) if source else "",
                    summary=description,
                    published_at=_rss_published(clean_text(published) if published else ""),
                )
            )
        if hits:
            log.debug("news_search(%r) → %d hit(s)", query, len(hits))
            return hits
    return []


# --------------------------------------------------------------------------------------
# 2. Publisher topic pages
# --------------------------------------------------------------------------------------


def topic_search(ipo_name: str, *, min_similarity: float = 0.35, limit: int = 6) -> list[SearchHit]:
    """Direct article URLs from publisher topic/tag pages, filtered by headline match."""
    slug = slugify_for_topic(f"{ipo_name} IPO")
    bare_slug = slugify_for_topic(ipo_name)
    first_token = ipo_name.split()[0].lower() if ipo_name.split() else ""
    hits: list[SearchHit] = []
    seen: set[str] = set()

    for template in SOURCE_URLS.get("topic_search", []):
        for candidate_slug in (slug, bare_slug):
            url = template.format(slug=candidate_slug)
            soup = get_soup(url)
            if soup is None:
                continue
            host = urlparse(url).netloc
            for anchor in soup.select(SELECTORS["topic_link"]):
                text = clean_text(anchor)
                href = str(anchor.get("href") or "")
                if len(text) < MIN_ANCHOR_LEN or not href:
                    continue
                if first_token and first_token not in text.lower():
                    continue
                if name_similarity(ipo_name, text) < min_similarity and first_token not in text.lower():
                    continue
                full = href if href.startswith("http") else f"https://{host}{href}"
                if full in seen:
                    continue
                seen.add(full)
                hits.append(SearchHit(title=text, url=full, publisher=host))
                if len(hits) >= limit:
                    return hits
            if hits:
                break  # this publisher answered; move to the next one
    return hits


# --------------------------------------------------------------------------------------
# 3. DuckDuckGo HTML
# --------------------------------------------------------------------------------------


def web_search(query: str, *, limit: int = 8) -> list[SearchHit]:
    for template in SOURCE_URLS.get("broker_search_fallback", []):
        soup = get_soup(template.format(query=quote_plus(query)))
        if soup is None:
            continue
        hits: list[SearchHit] = []
        for node in soup.select(SELECTORS["ddg_result"])[:limit]:
            anchor = node.select_one(SELECTORS["ddg_link"]) or node.select_one("a[href]")
            if anchor is None:
                continue
            url = unwrap_ddg(str(anchor.get("href") or ""))
            if not url:
                continue
            hits.append(
                SearchHit(title=clean_text(anchor), url=url, publisher=urlparse(url).netloc,
                          summary=clean_text(node))
            )
        if hits:
            return hits
    return []


def unwrap_ddg(href: str) -> Optional[str]:
    """DuckDuckGo wraps results in /l/?uddg=<encoded>."""
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        return parse_qs(urlparse(href).query).get("uddg", [None])[0]
    return href if href.startswith("http") else None


def best_match(hits: Sequence[SearchHit], ipo_name: str, *, require: str = "") -> Optional[SearchHit]:
    """Highest name-similarity hit, optionally required to mention a token (e.g. 'singhvi')."""
    best: Optional[tuple[float, SearchHit]] = None
    first_token = ipo_name.split()[0].lower() if ipo_name.split() else ""
    for hit in hits:
        blob = hit.blob.lower()
        if require and require not in blob:
            continue
        score = name_similarity(ipo_name, hit.title)
        if score < 0.35 and first_token and first_token in blob:
            score = 0.35
        if score < 0.35:
            continue
        if best is None or score > best[0]:
            best = (score, hit)
    return best[1] if best else None


__all__ = ["SearchHit", "best_match", "news_search", "topic_search", "unwrap_ddg", "web_search"]
