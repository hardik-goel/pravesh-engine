"""Which of the day's runs this is.

An open IPO is a moving target: QIB can go from 4x at the open to 12x by mid-afternoon,
and GMP moves with it. So the same weekday can produce more than one report, and every
surface has to say which one it is looking at — a subscription multiple with no timestamp
is a number you cannot act on.

The slot arrives as ``PRAVESH_SLOT`` (set by whoever dispatched the run — the backend
trigger, the workflow_dispatch input, or ``--slot``). An unknown or missing value degrades
to ``manual``: a mislabelled report is still worth sending, a crashed one is not.

Slot names, labels and their chronological order all live in ``config.RUN_SLOTS``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from .clock import now_market
from .config import DEFAULT_SLOT, MARKET, RUN_SLOTS, SLOT_ENV, SLOT_HEADLINE

log = logging.getLogger(__name__)

#: Declaration order in config is chronological order.
ORDER: list[str] = list(RUN_SLOTS)


def normalise(raw: Optional[str]) -> str:
    """A known slot key, always. Anything unrecognised falls back to the default."""
    key = (raw or "").strip().lower()
    if not key:
        return DEFAULT_SLOT
    if key in RUN_SLOTS:
        return key
    log.warning("unknown run slot %r — labelling this run %r instead", raw, DEFAULT_SLOT)
    return DEFAULT_SLOT


def current() -> str:
    """The slot this process was dispatched for."""
    return normalise(os.getenv(SLOT_ENV))


def label(slot: str) -> str:
    """Human label — "afternoon update"."""
    return str(RUN_SLOTS[normalise(slot)]["label"])


def since_label(slot: str) -> str:
    """How a delta measured *against* this slot reads — "since this morning"."""
    return str(RUN_SLOTS[normalise(slot)]["since"])


def order_index(slot: str) -> int:
    return ORDER.index(normalise(slot))


def is_earlier(candidate: str, slot: str) -> bool:
    return order_index(candidate) < order_index(slot)


def earlier_slots(slot: str) -> list[str]:
    """Every slot that precedes this one, most recent first — delta baseline lookup order."""
    return list(reversed(ORDER[: order_index(slot)]))


def _as_datetime(when: Optional[datetime | str]) -> datetime:
    if isinstance(when, datetime):
        return when
    if isinstance(when, str) and when:
        try:
            return datetime.fromisoformat(when)
        except ValueError:
            log.warning("could not read run timestamp %r — stamping with the current time", when)
    return now_market()


def headline(slot: str, when: Optional[datetime | str] = None) -> str:
    """"03 Aug · 15:04 IST · afternoon update" — the run's identity in one line.

    Used verbatim in the Telegram header, the email subject and header band, and
    ``latest.json``, so all three agree about which run the reader is holding.
    """
    moment = _as_datetime(when)
    return str(SLOT_HEADLINE["template"]).format(
        date=moment.strftime(str(SLOT_HEADLINE["date_format"])),
        time=moment.strftime(str(SLOT_HEADLINE["time_format"])),
        tz=MARKET["tz_label"],
        label=label(slot),
    )


__all__ = [
    "ORDER",
    "current",
    "earlier_slots",
    "headline",
    "is_earlier",
    "label",
    "normalise",
    "order_index",
    "since_label",
]
