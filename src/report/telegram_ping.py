"""Telegram ping — reuses the Trinetra bot token and channel.

One compact message, ≤4096 chars, no tables. It is a nudge, not the report — but the
nudge carries the verdict, because a one-liner you have to open your inbox to decode is
not a nudge:

  header (date + counts)
  ⚡ NAME (SME) — APPLY · QIB 12.4x · GMP +18% · Singhvi: Apply — closes 12 Aug
    ⚠ Anil Singhvi says AVOID
  ⚪ OTHER NAME — PRELIMINARY · leaning AVOID · GMP +2% — closes 14 Aug
  ...
  Full report in your inbox.

IPOs are ordered most-actionable first (closing tomorrow, then by conviction), so when
there are too many to fit, what gets dropped is what mattered least today.
"""

from __future__ import annotations

import html
import logging
import os
from datetime import date
from typing import Optional

import requests

from ..clock import human_date, today_market
from ..config import BRAND_NAME, HTTP, TELEGRAM, VERDICT_BANDS
from ..models import IPO, RunResult, Take

log = logging.getLogger(__name__)

TRUNCATION_NOTE = "\n…trimmed. "
MORE_NOTE = "…+{n} more — see email/app"
NO_TAKE_REASON = "nothing has landed yet"

#: Conviction order, straight off the scoring bands: APPLY, LISTING_GAINS, RISKY, AVOID.
_BAND_ORDER: list[str] = [str(band["key"]) for band in VERDICT_BANDS]


def _e(value: object) -> str:
    return html.escape(str(value), quote=False)


def _verdict_word(take: Optional[Take]) -> str:
    """APPLY / LISTING GAINS ONLY / RISKY / AVOID / PRELIMINARY — the headline, nothing else.

    Band labels carry their own qualifier after an em dash ("RISKY — high risk appetite
    only", "PRELIMINARY — APPLY"); the ping wants only the word before it.
    """
    if take is None:
        return "NO TAKE"
    word = str(take.verdict_label).split("—")[0].strip()
    return word or str(take.verdict_label)


def _reason(take: Optional[Take]) -> str:
    """The evidence half of the take's one-liner — "QIB 12.4x · GMP +18% · Singhvi: Apply".

    one_liner is built as "LABEL (detail)", so the detail is what we want; the label is
    already rendered as the verdict word. A preliminary take leads with the band it is
    provisionally sitting in — "PRELIMINARY" on its own tells the reader nothing.
    """
    if take is None or not take.has_score:
        return NO_TAKE_REASON
    text = (take.one_liner or "").strip()
    detail = ""
    if text.endswith(")") and "(" in text:
        detail = text[text.index("(") + 1 : -1].strip()
    detail = detail or text or NO_TAKE_REASON

    if take.preliminary:
        parts = [p.strip() for p in str(take.verdict_label).split("—")]
        if len(parts) > 1 and parts[1]:
            return f"leaning {parts[1]} · {detail}"
    return detail


def _conviction(take: Optional[Take]) -> int:
    """Lower sorts first. A preliminary read ranks below every scored band."""
    if take is None:
        return len(_BAND_ORDER) + 1
    if take.preliminary:
        return len(_BAND_ORDER)
    return _BAND_ORDER.index(take.verdict_key) if take.verdict_key in _BAND_ORDER else len(_BAND_ORDER)


def _ipo_block(ipo: IPO, take: Optional[Take], urgent: bool) -> list[str]:
    """One IPO's lines: the verdict line, plus any red-flag banner underneath it."""
    prefix = str(TELEGRAM["closing_tomorrow_prefix"]) if urgent else ""
    sme = " (SME)" if ipo.is_sme else ""
    emoji = take.verdict_emoji if take else "•"
    lines = [
        f"{prefix}{emoji} <b>{_e(ipo.name)}</b>{sme} — <b>{_e(_verdict_word(take))}</b> · "
        f"{_e(_reason(take))} — closes {_e(human_date(ipo.close_date))}"
    ]
    if take and take.flags:
        lines.extend(f"   <b>{_e(flag)}</b>" for flag in take.flags)
    return lines


def build_message(result: RunResult, today: Optional[date] = None) -> str:
    """The exact text that gets sent. Pure function — used by --dry-run too."""
    today = today or today_market()
    date_label = human_date(today, "%d %b")

    if result.is_quiet:
        lines = [
            f"<b>{_e(BRAND_NAME)}</b> · {_e(date_label)}",
            "All quiet — no open, closing or listing IPOs today.",
        ]
        if result.sources_failed:
            lines.append(f"⚠ Degraded: {_e('; '.join(result.sources_failed))}")
        lines.append(str(TELEGRAM["footer"]))
        return "\n".join(lines)

    closing = {i.slug for i in result.closing_tomorrow(today)}
    head = [
        f"<b>{_e(BRAND_NAME)}</b> · {_e(date_label)}",
        f"{len(result.open_ipos)} open · {len(closing)} closing soon · "
        f"{len(result.upcoming_ipos)} upcoming",
        "",
    ]

    # Most actionable first: closing tomorrow, then highest conviction, then earliest close.
    ordered = sorted(
        result.open_ipos,
        key=lambda i: (
            i.slug not in closing,
            _conviction(result.takes.get(i.slug)),
            i.close_date or today,
            i.name,
        ),
    )
    blocks = [
        _ipo_block(ipo, result.takes.get(ipo.slug), ipo.slug in closing) for ipo in ordered
    ]

    upcoming_lines: list[str] = []
    upcoming = [i for i in result.upcoming_ipos if i.open_date and (i.open_date - today).days <= 7]
    if upcoming:
        upcoming_lines.append("")
        upcoming_lines.append("<i>Opening this week (preliminary):</i>")
        for ipo in sorted(upcoming, key=lambda i: (i.open_date or today, i.name)):
            sme = " (SME)" if ipo.is_sme else ""
            upcoming_lines.append(f"• {_e(ipo.name)}{sme} — opens {_e(human_date(ipo.open_date))}")

    tail: list[str] = []
    if result.sources_failed:
        tail.append("")
        tail.append(f"⚠ Degraded this run: {_e('; '.join(result.sources_failed))}")
    tail.append("")
    tail.append(str(TELEGRAM["footer"]))

    return _assemble(head, blocks, upcoming_lines, tail)


def _assemble(
    head: list[str], blocks: list[list[str]], upcoming: list[str], tail: list[str]
) -> str:
    """Fit as many IPO blocks as the budget allows, then say how many were left out.

    Blocks arrive most-important-first, so dropping from the end drops the least useful
    lines. The "opening this week" section goes first — a preliminary name is never worth
    more than an IPO you can still apply to today.
    """
    soft = int(TELEGRAM.get("soft_max_chars", TELEGRAM["max_chars"]))

    def render(kept: int, note: Optional[str], with_upcoming: bool) -> str:
        lines = list(head)
        for block in blocks[:kept]:
            lines.extend(block)
        if note:
            lines.append("")
            lines.append(note)
        if with_upcoming:
            lines.extend(upcoming)
        lines.extend(tail)
        return "\n".join(lines)

    full = render(len(blocks), None, bool(upcoming))
    if len(full) <= soft:
        return full

    if upcoming:
        without_upcoming = render(len(blocks), None, False)
        if len(without_upcoming) <= soft:
            return without_upcoming

    # Still too long: drop IPO blocks from the least urgent end until it fits.
    for kept in range(len(blocks) - 1, 0, -1):
        note = MORE_NOTE.format(n=len(blocks) - kept)
        candidate = render(kept, note, False)
        if len(candidate) <= soft:
            return candidate

    # Pathological single block (a wall of red flags): fall back to a hard character cut.
    message = render(1, MORE_NOTE.format(n=max(0, len(blocks) - 1)), False)
    limit = int(TELEGRAM["max_chars"])
    if len(message) > limit:
        keep = limit - len(TRUNCATION_NOTE) - len(str(TELEGRAM["footer"])) - 1
        message = message[:keep].rsplit("\n", 1)[0] + TRUNCATION_NOTE + str(TELEGRAM["footer"])
    return message


def send(message: str) -> bool:
    """POST to the shared Trinetra bot. Returns False instead of raising."""
    token = os.getenv(str(TELEGRAM["token_env"]), "").strip()
    chat_id = os.getenv(str(TELEGRAM["chat_env"]), "").strip()
    if not token or not chat_id:
        log.error(
            "telegram not sent: %s / %s must both be set", TELEGRAM["token_env"], TELEGRAM["chat_env"]
        )
        return False
    url = f"{TELEGRAM['api_base']}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": TELEGRAM["parse_mode"],
        "disable_web_page_preview": TELEGRAM["disable_web_page_preview"],
    }
    try:
        response = requests.post(url, json=payload, timeout=float(HTTP["timeout_seconds"]))
        if response.status_code != 200:
            log.error("telegram API returned %s: %s", response.status_code, response.text[:300])
            return False
        log.info("telegram ping sent (%d chars)", len(message))
        return True
    except Exception as exc:  # noqa: BLE001 — delivery failure must not kill the run
        log.error("telegram send failed: %s", exc)
        return False


__all__ = ["build_message", "send"]
