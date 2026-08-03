"""Shared machinery for a named market expert (as opposed to a brokerage house).

An expert source is: search for "<expert> <IPO name>" across independent discovery routes,
take the best hit, classify a stance from headline → summary → article body, emit one
SourceCall per IPO. Everything that differs between experts — the person's name, the anchor
token that proves a result is really theirs, the search endpoints, the CSS selectors and the
query strings — is declared as class attributes by the subclass, in ONE constants block per
file. Nothing site-specific lives here.

Discovery runs through three independent routes (see search.py): the publisher's own search,
Google News RSS, then DuckDuckGo. Whichever answers first wins.

NO_VIEW is first-class and non-penalising — no expert covers every SME issue. That absence
is information, not a failure, and is never written to `sources_failed`. Only a *total*
discovery outage (no route answered for anything) is reported as a failure.

WHEN THIS BREAKS: patch the subclass's SELECTORS / QUERY_TEMPLATES, or the search endpoints
in config.SOURCE_URLS.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from urllib.parse import quote_plus, urljoin

from ..config import SOURCE_URLS
from ..models import IPO, IPOStatus, Segment, SourceCall, Stance
from .base import (
    Source,
    classify_stance_verbatim,
    clean_text,
    get_soup,
    last_outcome,
    name_similarity,
    snippet,
)
from .search import SearchHit, best_match, news_search, web_search

#: Below this, a headline is only accepted if it at least carries the issue's first word.
MIN_NAME_SIMILARITY = 0.55

# A blocked host costs ~35s per IPO (3 attempts + backoff) and is blocked for the whole run,
# not for one issue. With two experts on the panel and 20+ issues on the calendar, retrying a
# dead route per IPO is the difference between a 3-minute run and one that eats the workflow's
# 25-minute budget. After this many consecutive no-answers a route is dropped for the rest of
# the run and the remaining routes carry the source — which is exactly what they are for.
ROUTE_GIVE_UP_AFTER = 3


class Reach:
    """What discovery managed for ONE issue.

    `answered` is the question that matters downstream: did anything respond about this
    specific IPO? If nothing did, a NO_VIEW on it carries no information — it is "we could
    not check", not "nobody had a view" — and the two must never render the same.
    """

    __slots__ = ("answered_routes", "quiet_routes", "found_via")

    def __init__(self) -> None:
        self.answered_routes: list[str] = []
        self.quiet_routes: list[str] = []
        self.found_via: Optional[str] = None

    @property
    def answered(self) -> bool:
        return bool(self.answered_routes)

    def note(self, route: str, *, answered: bool) -> None:
        (self.answered_routes if answered else self.quiet_routes).append(route)

    def via(self, route: str) -> "Reach":
        self.found_via = route
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.answered,
            "answeredRoutes": list(self.answered_routes),
            "quietRoutes": list(self.quiet_routes),
            "foundVia": self.found_via,
        }


class NamedExpertSource(Source):
    """One named expert's calls on the IPO calendar. Emits NO_VIEW rather than guessing."""

    #: Ledger identity — must match the SOURCE_* constant in config.
    expert_name: str = ""
    #: A result without this token in its text is not this person's call.
    anchor_token: str = ""
    #: Key into config.SOURCE_URLS for the publisher's own search endpoint.
    search_urls_key: str = ""
    #: Publisher shown in the rationale when a hit does not name its own source.
    default_publisher: str = ""
    #: Host label attached to hits from the publisher's own search page.
    site_publisher: str = ""
    #: How the publisher search route is named in the degraded notice.
    discovery_label: str = ""
    #: CSS selectors for the publisher search page + article body.
    selectors: dict[str, str] = {}
    #: Query strings for each discovery route: "site", "news", "web".
    query_templates: dict[str, str] = {}
    #: How much of the article to read when the headline is vague.
    max_article_paragraphs: int = 6

    segment_coverage = (Segment.MAINBOARD, Segment.SME)  # SME queried too, usually NO_VIEW

    def __init__(self) -> None:
        super().__init__()
        #: Consecutive no-answers per route, this run only. See ROUTE_GIVE_UP_AFTER.
        self._route_misses: dict[str, int] = {}
        #: Per-route outcome for this run — what each route DID, not just whether it helped.
        #: {route: {"attempted", "answered", "hits", "reason"}}. Read by engine/expert_feed.
        self.route_report: dict[str, dict[str, object]] = {}
        #: Per-ISSUE discovery outcome: {ipo_slug: Reach.to_dict()}. Run-level route counts
        #: cannot answer "could we check THIS issue?" — a route that answers 2 of 4 times
        #: leaves 2 issues unchecked while still looking alive in the aggregate.
        self.issue_reach: dict[str, dict[str, Any]] = {}

    def fetch(self, ipos: Sequence[IPO]) -> list[SourceCall]:
        now = datetime.now(timezone.utc).isoformat()
        calls: list[SourceCall] = []
        any_route_answered = False
        self._route_misses = {}
        self.route_report = {}
        self.issue_reach = {}

        for ipo in self._targets(ipos):
            hit, body, reach = self._find(ipo)
            any_route_answered = any_route_answered or reach.answered
            self.issue_reach[ipo.slug] = reach.to_dict()
            raw_call = ""
            published_at = None
            publisher = ""
            if hit is None:
                stance = Stance.NO_VIEW
                url = None
                rationale = f"No {self.default_publisher} call found for this issue."
            else:
                # Headline first, then the feed summary, then the body — each classified
                # with the phrase that decided it kept verbatim.
                for text in (hit.title, hit.summary, body):
                    stance, raw_call = classify_stance_verbatim(text or "")
                    if stance is not Stance.NO_VIEW:
                        break
                url = hit.url
                published_at = hit.published_at
                publisher = hit.publisher or self.default_publisher
                rationale = self._rationale(stance, hit, body)

            calls.append(
                SourceCall(
                    source_name=self.expert_name,
                    ipo_slug=ipo.slug,
                    ipo_name=ipo.name,
                    stance=stance,
                    rationale=rationale,
                    url=url,
                    captured_at=now,
                    segment=ipo.segment,
                    raw_call=raw_call,
                    published_at=published_at,
                    publisher=publisher,
                    discovery_reachable=reach.answered,
                    discovery_route=reach.found_via,
                )
            )

        self._finalise_routes()

        # Only a total discovery outage is a failure. NO_VIEW everywhere is normal.
        if calls and not any_route_answered:
            self.fail(
                f"{self.name}: every discovery route "
                f"({self.discovery_label}, Google News, DuckDuckGo) returned nothing"
            )
        return calls

    # -- internals ---------------------------------------------------------------------

    def _targets(self, ipos: Sequence[IPO]) -> list[IPO]:
        """Only issues a view can still act on."""
        return [i for i in self.relevant(ipos) if i.status in (IPOStatus.OPEN, IPOStatus.UPCOMING)]

    def _route_open(self, route: str) -> bool:
        """False once a route has gone quiet often enough to stop being worth the wait."""
        return self._route_misses.get(route, 0) < ROUTE_GIVE_UP_AFTER

    def _note_route(self, route: str, *, answered: bool, hits: int = 0, reason: str = "") -> None:
        """Record what a route did. `answered` = it responded; `hits` = it had something.

        Counts only — the human-readable reason is derived once at the end of the run by
        `_finalise_routes`, so a route that worked twice and then died does not get reported
        as if it had never worked.
        """
        entry = self.route_report.setdefault(
            route,
            {"attempted": 0, "answered": 0, "hits": 0, "reason": "", "blockReason": "",
             "dropped": False},
        )
        entry["attempted"] = int(entry["attempted"]) + 1
        if answered:
            entry["answered"] = int(entry["answered"]) + 1
            entry["hits"] = int(entry["hits"]) + hits
            self._route_misses[route] = 0
            return
        # An empty result from a host that answered fine means "nothing published", not a block.
        entry["blockReason"] = reason or "responded, nothing in the result set"
        misses = self._route_misses.get(route, 0) + 1
        self._route_misses[route] = misses
        if misses == ROUTE_GIVE_UP_AFTER:
            entry["dropped"] = True
            self.log.info(
                "%s: %s answered nothing %d times running (%s) — skipping it for the rest of "
                "this run",
                self.name,
                route,
                misses,
                entry["blockReason"],
            )

    def _finalise_routes(self) -> None:
        """Turn the per-route counts into one honest sentence each."""
        for entry in self.route_report.values():
            attempted = int(entry["attempted"])
            answered = int(entry["answered"])
            hits = int(entry["hits"])
            block = str(entry["blockReason"])
            dropped = bool(entry["dropped"])
            if answered == 0:
                reason = block or "no response"
            elif hits:
                reason = f"{hits} match(es) from {answered}/{attempted} responses"
            else:
                reason = f"responded {answered}/{attempted}, no matching item"
            if dropped and answered < attempted:
                reason = f"{reason} — route dropped after {ROUTE_GIVE_UP_AFTER} in a row"
            entry["reason"] = reason

    def _find(self, ipo: IPO) -> tuple[Optional[SearchHit], str, "Reach"]:
        """-> (best hit, article body if fetchable, what discovery managed FOR THIS ISSUE).

        Routes are tried in order and each is dropped for the remainder of the run once it
        has gone quiet ROUTE_GIVE_UP_AFTER times in a row — a host that is blocking us is
        blocking us for the whole run, and re-proving that per IPO costs minutes.

        Reachability is tracked PER ISSUE, not per run. A route that answered for three
        IPOs and went quiet on the fourth leaves that fourth issue genuinely unchecked, and
        reporting it as "nobody had a view" would manufacture a coverage fact out of a rate
        limit.
        """
        reach = Reach()

        site_route = f"{self.discovery_label} search"
        if self._route_open(site_route):
            site_hit, site_reachable = self._search_publisher(ipo)
            self._note_route(
                site_route,
                answered=site_reachable,
                hits=1 if site_hit is not None else 0,
                reason=self._host_reason(SOURCE_URLS[self.search_urls_key]),
            )
            reach.note(site_route, answered=site_reachable)
            if site_hit is not None:
                return site_hit, self._article_body(site_hit.url), reach.via(site_route)

        if self._route_open("Google News RSS"):
            news_hits = news_search(self.query_templates["news"].format(name=ipo.name))
            hit = (
                best_match(news_hits, ipo.name, require=self.anchor_token) if news_hits else None
            )
            self._note_route(
                "Google News RSS",
                answered=bool(news_hits),
                hits=1 if hit is not None else 0,
                reason=self._host_reason(SOURCE_URLS.get("news_search", [])),
            )
            reach.note("Google News RSS", answered=bool(news_hits))
            if hit is not None:
                # Google News links are wrappers; the body is not fetchable server-side.
                return hit, "", reach.via("Google News RSS")

        if self._route_open("DuckDuckGo"):
            web_hits = web_search(self.query_templates["web"].format(name=ipo.name))
            hit = best_match(web_hits, ipo.name, require=self.anchor_token) if web_hits else None
            self._note_route(
                "DuckDuckGo",
                answered=bool(web_hits),
                hits=1 if hit is not None else 0,
                reason=self._host_reason(SOURCE_URLS.get("broker_search_fallback", [])),
            )
            reach.note("DuckDuckGo", answered=bool(web_hits))
            if hit is not None:
                return hit, self._article_body(hit.url), reach.via("DuckDuckGo")

        return None, "", reach

    @staticmethod
    def _host_reason(url_templates: Sequence[str]) -> str:
        """The real HTTP reason the first configured host last gave us ("HTTP 403")."""
        if not url_templates:
            return ""
        outcome = last_outcome(url_templates[0])
        if outcome is None or outcome.ok:
            return ""
        return outcome.reason

    def _search_publisher(self, ipo: IPO) -> tuple[Optional[SearchHit], bool]:
        """-> (best hit, whether the publisher's search page responded at all)."""
        query = quote_plus(self.query_templates["site"].format(name=ipo.name))
        soup, used_url = self.first_working_soup(SOURCE_URLS[self.search_urls_key], query=query)
        if soup is None:
            return None, False
        best: Optional[tuple[float, SearchHit]] = None
        for node in soup.select(self.selectors["result"]):
            link = node.select_one(self.selectors["link"])
            if link is None or not link.get("href"):
                continue
            headline = clean_text(link) or clean_text(node.select_one(self.selectors["title"]))
            blob = clean_text(node).lower()
            if self.anchor_token not in blob:
                continue
            score = name_similarity(ipo.name, headline)
            if score < MIN_NAME_SIMILARITY:
                if ipo.name.split()[0].lower() not in blob:
                    continue
                score = MIN_NAME_SIMILARITY
            href = str(link["href"])
            url = href if href.startswith("http") else urljoin(used_url or "", href)
            hit = SearchHit(
                title=headline,
                url=url,
                publisher=self.site_publisher or self.default_publisher,
                summary=blob[:300],
            )
            if best is None or score > best[0]:
                best = (score, hit)
        # The page loaded, so the route is alive even when it had nothing on this issue.
        return (best[1] if best else None), True

    def _article_body(self, url: str) -> str:
        """Opening paragraphs, so a vague headline still yields a stance."""
        article = get_soup(url)
        if article is None:
            return ""
        paragraphs = [clean_text(p) for p in article.select(self.selectors["article_body"])]
        return " ".join(p for p in paragraphs[: self.max_article_paragraphs] if p)

    def _rationale(self, stance: Stance, hit: SearchHit, body: str) -> str:
        publisher = hit.publisher or self.default_publisher
        headline = snippet(hit.title, 160)
        if stance is Stance.NO_VIEW:
            return snippet(headline or "Coverage found but no explicit call.", 160)
        if headline:
            return f"{publisher}: “{headline}”"
        return snippet(body or hit.summary, 180)


__all__ = ["NamedExpertSource"]
