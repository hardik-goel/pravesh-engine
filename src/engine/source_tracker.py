"""The accuracy ledger — the thing that makes this engine honest.

Every source call is persisted. When an IPO lists, each call is resolved against the
actual listing gain:

  * apply-type call correct  if listing gain >= +5%   (THRESHOLDS.listing_gain_success_pct)
  * avoid-type call correct  if listing gain <= +5%
  * NEUTRAL / NO_VIEW        excluded entirely — a non-call cannot be right or wrong

Accuracy is reported all-time and over the last 15 resolved calls, always with n. Below
n=5 we print "insufficient history" rather than a meaningless percentage.

My own take is held to exactly the same standard (`own_accuracy`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from ..config import ACCURACY, EXPERT_SOURCES, SOURCE_GMP, SOURCE_QIB, THRESHOLDS
from ..models import (
    IPO,
    Segment,
    SourceAccuracy,
    SourceCall,
    Stance,
    Take,
    VerdictRecord,
)
from ..store.base import Store

log = logging.getLogger(__name__)

SYNTHETIC_SOURCES = {SOURCE_GMP, SOURCE_QIB}
BROKER_CONSENSUS_KEY = "__broker_consensus__"

#: Verdict bands that count as an "apply" call when grading my own take.
OWN_APPLY_VERDICTS = {"APPLY", "LISTING_GAINS"}
OWN_AVOID_VERDICTS = {"AVOID"}
#: RISKY is deliberately excluded — it is an explicit refusal to call the issue.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def grade(stance: Stance, listing_gain_pct: float) -> Optional[bool]:
    """None => this stance is not gradable."""
    bar = float(THRESHOLDS["listing_gain_success_pct"])
    if stance.is_apply_type:
        return listing_gain_pct >= bar
    if stance.is_avoid_type:
        return listing_gain_pct <= bar
    return None


class SourceTracker:
    """Owns the ledger. Reads and writes go through the injected Store."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls: list[SourceCall] = store.load_source_calls()
        self.verdicts: list[VerdictRecord] = store.load_verdicts()
        log.info("ledger loaded: %d call(s), %d verdict(s)", len(self.calls), len(self.verdicts))
        self._accuracy_cache: dict[str, SourceAccuracy] = {}

    # ---------------------------------------------------------------------------------
    # Recording
    # ---------------------------------------------------------------------------------

    def record_calls(self, calls: Sequence[SourceCall]) -> None:
        if not calls:
            return
        self.calls = Store.merge_source_calls(self.calls, list(calls))
        self._accuracy_cache.clear()
        log.info("ledger now holds %d call(s)", len(self.calls))

    def record_takes(self, takes: Iterable[Take], ipos: Sequence[IPO]) -> None:
        by_slug = {i.slug: i for i in ipos}
        incoming: list[VerdictRecord] = []
        for take in takes:
            ipo = by_slug.get(take.ipo_slug)
            if ipo is None or take.preliminary:
                continue  # a PRELIMINARY take is not a call yet
            incoming.append(
                VerdictRecord(
                    ipo_slug=take.ipo_slug,
                    ipo_name=ipo.name,
                    segment=ipo.segment,
                    verdict_key=take.verdict_key,
                    score=take.score,
                    created_at=take.created_at or _now(),
                    listing_date=ipo.listing_date.isoformat() if ipo.listing_date else None,
                    issue_price=ipo.price_band_high,
                    flags=list(take.flags),
                )
            )
        if incoming:
            self.verdicts = Store.merge_verdicts(self.verdicts, incoming)

    # ---------------------------------------------------------------------------------
    # Resolution
    # ---------------------------------------------------------------------------------

    def resolve(self, outcomes: dict[str, dict[str, Optional[float]]]) -> int:
        """Grade every open call and verdict against actual listing gains."""
        resolved_calls = 0
        stamp = _now()

        for call in self.calls:
            if call.resolved:
                continue
            outcome = outcomes.get(call.ipo_slug)
            if not outcome:
                continue
            gain = outcome.get("listing_gain_pct")
            if gain is None:
                continue
            verdict = grade(call.stance, gain)
            call.listing_gain_pct = gain
            if verdict is None:
                # Neutral/no-view: mark resolved so we stop re-checking, but never counted.
                call.resolved = True
                call.correct = None
                call.resolved_at = stamp
                continue
            call.resolved = True
            call.correct = verdict
            call.resolved_at = stamp
            resolved_calls += 1

        resolved_verdicts = 0
        for record in self.verdicts:
            if record.resolved:
                continue
            outcome = outcomes.get(record.ipo_slug)
            if not outcome:
                continue
            gain = outcome.get("listing_gain_pct")
            if gain is None:
                continue
            record.listing_price = outcome.get("listing_price")
            record.issue_price = outcome.get("issue_price") or record.issue_price
            record.listing_gain_pct = gain
            record.resolved = True
            bar = float(THRESHOLDS["listing_gain_success_pct"])
            if record.verdict_key in OWN_APPLY_VERDICTS:
                record.correct = gain >= bar
            elif record.verdict_key in OWN_AVOID_VERDICTS:
                record.correct = gain <= bar
            else:
                record.correct = None
            resolved_verdicts += 1

        if resolved_calls or resolved_verdicts:
            self._accuracy_cache.clear()
            log.info("resolved %d call(s) and %d verdict(s)", resolved_calls, resolved_verdicts)
        return resolved_calls

    # ---------------------------------------------------------------------------------
    # Accuracy
    # ---------------------------------------------------------------------------------

    def _graded(self, source_name: str) -> list[SourceCall]:
        rows = [
            c
            for c in self.calls
            if c.source_name == source_name and c.resolved and c.correct is not None
        ]
        rows.sort(key=lambda c: c.resolved_at or c.captured_at or "")
        return rows

    def accuracy(self, source_name: str) -> SourceAccuracy:
        if source_name == BROKER_CONSENSUS_KEY:
            return self.broker_consensus_accuracy()
        cached = self._accuracy_cache.get(source_name)
        if cached is not None:
            return cached
        rows = self._graded(source_name)
        window = int(ACCURACY["recent_window"])
        recent = rows[-window:]
        stats = SourceAccuracy(
            source_name=source_name,
            n_all=len(rows),
            correct_all=sum(1 for c in rows if c.correct),
            n_recent=len(recent),
            correct_recent=sum(1 for c in recent if c.correct),
            is_synthetic=source_name in SYNTHETIC_SOURCES,
        )
        self._accuracy_cache[source_name] = stats
        return stats

    def broker_consensus_accuracy(self) -> SourceAccuracy:
        """Every named brokerage pooled — calibrates the 'brokers' weight.

        The named experts are excluded: each of them calibrates their own weight, so pooling
        them in here would let one person's record move two dials at once.
        """
        rows = [
            c
            for c in self.calls
            if c.resolved
            and c.correct is not None
            and not c.is_synthetic
            and c.source_name not in SYNTHETIC_SOURCES
            and c.source_name not in EXPERT_SOURCES
        ]
        rows.sort(key=lambda c: c.resolved_at or c.captured_at or "")
        window = int(ACCURACY["recent_window"])
        recent = rows[-window:]
        return SourceAccuracy(
            source_name="Broker consensus",
            n_all=len(rows),
            correct_all=sum(1 for c in rows if c.correct),
            n_recent=len(recent),
            correct_recent=sum(1 for c in recent if c.correct),
        )

    @staticmethod
    def format_accuracy(stats: SourceAccuracy) -> str:
        """Display string. Below n=5 we refuse to show a percentage."""
        minimum = int(ACCURACY["min_n_for_percentage"])
        if stats.n_all < minimum:
            return f"{ACCURACY['insufficient_label']} (n={stats.n_all})"
        return f"{stats.accuracy_all:.0f}% (n={stats.n_all})"

    def source_names(self) -> list[str]:
        return sorted({c.source_name for c in self.calls})

    def leaderboard(self) -> list[SourceAccuracy]:
        """All sources with enough history, best first. Ties broken by sample size."""
        minimum = int(ACCURACY["leaderboard_min_n"])
        rows = [self.accuracy(name) for name in self.source_names()]
        qualified = [r for r in rows if r.n_all >= minimum and r.accuracy_all is not None]
        qualified.sort(key=lambda r: (r.accuracy_all or 0.0, r.n_all), reverse=True)
        return qualified

    def top_leaderboard(self) -> list[SourceAccuracy]:
        return self.leaderboard()[: int(ACCURACY["leaderboard_size"])]

    # ---------------------------------------------------------------------------------
    # My own record — same standard, no exceptions
    # ---------------------------------------------------------------------------------

    def own_accuracy(self) -> dict[str, Any]:
        graded = [v for v in self.verdicts if v.resolved and v.correct is not None]
        graded.sort(key=lambda v: v.created_at or "")
        window = int(ACCURACY["recent_window"])
        recent = graded[-window:]
        n_all = len(graded)
        n_recent = len(recent)
        minimum = int(ACCURACY["min_n_for_percentage"])
        accuracy_all = round(100.0 * sum(1 for v in graded if v.correct) / n_all, 1) if n_all else None
        accuracy_recent = (
            round(100.0 * sum(1 for v in recent if v.correct) / n_recent, 1) if n_recent else None
        )
        label = (
            f"{ACCURACY['insufficient_label']} (n={n_all})"
            if n_all < minimum
            else f"{accuracy_all:.0f}% (n={n_all})"
        )
        return {
            "n_all": n_all,
            "accuracy_all": accuracy_all,
            "n_recent": n_recent,
            "accuracy_recent": accuracy_recent,
            "label": label,
        }

    def history(self, limit: int = 60) -> list[VerdictRecord]:
        """Resolved verdicts, newest first — powers the web tab's history view."""
        rows = [v for v in self.verdicts if v.resolved]
        rows.sort(key=lambda v: v.listing_date or v.created_at or "", reverse=True)
        return rows[:limit]

    def calls_for(self, ipo_slug: str) -> list[SourceCall]:
        return [c for c in self.calls if c.ipo_slug == ipo_slug]

    def calls_for_universe(self, ipos: Sequence[IPO]) -> list[SourceCall]:
        """Ledger view restricted to the IPOs being reported on today.

        Reading from the ledger (not just this run's fetch) means a source that answered
        yesterday but is unreachable today still shows up in the evidence table.
        """
        slugs = {i.slug for i in ipos}
        return [c for c in self.calls if c.ipo_slug in slugs]

    # ---------------------------------------------------------------------------------
    # Persistence
    # ---------------------------------------------------------------------------------

    def flush(self) -> None:
        self.store.save_source_calls(self.calls)
        self.store.save_verdicts(self.verdicts)
        log.info("ledger persisted via %s store", self.store.name)


def segment_of(calls: Sequence[SourceCall]) -> Segment:
    """Convenience for callers that need a segment from a call bundle."""
    return calls[0].segment if calls else Segment.MAINBOARD
