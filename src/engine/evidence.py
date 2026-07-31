"""Assemble the per-IPO evidence table — the heart of the product.

Each row is: source name · stance · rationale snippet · that source's own historical
accuracy (always with n, "insufficient history" below n=5).

Row order is deliberate and fixed: Anil Singhvi, then each named brokerage, then the two
synthetic signals. The named humans come first because the table is meant to be read as
"here is who said what", not as a scoreboard.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..config import SOURCE_GMP, SOURCE_QIB, SOURCE_SINGHVI
from ..models import IPO, Evidence, EvidenceRow, SourceCall, Stance
from .source_tracker import SourceTracker

log = logging.getLogger(__name__)

SYNTHETIC_ORDER = [SOURCE_GMP, SOURCE_QIB]


def _sort_key(call: SourceCall) -> tuple[int, str]:
    if call.source_name == SOURCE_SINGHVI:
        return (0, call.source_name)
    if call.source_name in SYNTHETIC_ORDER:
        return (2 + SYNTHETIC_ORDER.index(call.source_name), call.source_name)
    return (1, call.source_name.lower())


def build_evidence(ipo: IPO, calls: Sequence[SourceCall], tracker: SourceTracker) -> Evidence:
    """One evidence table for one IPO."""
    rows: list[EvidenceRow] = []
    for call in sorted(calls, key=_sort_key):
        stats = tracker.accuracy(call.source_name)
        rows.append(
            EvidenceRow(
                source_name=call.source_name,
                stance=call.stance,
                rationale=call.rationale,
                accuracy_pct=stats.accuracy_all,
                accuracy_n=stats.n_all,
                accuracy_label=SourceTracker.format_accuracy(stats),
                url=call.url,
                is_synthetic=call.is_synthetic or call.source_name in SYNTHETIC_ORDER,
            )
        )
    return Evidence(ipo_slug=ipo.slug, rows=rows)


def build_all(
    ipos: Sequence[IPO],
    calls: Sequence[SourceCall],
    tracker: SourceTracker,
) -> dict[str, Evidence]:
    by_slug: dict[str, list[SourceCall]] = {}
    for call in calls:
        by_slug.setdefault(call.ipo_slug, []).append(call)
    evidence = {ipo.slug: build_evidence(ipo, by_slug.get(ipo.slug, []), tracker) for ipo in ipos}
    log.info("evidence tables built for %d IPO(s)", len(evidence))
    return evidence


# --------------------------------------------------------------------------------------
# Consensus helpers, consumed by take.py and the report layer
# --------------------------------------------------------------------------------------


def broker_rows(evidence: Evidence) -> list[EvidenceRow]:
    """Named brokerages only — excludes Singhvi and the synthetic signals."""
    return [r for r in evidence.rows if not r.is_synthetic and r.source_name != SOURCE_SINGHVI]


def row_for(evidence: Evidence, source_name: str) -> EvidenceRow | None:
    for row in evidence.rows:
        if row.source_name == source_name:
            return row
    return None


def broker_consensus(evidence: Evidence) -> dict[str, float | int]:
    """Accuracy-weighted bullishness of the named brokerages, on a 0..1 scale.

    A firm with a proven record counts more than an unknown one, but never more than
    twice as much — this is a tilt, not a takeover.
    """
    rows = broker_rows(evidence)
    scorable = [r for r in rows if r.stance is not Stance.NO_VIEW]
    if not scorable:
        return {"n": 0, "bullish": 0, "bearish": 0, "neutral": 0, "score": 0.0}

    total_weight = 0.0
    weighted_sum = 0.0
    for row in scorable:
        weight = 1.0
        if row.accuracy_pct is not None and row.accuracy_n >= 5:
            weight = max(0.5, min(2.0, row.accuracy_pct / 50.0))
        total_weight += weight
        # polarity -1..+1 mapped onto 0..1
        weighted_sum += weight * ((row.stance.polarity + 1.0) / 2.0)

    return {
        "n": len(scorable),
        "bullish": sum(1 for r in scorable if r.stance.is_apply_type),
        "bearish": sum(1 for r in scorable if r.stance.is_avoid_type),
        "neutral": sum(1 for r in scorable if r.stance is Stance.NEUTRAL),
        "score": round(weighted_sum / total_weight, 4) if total_weight else 0.0,
    }


def consensus_sentence(evidence: Evidence) -> str:
    """Plain-English summary of the brokerage table, used in the take paragraph."""
    stats = broker_consensus(evidence)
    n = int(stats["n"])
    if n == 0:
        return "no named brokerage has published a view yet"
    bullish, bearish, neutral = int(stats["bullish"]), int(stats["bearish"]), int(stats["neutral"])
    parts = [f"{bullish} of {n} brokerages say subscribe"]
    if bearish:
        parts.append(f"{bearish} say avoid")
    if neutral:
        parts.append(f"{neutral} are neutral")
    return ", ".join(parts)
