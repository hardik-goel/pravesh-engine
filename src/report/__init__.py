"""Delivery surfaces: email (full report) and Telegram (nudge)."""

from . import telegram_ping
from .email_builder import (
    build_html,
    build_quiet_html,
    build_subject,
    send_email,
    send_failure_notice,
    write_preview,
)

__all__ = [
    "build_html",
    "build_quiet_html",
    "build_subject",
    "send_email",
    "send_failure_notice",
    "telegram_ping",
    "write_preview",
]
