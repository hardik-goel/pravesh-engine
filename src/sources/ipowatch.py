"""IPO Watch — server-rendered calendar, detail strip and listing outcomes.

This is the working spine as of 2026-07-31 (chittorgarh.com went client-side). Two
calendar tables: mainboard first, then SME with a "Platform" column (BSE SME / NSE SME).
The GMP page doubles as the listing-outcome ledger: it carries IPO price and actual
listing price for every recent issue.

WHEN THIS BREAKS: patch SELECTORS / HEADER_KEYS / DETAIL_KEYS below.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional, Sequence

from ..config import SOURCE_URLS
from ..models import IPO, IPOStatus, Segment
from .base import (
    DataProvider,
    clean_text,
    get_soup,
    label_value_pairs,
    match_ipo,
    parse_date,
    parse_date_range,
    parse_int,
    parse_number,
    parse_price_band,
)
from .chittorgarh import apply_detail_pairs

# --------------------------------------------------------------------------------------
# SELECTORS — patch here when the site changes
# --------------------------------------------------------------------------------------

SELECTORS: dict[str, str] = {
    "table": "table",
    "row": "tr",
    "header_cell": "th",
    "cell": "td",
    "link": "a[href]",
}

HEADER_KEYS: dict[str, list[str]] = {
    # Deliberately NOT a bare "ipo": it would swallow "IPO Size" / "IPO Price Band".
    # A leading column that matches nothing is treated as the name (see parse_tables).
    "name": ["company", "ipo name"],
    "date_range": ["ipo date", "date"],
    "issue_size": ["ipo size", "issue size"],
    "price": ["price band", "ipo price", "price"],
    "platform": ["platform", "type"],
    "status": ["status"],
    "listing_price": ["listing price"],
    "gmp": ["gmp"],
}

DETAIL_KEYS: dict[str, list[str]] = {
    "price_band": ["ipo price band", "price band"],
    "lot_size": ["lot size", "market lot"],
    "issue_size": ["issue size"],
    "fresh_issue": ["fresh issue"],
    "ofs": ["offer for sale", "ofs"],
    "open_date": ["ipo open date", "open date"],
    "close_date": ["ipo close date", "close date"],
    "allotment_date": ["basis of allotment", "allotment"],
    "listing_date": ["ipo listing date", "listing date"],
    "min_investment": ["retail minimum", "minimum amount"],
    "exchange": ["listing at", "exchange"],
}

MAX_DETAIL_PAGES = 24

#: The SME table is the one carrying an exchange platform per row.
SME_PLATFORM_TOKENS = ("sme", "emerge", "bse sme", "nse sme")
DETAIL_URL_HINT = "ipowatch.in"


def _segment_for(row: dict[str, Any], table_index: int) -> Segment:
    platform = clean_text(row.get("platform")).lower()
    if platform:
        return Segment.SME if any(token in platform for token in SME_PLATFORM_TOKENS) else Segment.MAINBOARD
    # Table 0 is mainboard, table 1 is SME on the calendar page.
    return Segment.SME if table_index >= 1 else Segment.MAINBOARD


class IPOWatchSource(DataProvider):
    """Calendar spine + detail strip + listing outcomes."""

    name = "ipowatch"
    segment_coverage = (Segment.MAINBOARD, Segment.SME)

    # -- calendar ----------------------------------------------------------------------

    def fetch_calendar(self, today: date) -> list[IPO]:
        tables, used_url = self.first_url_with_rows(
            SOURCE_URLS["ipowatch_calendar"],
            HEADER_KEYS,
            required=("name", "date_range"),
            selectors=SELECTORS,
        )
        if not tables:
            self.fail("ipowatch: calendar table empty — check SELECTORS")
            return []
        self.log.info("calendar rows from %s", used_url)

        ipos: dict[str, IPO] = {}
        for table_index, table in enumerate(tables):
            for row in table:
                ipo = self._row_to_ipo(row, _segment_for(row, table_index), today)
                if ipo is None:
                    continue
                existing = ipos.get(ipo.slug)
                if existing is None or (existing.open_date is None and ipo.open_date):
                    ipos[ipo.slug] = ipo
        return list(ipos.values())

    def _row_to_ipo(self, row: dict[str, Any], segment: Segment, today: date) -> Optional[IPO]:
        name = re.sub(r"\s+IPO$", "", clean_text(row.get("name"))).strip()
        if not name or len(name) < 3:
            return None
        if "tba" in clean_text(row.get("date_range")).lower():
            return None  # "Reliance Jio · 2026 · TBA" — a rumour, not a calendar entry
        open_date, close_date = parse_date_range(row.get("date_range"), today)
        if close_date is None:
            return None
        low, high = parse_price_band(row.get("price"))
        ipo = IPO(
            name=name,
            segment=segment,
            open_date=open_date,
            close_date=close_date,
            price_band_low=low,
            price_band_high=high,
            issue_size_cr=parse_number(row.get("issue_size")),
            exchange=clean_text(row.get("platform")) or None,
            detail_url=row.get("_link") if DETAIL_URL_HINT in str(row.get("_link", "")) else None,
        )
        ipo.status = ipo.status_on(today)
        return ipo

    # -- detail strip ------------------------------------------------------------------

    def enrich_details(self, ipos: Sequence[IPO], today: date) -> None:
        targets = [
            i
            for i in ipos
            if i.detail_url
            and DETAIL_URL_HINT in str(i.detail_url)
            and i.status in (IPOStatus.OPEN, IPOStatus.UPCOMING, IPOStatus.CLOSED)
        ]
        if not targets:
            return
        parsed = 0
        for ipo in targets[:MAX_DETAIL_PAGES]:
            soup = get_soup(str(ipo.detail_url))
            if soup is None:
                self.fail(f"ipowatch: detail page unreachable for {ipo.name}")
                continue
            pairs = label_value_pairs(soup, selectors=SELECTORS)
            if not pairs:
                self.fail(f"ipowatch: detail page unparsed for {ipo.name} — check DETAIL_KEYS")
                continue
            apply_detail_pairs(ipo, pairs, today, DETAIL_KEYS)
            self._apply_lot_table(ipo, soup)
            parsed += 1
        self.log.info("detail strip filled for %d IPO(s)", parsed)

    def _apply_lot_table(self, ipo: IPO, soup: Any) -> None:
        """The lot table is 'Application | Lot Size | Shares | Amount' — retail minimum row."""
        for table in soup.select(SELECTORS["table"]):
            rows = table.select(SELECTORS["row"])
            if not rows:
                continue
            header = " ".join(clean_text(c) for c in rows[0].select("td, th")).lower()
            if "lot size" not in header or "amount" not in header:
                continue
            for tr in rows[1:]:
                cells = [clean_text(c) for c in tr.select(SELECTORS["cell"])]
                if len(cells) < 4 or "retail minimum" not in cells[0].lower():
                    continue
                shares = parse_int(cells[2])
                amount = parse_number(cells[3])
                if shares:
                    ipo.lot_size = ipo.lot_size or shares
                if amount:
                    ipo.min_investment = amount
                return

    # -- listing outcomes --------------------------------------------------------------

    def fetch_listing_outcomes(self, ipos: Sequence[IPO]) -> dict[str, dict[str, Optional[float]]]:
        tables, _ = self.first_url_with_rows(
            SOURCE_URLS["ipowatch_listing"],
            HEADER_KEYS,
            required=("name", "listing_price"),
            selectors=SELECTORS,
        )
        if not tables:
            self.fail("ipowatch: listing table empty — check SELECTORS")
            return {}
        outcomes: dict[str, dict[str, Optional[float]]] = {}
        for row in (r for table in tables for r in table):
            ipo = match_ipo(clean_text(row.get("name")), ipos)
            if ipo is None:
                continue
            issue_price = parse_number(row.get("price"))
            listing_price = parse_number(row.get("listing_price"))
            if not issue_price or not listing_price:
                continue
            outcomes[ipo.slug] = {
                "issue_price": issue_price,
                "listing_price": listing_price,
                "listing_gain_pct": round(100.0 * (listing_price - issue_price) / issue_price, 2),
            }
        self.log.info("listing outcomes resolved for %d IPO(s)", len(outcomes))
        return outcomes
