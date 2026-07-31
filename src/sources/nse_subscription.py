"""Live subscription from NSE's own JSON endpoints — the primary bidding-data provider.

This is the exchange's own feed, so it beats scraping an aggregator: QIB / NII / Retail /
Total straight from the source, updated through the bidding window.

NSE requires a cookie handshake (hit the HTML page first) and rejects datacentre IPs on a
bad day. Both failure modes degrade to the next provider in config.DATA_PROVIDERS.

WHEN THIS BREAKS: patch ENDPOINTS / CATEGORY_KEYS below.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence
from urllib.parse import quote

import requests

from ..config import HTTP, SOURCE_URLS
from ..models import IPO, Segment, Subscription
from .base import DataProvider, clean_text, match_ipo

# --------------------------------------------------------------------------------------
# ENDPOINTS — patch here when NSE moves things
# --------------------------------------------------------------------------------------

ENDPOINTS: dict[str, str] = {
    "bootstrap": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
    "current": "https://www.nseindia.com/api/ipo-current-issue",
    "detail": "https://www.nseindia.com/api/ipo-detail?symbol={symbol}&series={series}",
}

#: Substring of NSE's category label -> Subscription field.
CATEGORY_KEYS: dict[str, str] = {
    "qualified institutional": "qib",
    "non institutional": "nii",
    "retail individual": "retail",
    "employee": "employee",
    "total": "total",
}

#: Sub-rows we must not mistake for the headline category (they have no offered count).
CATEGORY_IGNORE = ("foreign institutional", "domestic financial", "mutual funds", "others")


class NSESubscriptionSource(DataProvider):
    """Live QIB/NII/Retail/Total from nseindia.com."""

    name = "nse_subscription"
    segment_coverage = (Segment.MAINBOARD, Segment.SME)

    def __init__(self) -> None:
        super().__init__()
        self.session: Optional[requests.Session] = None

    # -- session -----------------------------------------------------------------------

    def _bootstrap(self) -> Optional[requests.Session]:
        if self.session is not None:
            return self.session
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": str(HTTP["user_agent"]),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": str(HTTP["accept_language"]),
                "Referer": ENDPOINTS["bootstrap"],
            }
        )
        try:
            session.get(ENDPOINTS["bootstrap"], timeout=float(HTTP["timeout_seconds"]))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"nse: cookie handshake failed ({type(exc).__name__})")
            return None
        self.session = session
        return session

    def _json(self, url: str) -> Optional[Any]:
        session = self._bootstrap()
        if session is None:
            return None
        attempts = int(HTTP["retries"]) + 1
        for attempt in range(attempts):
            try:
                response = session.get(url, timeout=float(HTTP["timeout_seconds"]))
                if response.status_code != 200:
                    raise requests.HTTPError(f"HTTP {response.status_code}")
                return response.json()
            except Exception as exc:  # noqa: BLE001
                if attempt == attempts - 1:
                    self.log.warning("nse GET %s failed: %s", url, exc)
                    return None
                self.session = None  # cookies may have expired mid-run
                self._bootstrap()
        return None

    # -- provider ----------------------------------------------------------------------

    def enrich_subscription(self, ipos: Sequence[IPO]) -> int:
        current = self._json(ENDPOINTS["current"])
        if not isinstance(current, list) or not current:
            self.fail("nse: current-issue feed empty or unreachable")
            return 0

        matched = 0
        for issue in current:
            company = clean_text(issue.get("companyName"))
            symbol = clean_text(issue.get("symbol"))
            series = clean_text(issue.get("series")) or "EQ"
            if not company or not symbol:
                continue
            ipo = match_ipo(company, ipos)
            if ipo is None:
                self.log.debug("nse issue %s did not match the calendar", company)
                continue

            subscription = Subscription(updated_at=f"NSE · {symbol}")
            total = _to_float(issue.get("noOfTime"))
            if str(issue.get("category", "")).strip().lower() == "total" and total is not None:
                subscription.total = round(total, 2)

            detail = self._json(
                ENDPOINTS["detail"].format(symbol=quote(symbol), series=quote(series))
            )
            if isinstance(detail, dict):
                self._apply_bid_details(subscription, detail.get("bidDetails") or [])

            if subscription.has_data:
                ipo.subscription = subscription
                matched += 1

        if not matched:
            self.fail("nse: no live issue matched the calendar")
        self.log.info("nse subscription matched for %d IPO(s)", matched)
        return matched

    @staticmethod
    def _apply_bid_details(subscription: Subscription, rows: Sequence[dict[str, Any]]) -> None:
        for row in rows:
            label = clean_text(row.get("category")).lower()
            if any(token in label for token in CATEGORY_IGNORE):
                continue
            times = _to_float(row.get("noOfTime"))
            if times is None:
                continue
            for keyword, field in CATEGORY_KEYS.items():
                if keyword in label:
                    setattr(subscription, field, round(times, 2))
                    break


def _to_float(value: Any) -> Optional[float]:
    text = clean_text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


__all__ = ["NSESubscriptionSource", "SOURCE_URLS"]
