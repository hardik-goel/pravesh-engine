"""Market-clock helpers.

The timezone comes from config.MARKET so a white-label deployment on another exchange
changes one line, not every date call in the codebase.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo

from .config import MARKET

_FALLBACK_OFFSETS: dict[str, timedelta] = {
    "Asia/Kolkata": timedelta(hours=5, minutes=30),
}


def market_tz() -> tzinfo:
    name = str(MARKET["timezone"])
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — tzdata missing on bare runners
        return timezone(_FALLBACK_OFFSETS.get(name, timedelta(0)), name)


def now_market() -> datetime:
    return datetime.now(market_tz())


def today_market() -> date:
    return now_market().date()


def stamp(fmt: str = "%d %b %Y, %H:%M", when: datetime | str | None = None) -> str:
    """Human market-time stamp. Pass `when` to stamp a captured moment, not "now".

    Every surface of a single run has to agree on when that run happened, so the timestamp
    is captured once and handed around rather than re-read per caller.
    """
    moment = when if isinstance(when, datetime) else None
    if moment is None and isinstance(when, str) and when:
        try:
            moment = datetime.fromisoformat(when)
        except ValueError:
            moment = None
    return f"{(moment or now_market()).strftime(fmt)} {MARKET['tz_label']}"


def human_date(value: date | None, fmt: str = "%d %b") -> str:
    return value.strftime(fmt) if value else "—"
