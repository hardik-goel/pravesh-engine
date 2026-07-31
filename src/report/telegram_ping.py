"""Telegram ping — reuses the Trinetra bot token and channel.

One compact message, ≤4096 chars, no tables. It is a nudge, not the report:

  header (date + counts)
  ⚡ NAME (SME) — my-take one-liner — closes 12 Aug
    ⚠ Anil Singhvi says AVOID
  ...
  Full report in your inbox.
"""

from __future__ import annotations

import html
import logging
import os
from datetime import date
from typing import Optional

import requests

from ..clock import human_date, today_market
from ..config import BRAND_NAME, HTTP, TELEGRAM
from ..models import RunResult

log = logging.getLogger(__name__)

TRUNCATION_NOTE = "\n…trimmed. "


def _e(value: object) -> str:
    return html.escape(str(value), quote=False)


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
    lines = [
        f"<b>{_e(BRAND_NAME)}</b> · {_e(date_label)}",
        f"{len(result.open_ipos)} open · {len(closing)} closing soon · "
        f"{len(result.upcoming_ipos)} upcoming",
        "",
    ]

    for ipo in sorted(result.open_ipos, key=lambda i: (i.close_date or today, i.name)):
        take = result.takes.get(ipo.slug)
        emoji = take.verdict_emoji if take else "•"
        one_liner = take.one_liner if take else "no take yet"
        prefix = str(TELEGRAM["closing_tomorrow_prefix"]) if ipo.slug in closing else ""
        sme = " (SME)" if ipo.is_sme else ""
        lines.append(
            f"{prefix}{emoji} <b>{_e(ipo.name)}</b>{sme} — {_e(one_liner)} — "
            f"closes {_e(human_date(ipo.close_date))}"
        )
        if take and take.flags:
            for flag in take.flags:
                lines.append(f"   <b>{_e(flag)}</b>")

    upcoming = [i for i in result.upcoming_ipos if i.open_date and (i.open_date - today).days <= 7]
    if upcoming:
        lines.append("")
        lines.append("<i>Opening this week (preliminary):</i>")
        for ipo in sorted(upcoming, key=lambda i: (i.open_date or today, i.name)):
            sme = " (SME)" if ipo.is_sme else ""
            lines.append(f"• {_e(ipo.name)}{sme} — opens {_e(human_date(ipo.open_date))}")

    if result.sources_failed:
        lines.append("")
        lines.append(f"⚠ Degraded this run: {_e('; '.join(result.sources_failed))}")

    lines.append("")
    lines.append(str(TELEGRAM["footer"]))

    message = "\n".join(lines)
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
