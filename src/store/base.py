"""Store ABC — the persistence seam.

Two implementations ship: JsonStore (default, files under data/) and SupabaseStore.
Switching is one line in config.py (or the PRAVESH_STORE env var). Nothing above this
layer knows which one is in play.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from ..models import SourceCall, VerdictRecord

log = logging.getLogger(__name__)


class Store(ABC):
    """Append-and-upsert semantics: a call is unique on (ipo_slug, source_name)."""

    name: str = "store"

    # -- source calls ------------------------------------------------------------------

    @abstractmethod
    def load_source_calls(self) -> list[SourceCall]:
        """Every call ever recorded, oldest first."""

    @abstractmethod
    def save_source_calls(self, calls: list[SourceCall]) -> None:
        """Persist the full ledger (already merged by the caller)."""

    # -- verdicts ----------------------------------------------------------------------

    @abstractmethod
    def load_verdicts(self) -> list[VerdictRecord]:
        """My-take history + resolved listing outcomes."""

    @abstractmethod
    def save_verdicts(self, verdicts: list[VerdictRecord]) -> None: ...

    # -- web contract ------------------------------------------------------------------

    @abstractmethod
    def save_latest(self, payload: dict[str, Any]) -> None:
        """Machine-readable snapshot consumed by the Pravesh web tab."""

    # -- merge helpers (shared by all backends) ----------------------------------------

    @staticmethod
    def merge_source_calls(existing: list[SourceCall], incoming: list[SourceCall]) -> list[SourceCall]:
        """Upsert on (ipo_slug, source_name), preserving any resolution already recorded."""
        by_key: dict[str, SourceCall] = {c.key: c for c in existing}
        for call in incoming:
            previous = by_key.get(call.key)
            if previous is None:
                by_key[call.key] = call
                continue
            # Keep the original capture timestamp and any resolved outcome; refresh the view.
            call.captured_at = previous.captured_at or call.captured_at
            if previous.resolved:
                call.resolved = True
                call.correct = previous.correct
                call.listing_gain_pct = previous.listing_gain_pct
                call.resolved_at = previous.resolved_at
                # A resolved call is frozen — a source cannot rewrite history.
                by_key[call.key] = previous
            else:
                by_key[call.key] = call
        return list(by_key.values())

    @staticmethod
    def merge_verdicts(existing: list[VerdictRecord], incoming: list[VerdictRecord]) -> list[VerdictRecord]:
        by_slug: dict[str, VerdictRecord] = {v.ipo_slug: v for v in existing}
        for verdict in incoming:
            previous = by_slug.get(verdict.ipo_slug)
            if previous is not None and previous.resolved:
                continue  # frozen once the listing has happened
            if previous is not None:
                verdict.created_at = previous.created_at or verdict.created_at
            by_slug[verdict.ipo_slug] = verdict
        return list(by_slug.values())


def build_store(backend: str | None = None) -> Store:
    """Factory. One-line switch between JSON files and Supabase."""
    from ..config import STORE_BACKEND
    from .json_store import JsonStore
    from .supabase_store import SupabaseStore

    key = (backend or STORE_BACKEND).strip().lower()
    if key == "supabase":
        try:
            return SupabaseStore()
        except Exception as exc:  # noqa: BLE001 — never lose a run over a store misconfig
            log.error("SupabaseStore unavailable (%s); falling back to JsonStore", exc)
            return JsonStore()
    return JsonStore()
