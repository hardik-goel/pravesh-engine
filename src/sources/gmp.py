"""Grey Market Premium — investorgain.com, falling back to ipowatch.in.

Two jobs:
  1. Attach `gmp` / `gmp_pct` to each IPO (detail strip).
  2. Emit the synthetic source "GMP signal", scored in the ledger like any human source.

GMP is indicative and unofficial — every surface labels it as such.

WHEN THIS BREAKS: patch SELECTORS / GMP_HEADER_KEYS below.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from ..config import SOURCE_GMP, SOURCE_URLS, THRESHOLDS
from ..models import IPO, Segment, SourceCall, Stance
from .base import Source, clean_text, match_ipo, parse_number

# --------------------------------------------------------------------------------------
# SELECTORS — patch here when a site changes
# --------------------------------------------------------------------------------------

SELECTORS: dict[str, str] = {
    "table": "table",
    "row": "tr",
    "header_cell": "th",
    "cell": "td",
}

GMP_HEADER_KEYS: dict[str, list[str]] = {
    # Deliberately NOT "ipo": that would swallow the "IPO GMP" and "IPO Price" columns.
    "name": ["ipo name", "company name", "company"],
    "gmp": ["gmp", "grey market", "premium"],
    "price": ["price band", "ipo price", "price"],
    "est_listing": ["est. listing", "est listing", "estimated listing", "expected listing"],
    "gain_pct": ["gain", "profit", "%"],
    "status": ["status"],
}

RATIONALE_LABEL = "Indicative, unofficial grey-market premium"

#: "₹66 (24.53%)" — the percentage is inside the estimated-listing cell on ipowatch.
_PCT_IN_TEXT = re.compile(r"\(?\s*([+-]?\d+(?:\.\d+)?)\s*%")


def percent_in(text: Any) -> Optional[float]:
    match = _PCT_IN_TEXT.search(clean_text(text))
    return float(match.group(1)) if match else None


class GMPSource(Source):
    """Synthetic source: GMP >= threshold implies Apply."""

    name = "gmp"
    segment_coverage = (Segment.MAINBOARD, Segment.SME)

    def fetch(self, ipos: Sequence[IPO]) -> list[SourceCall]:
        candidates = self.relevant(ipos)
        if not candidates:
            return []

        rows, provider = self._fetch_rows()
        if not rows:
            self.fail("gmp: no GMP data from investorgain or ipowatch")
            return []

        now = datetime.now(timezone.utc).isoformat()
        calls: list[SourceCall] = []
        for row in rows:
            ipo = match_ipo(clean_text(row.get("name")), candidates)
            if ipo is None:
                continue
            gmp = parse_number(row.get("gmp"))
            if gmp is None:
                continue
            band_high = ipo.price_band_high or parse_number(row.get("price"))
            gmp_pct = percent_in(row.get("gain_pct")) or percent_in(row.get("est_listing"))
            if gmp_pct is None and band_high:
                gmp_pct = round(100.0 * gmp / band_high, 1)
            ipo.gmp = gmp
            ipo.gmp_pct = gmp_pct
            ipo.gmp_source = provider
            if gmp_pct is None:
                continue
            calls.append(
                SourceCall(
                    source_name=SOURCE_GMP,
                    ipo_slug=ipo.slug,
                    ipo_name=ipo.name,
                    stance=self._stance_for(gmp_pct),
                    rationale=self._rationale(gmp, gmp_pct, provider),
                    url=provider,
                    captured_at=now,
                    segment=ipo.segment,
                    is_synthetic=True,
                )
            )
        if not calls:
            self.fail("gmp: rows fetched but none matched the calendar")
        return calls

    # -- internals ---------------------------------------------------------------------

    def _fetch_rows(self) -> tuple[list[dict[str, Any]], Optional[str]]:
        for key in ("gmp_primary", "gmp_fallback"):
            tables, url = self.first_url_with_rows(
                SOURCE_URLS[key], GMP_HEADER_KEYS, required=("name", "gmp"), selectors=SELECTORS
            )
            if tables:
                # The first matching table is the live board; later ones are history.
                rows = tables[0]
                self.log.info("gmp rows from %s: %d", url, len(rows))
                return rows, url
            self.fail(f"gmp: {key} returned no rows — check SELECTORS")
        return [], None

    @staticmethod
    def _stance_for(gmp_pct: float) -> Stance:
        if gmp_pct >= float(THRESHOLDS["gmp_apply_pct"]):
            return Stance.APPLY
        if gmp_pct >= float(THRESHOLDS["gmp_neutral_pct"]):
            return Stance.NEUTRAL
        return Stance.AVOID

    @staticmethod
    def _rationale(gmp: float, gmp_pct: float, provider: Optional[str]) -> str:
        host = (provider or "").split("//")[-1].split("/")[0] or "grey market"
        direction = "premium" if gmp >= 0 else "discount"
        return (
            f"{RATIONALE_LABEL}: ₹{abs(gmp):,.0f} {direction} "
            f"({gmp_pct:+.1f}% of the upper band), per {host}."
        )
