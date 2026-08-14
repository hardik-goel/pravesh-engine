"""Chittorgarh — calendar, detail strip, subscription, listing outcomes.

STATUS NOTE (verified 2026-07-31): chittorgarh.com's report pages are now a client-side
Next.js app, so the report URLs return HTTP 200 with no table in the HTML. The legacy
`/ipo/ipo_dashboard.asp` page still renders server-side and is kept as the fallback. If
this provider yields nothing, `ipowatch.py` takes over as the calendar spine — that
ordering lives in config.DATA_PROVIDERS, not here.

DASHBOARD SHAPE (verified 2026-08-14): the fallback's calendar is the FIRST table, and
its rows carry one `<td colspan="2">` holding both name and dates — "Skyways Air Services
24 - 27 Aug" — with status pills (CT closing today, LT listing today, P pending) rendered
inside the name. Two consequences, both handled and both easy to reintroduce: a row with
a single cell must not be discarded (parse_tables), and `span.badge` must be stripped
before the text is read (SELECTORS["drop"]) or every name is "Shiprocket CT" and matches
nothing. Discarding the row let a recommendation-count table further down the page win
the name match, producing a full calendar with no dates on any row — which reported as a
quiet day while two mainboard issues were open and closing. Mainboard only: the SME
report URL is client-rendered, so a run served by this fallback has no SME coverage.

WHEN THIS BREAKS: patch SELECTORS / HEADER_KEYS / DETAIL_KEYS below. If the site returns
rows again, put "chittorgarh" back at the head of config.DATA_PROVIDERS["calendar"].
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional, Sequence

from ..config import SOURCE_URLS
from ..models import IPO, IPOStatus, Segment, Subscription
from .base import (
    DataProvider,
    clean_text,
    get_soup,
    label_value_pairs,
    lookup,
    match_ipo,
    parse_date,
    parse_date_range,
    parse_int,
    parse_number,
    parse_price_band,
    parse_tables,
)

# --------------------------------------------------------------------------------------
# SELECTORS — patch here when the site changes
# --------------------------------------------------------------------------------------

SELECTORS: dict[str, str] = {
    "table": "table",
    "row": "tr",
    "header_cell": "th",
    "cell": "td",
    "link": "a[href]",
    # Status pills the dashboard hangs off the company name: CT (closing today),
    # LT (listing today), P (pending). They are rendered inside the name cell, so
    # without this the issue reads as "Shiprocket CT" and matches nothing.
    "drop": "span.badge",
}

HEADER_KEYS: dict[str, list[str]] = {
    "name": ["issuer company", "company name", "ipo name", "company", "issuer"],
    "open_date": ["open date", "opening date", "issue open"],
    "close_date": ["close date", "closing date", "issue close"],
    "date_range": ["issue date", "ipo date", "bidding date"],
    "listing_date": ["listing date", "list date"],
    "allotment_date": ["allotment", "basis of allotment"],
    "price": ["issue price", "price band", "price (rs"],
    "lot_size": ["lot size", "market lot"],
    "issue_size": ["issue size", "total issue size", "issue amount"],
    "exchange": ["exchange", "listing at"],
    "qib": ["qib"],
    "nii": ["nii", "hni", "non institutional", "non-institutional"],
    "retail": ["retail", "rii"],
    "employee": ["employee"],
    "total": ["total", "subscription"],
    "listing_price": ["listing open", "listing price", "list price", "current price"],
    "listing_gain": ["listing gain", "gain/loss", "gain %", "listing day gain"],
}

DETAIL_KEYS: dict[str, list[str]] = {
    "price_band": ["price band", "issue price"],
    "lot_size": ["lot size", "market lot"],
    "issue_size": ["total issue size", "issue size"],
    "fresh_issue": ["fresh issue"],
    "ofs": ["offer for sale", "ofs"],
    "open_date": ["ipo open date", "issue open date", "open date"],
    "close_date": ["ipo close date", "issue close date", "close date"],
    "allotment_date": ["basis of allotment", "allotment"],
    "listing_date": ["listing date"],
    "min_investment": ["minimum amount", "min investment", "retail minimum"],
    "exchange": ["listing at", "exchange"],
}

MAX_DETAIL_PAGES = 24

#: The dashboard prints "Ardee Industries 05 - 07 Aug" in a single cell.
_TRAILING_DATES = re.compile(
    r"\s+(\d{1,2}\s*(?:[A-Za-z]{3,9})?\s*[-–—]\s*\d{1,2}\s*[A-Za-z]{3,9}.*)$"
)


def split_name_and_dates(cell: str) -> tuple[str, Optional[str]]:
    text = clean_text(cell)
    match = _TRAILING_DATES.search(text)
    if not match:
        return text, None
    return text[: match.start()].strip(), match.group(1).strip()


class ChittorgarhSource(DataProvider):
    """Calendar + detail strip + live subscription + listing outcomes."""

    name = "chittorgarh"
    segment_coverage = (Segment.MAINBOARD, Segment.SME)

    # -- calendar ----------------------------------------------------------------------

    def fetch_calendar(self, today: date) -> list[IPO]:
        ipos: list[IPO] = []
        ipos.extend(self._fetch_segment(SOURCE_URLS["chittorgarh_mainboard"], Segment.MAINBOARD, today))
        ipos.extend(self._fetch_segment(SOURCE_URLS["chittorgarh_sme"], Segment.SME, today))
        deduped: dict[str, IPO] = {}
        for ipo in ipos:
            existing = deduped.get(ipo.slug)
            if existing is None or (existing.open_date is None and ipo.open_date is not None):
                deduped[ipo.slug] = ipo
        result = list(deduped.values())
        if not result:
            self.fail("chittorgarh: calendar returned zero rows (site is client-rendered)")
        return result

    def _fetch_segment(self, urls: Sequence[str], segment: Segment, today: date) -> list[IPO]:
        tables, used_url = self.first_url_with_rows(urls, HEADER_KEYS, selectors=SELECTORS)
        if not tables:
            self.fail(f"chittorgarh: no rows for {segment.value.lower()} — check SELECTORS")
            return []
        self.log.info("calendar rows from %s", used_url)
        ipos: list[IPO] = []
        for row in (r for table in tables for r in table):
            ipo = self._row_to_ipo(row, segment, today)
            if ipo is not None:
                ipos.append(ipo)
        return ipos

    def _row_to_ipo(self, row: dict[str, Any], segment: Segment, today: date) -> Optional[IPO]:
        name, inline_dates = split_name_and_dates(row.get("name", ""))
        name = re.sub(r"\s+IPO$", "", name).strip()
        if not name or len(name) < 3:
            return None

        open_date = parse_date(row.get("open_date"), default_year=today.year)
        close_date = parse_date(row.get("close_date"), default_year=today.year)
        if open_date is None or close_date is None:
            ranged_open, ranged_close = parse_date_range(row.get("date_range") or inline_dates, today)
            open_date = open_date or ranged_open
            close_date = close_date or ranged_close

        low, high = parse_price_band(row.get("price"))
        ipo = IPO(
            name=name,
            segment=segment,
            open_date=open_date,
            close_date=close_date,
            listing_date=parse_date(row.get("listing_date"), default_year=today.year),
            allotment_date=parse_date(row.get("allotment_date"), default_year=today.year),
            price_band_low=low,
            price_band_high=high,
            lot_size=parse_int(row.get("lot_size")),
            issue_size_cr=parse_number(row.get("issue_size")),
            exchange=clean_text(row.get("exchange")) or None,
            detail_url=row.get("_link"),
        )
        if ipo.lot_size and ipo.price_band_high:
            ipo.min_investment = round(ipo.lot_size * ipo.price_band_high, 2)
        ipo.status = ipo.status_on(today)
        return ipo

    # -- detail strip ------------------------------------------------------------------

    def enrich_details(self, ipos: Sequence[IPO], today: date) -> None:
        targets = [
            i
            for i in ipos
            if i.detail_url
            and "chittorgarh" in str(i.detail_url)
            and i.status in (IPOStatus.OPEN, IPOStatus.UPCOMING, IPOStatus.CLOSED)
        ]
        for ipo in targets[:MAX_DETAIL_PAGES]:
            soup = get_soup(str(ipo.detail_url))
            if soup is None:
                self.fail(f"chittorgarh: detail page unreachable for {ipo.name}")
                continue
            pairs = label_value_pairs(soup, selectors=SELECTORS)
            if not pairs:
                self.fail(f"chittorgarh: detail page unparsed for {ipo.name} — check DETAIL_KEYS")
                continue
            apply_detail_pairs(ipo, pairs, today, DETAIL_KEYS)

    # -- live subscription -------------------------------------------------------------

    def enrich_subscription(self, ipos: Sequence[IPO]) -> int:
        tables, used_url = self.first_url_with_rows(
            SOURCE_URLS["chittorgarh_subscription"], HEADER_KEYS, selectors=SELECTORS
        )
        if not tables:
            self.fail("chittorgarh: live subscription table empty — check SELECTORS")
            return 0
        matched = 0
        for row in (r for table in tables for r in table):
            name, _ = split_name_and_dates(row.get("name", ""))
            ipo = match_ipo(name, ipos)
            if ipo is None:
                continue
            subscription = Subscription(
                qib=parse_number(row.get("qib")),
                nii=parse_number(row.get("nii")),
                retail=parse_number(row.get("retail")),
                employee=parse_number(row.get("employee")),
                total=parse_number(row.get("total")),
                updated_at=used_url,
            )
            if subscription.has_data:
                ipo.subscription = subscription
                matched += 1
        self.log.info("subscription matched for %d IPO(s)", matched)
        return matched

    # -- listing outcomes --------------------------------------------------------------

    def fetch_listing_outcomes(self, ipos: Sequence[IPO]) -> dict[str, dict[str, Optional[float]]]:
        tables, _ = self.first_url_with_rows(
            SOURCE_URLS["chittorgarh_listing"], HEADER_KEYS, selectors=SELECTORS
        )
        if not tables:
            self.fail("chittorgarh: listing performance table empty — check SELECTORS")
            return {}
        outcomes: dict[str, dict[str, Optional[float]]] = {}
        for row in (r for table in tables for r in table):
            name, _ = split_name_and_dates(row.get("name", ""))
            ipo = match_ipo(name, ipos)
            if ipo is None:
                continue
            issue_price = parse_number(row.get("price"))
            listing_price = parse_number(row.get("listing_price"))
            gain = parse_number(row.get("listing_gain"))
            if gain is None and issue_price and listing_price:
                gain = round(100.0 * (listing_price - issue_price) / issue_price, 2)
            if gain is None:
                continue
            outcomes[ipo.slug] = {
                "issue_price": issue_price,
                "listing_price": listing_price,
                "listing_gain_pct": gain,
            }
        self.log.info("listing outcomes resolved for %d IPO(s)", len(outcomes))
        return outcomes


# --------------------------------------------------------------------------------------
# Shared with ipowatch.py — every provider fills the same detail strip
# --------------------------------------------------------------------------------------


def apply_detail_pairs(
    ipo: IPO, pairs: dict[str, str], today: date, keys: dict[str, list[str]]
) -> None:
    """Fill blanks on an IPO from a detail page's label/value pairs. Never overwrites."""
    low, high = parse_price_band(lookup(pairs, keys["price_band"]))
    ipo.price_band_low = ipo.price_band_low or low
    ipo.price_band_high = ipo.price_band_high or high
    ipo.lot_size = ipo.lot_size or parse_int(lookup(pairs, keys["lot_size"]))
    ipo.issue_size_cr = ipo.issue_size_cr or parse_number(lookup(pairs, keys["issue_size"]))
    ipo.fresh_issue_cr = ipo.fresh_issue_cr or parse_number(lookup(pairs, keys["fresh_issue"]))
    ipo.ofs_cr = ipo.ofs_cr or parse_number(lookup(pairs, keys["ofs"]))
    ipo.open_date = ipo.open_date or parse_date(lookup(pairs, keys["open_date"]), default_year=today.year)
    ipo.close_date = ipo.close_date or parse_date(lookup(pairs, keys["close_date"]), default_year=today.year)
    ipo.allotment_date = ipo.allotment_date or parse_date(
        lookup(pairs, keys["allotment_date"]), default_year=today.year
    )
    ipo.listing_date = ipo.listing_date or parse_date(
        lookup(pairs, keys["listing_date"]), default_year=today.year
    )
    ipo.exchange = ipo.exchange or (clean_text(lookup(pairs, keys["exchange"])) or None)

    min_amount = parse_number(lookup(pairs, keys["min_investment"]))
    if min_amount and min_amount > 1000:  # a lot-count column would be a small number
        ipo.min_investment = min_amount
    elif ipo.lot_size and ipo.price_band_high:
        ipo.min_investment = round(ipo.lot_size * ipo.price_band_high, 2)

    if ipo.issue_size_cr:
        if ipo.fresh_issue_cr is not None and ipo.ofs_cr is None:
            ipo.ofs_cr = max(0.0, round(ipo.issue_size_cr - ipo.fresh_issue_cr, 2))
        elif ipo.ofs_cr is not None and ipo.fresh_issue_cr is None:
            ipo.fresh_issue_cr = max(0.0, round(ipo.issue_size_cr - ipo.ofs_cr, 2))
    ipo.status = ipo.status_on(today)


__all__ = ["ChittorgarhSource", "apply_detail_pairs", "split_name_and_dates", "parse_tables"]
