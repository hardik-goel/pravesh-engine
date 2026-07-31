"""Synthetic source "QIB signal", derived from live subscription data.

No network access of its own — it reads what chittorgarh.py already attached. Emits only
once the IPO is open AND QIB numbers exist; institutional demand on day zero is noise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from ..config import SOURCE_QIB, THRESHOLDS
from ..models import IPO, IPOStatus, Segment, SourceCall, Stance
from .base import Source


class QIBSignalSource(Source):
    """QIB >= 10x implies Apply; 3-10x Neutral; below 3x implies Avoid."""

    name = "qib_signal"
    segment_coverage = (Segment.MAINBOARD, Segment.SME)

    def fetch(self, ipos: Sequence[IPO]) -> list[SourceCall]:
        now = datetime.now(timezone.utc).isoformat()
        apply_x = float(THRESHOLDS["qib_apply_x"])
        neutral_x = float(THRESHOLDS["qib_neutral_x"])
        calls: list[SourceCall] = []

        for ipo in self.relevant(ipos):
            if ipo.status not in (IPOStatus.OPEN, IPOStatus.CLOSED):
                continue
            qib = ipo.subscription.qib
            if qib is None:
                continue
            if qib >= apply_x:
                stance = Stance.APPLY
            elif qib >= neutral_x:
                stance = Stance.NEUTRAL
            else:
                stance = Stance.AVOID
            calls.append(
                SourceCall(
                    source_name=SOURCE_QIB,
                    ipo_slug=ipo.slug,
                    ipo_name=ipo.name,
                    stance=stance,
                    rationale=self._rationale(ipo, qib, apply_x, neutral_x),
                    url=ipo.detail_url,
                    captured_at=now,
                    segment=ipo.segment,
                    is_synthetic=True,
                )
            )

        if not calls:
            self.log.info("qib_signal: no open IPO has published QIB numbers yet")
        return calls

    @staticmethod
    def _rationale(ipo: IPO, qib: float, apply_x: float, neutral_x: float) -> str:
        book = ipo.subscription
        parts = [f"QIB book at {qib:.2f}x"]
        if book.total is not None:
            parts.append(f"overall {book.total:.2f}x")
        if qib >= apply_x:
            verdict = f"institutional demand clears the {apply_x:.0f}x bar"
        elif qib >= neutral_x:
            verdict = f"institutions are in but short of the {apply_x:.0f}x bar"
        else:
            verdict = f"institutional demand below {neutral_x:.0f}x — thin"
        return f"{' · '.join(parts)}; {verdict}."
