"""What moved since the earlier run of the same trading day.

The afternoon update only earns its place if it says what *changed*. Re-reading the whole
report to spot that QIB went 4.2x → 11.8x is work the engine should have done.

Two rules hold this honest:

  * No baseline, no delta. If no earlier snapshot exists for today — first run of the day,
    first run after a deploy, an archive that never got committed — every surface omits the
    line silently. An invented "since this morning" is worse than none.
  * A number that vanished is not a fall. Sources drop out; a category that had data this
    morning and none now is a scrape gap, so it is skipped rather than reported as a move.

The baseline is the most recent *earlier* slot of the same day (see ``slots.earlier_slots``),
read from the run archive the store keeps. ``data/latest.json`` is the fallback for the very
first run after this feature ships, when the archive is still empty.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Sequence

from .. import slots
from ..config import DELTA
from ..models import IPO, IPODelta, IPOStatus
from ..store.base import Store

log = logging.getLogger(__name__)

#: Priority order for the subscription categories. QIB and Total carry the most meaning,
#: so when the line is capped they are what survives.
_SUBSCRIPTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("QIB", "qib"),
    ("Total", "total"),
    ("NII", "nii"),
    ("Retail", "retail"),
)


# --------------------------------------------------------------------------------------
# Baseline lookup
# --------------------------------------------------------------------------------------


def load_baseline(store: Store, run_date: str, slot: str) -> tuple[Optional[dict[str, Any]], str]:
    """The most recent earlier run of `run_date`. Returns (payload, that run's slot)."""
    for earlier in slots.earlier_slots(slot):
        payload = store.load_run_snapshot(run_date, earlier)
        if payload:
            return payload, earlier

    # Nothing archived yet. latest.json still holds the previous run in full, so use it when
    # it is from today and from an earlier slot — that is what makes the very first
    # afternoon update after a deploy carry a delta instead of a shrug.
    payload = store.load_latest()
    if isinstance(payload, dict) and payload.get("run_date") == run_date:
        earlier = str(payload.get("slot") or "")
        if earlier and earlier in slots.ORDER and slots.is_earlier(earlier, slot):
            return payload, earlier
    return None, ""


def _index(payload: Optional[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """slug → that IPO's row in the baseline snapshot."""
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("ipos")
    if not isinstance(rows, list):
        return {}
    return {str(row["slug"]): row for row in rows if isinstance(row, dict) and row.get("slug")}


# --------------------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------------------


def _number(value: Any) -> Optional[float]:
    """Tolerant float — a snapshot is JSON, so anything could be in there."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _multiple(value: float) -> str:
    return f"{value:.2f}x"


def _percent(value: float) -> str:
    return f"{value:+.1f}%"


def _movement(
    label: str,
    before: Optional[float],
    after: Optional[float],
    minimum: float,
    render: Callable[[float], str],
) -> Optional[str]:
    """One "X a → b" fragment, or None when there is nothing worth saying."""
    if after is None:
        return None  # lost data is a scrape gap, not a move
    if before is None:
        return f"{label} {render(after)} (new)"
    if abs(after - before) < minimum:
        return None
    return f"{label} {render(before)} → {render(after)}"


# --------------------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------------------


def _delta_for(
    ipo: IPO, previous: dict[str, Any], since: str, baseline_slot: str, *, closing: bool
) -> IPODelta:
    delta = IPODelta(ipo_slug=ipo.slug, since_label=since, baseline_slot=baseline_slot)

    # Status first — "now open" / "now closing" changes what the reader can still do today,
    # which outranks any multiple.
    was_status = str(previous.get("status") or "")
    if was_status == IPOStatus.UPCOMING.value and ipo.status is IPOStatus.OPEN:
        delta.parts.append(str(DELTA["now_open_label"]))
    if closing and not previous.get("closing_tomorrow"):
        delta.parts.append(str(DELTA["now_closing_label"]))

    previous_subscription = previous.get("subscription")
    previous_subscription = previous_subscription if isinstance(previous_subscription, dict) else {}
    minimum_x = float(DELTA["subscription_min_change_x"])
    for label, key in _SUBSCRIPTION_FIELDS:
        fragment = _movement(
            label,
            _number(previous_subscription.get(key)),
            _number(getattr(ipo.subscription, key)),
            minimum_x,
            _multiple,
        )
        if fragment:
            delta.parts.append(fragment)

    gmp = _movement(
        "GMP",
        _number(previous.get("gmp_pct")),
        _number(ipo.gmp_pct),
        float(DELTA["gmp_min_change_pct"]),
        _percent,
    )
    if gmp:
        delta.parts.append(gmp)

    # GMP sits behind QIB/Total in the priority order above but ahead of NII/Retail, so
    # re-order before capping: status → QIB → Total → GMP → NII → Retail.
    delta.parts = _prioritise(delta.parts)[: int(DELTA["max_parts"])]
    return delta


_PRIORITY: tuple[str, ...] = (
    str(DELTA["now_open_label"]),
    str(DELTA["now_closing_label"]),
    "QIB",
    "Total",
    "GMP",
    "NII",
    "Retail",
)


def _prioritise(parts: Sequence[str]) -> list[str]:
    def rank(part: str) -> int:
        for index, prefix in enumerate(_PRIORITY):
            if part.startswith(prefix):
                return index
        return len(_PRIORITY)

    return sorted(parts, key=rank)


def build_all(
    ipos: Sequence[IPO],
    baseline: Optional[dict[str, Any]],
    baseline_slot: str,
    *,
    closing_slugs: Optional[set[str]] = None,
) -> dict[str, IPODelta]:
    """slug → delta, for every IPO that actually moved. No baseline ⇒ empty dict."""
    previous_rows = _index(baseline)
    if not previous_rows:
        return {}

    since = slots.since_label(baseline_slot)
    closing_slugs = closing_slugs or set()
    deltas: dict[str, IPODelta] = {}
    for ipo in ipos:
        previous = previous_rows.get(ipo.slug)
        if previous is None:
            deltas[ipo.slug] = IPODelta(
                ipo_slug=ipo.slug, since_label=since, baseline_slot=baseline_slot, is_new=True
            )
            continue
        delta = _delta_for(
            ipo, previous, since, baseline_slot, closing=ipo.slug in closing_slugs
        )
        if delta.has_content:
            deltas[ipo.slug] = delta
    return deltas


__all__ = ["build_all", "load_baseline"]
