"""Supabase store — the drop-in upgrade from JSON files.

Wired and ready. Flip it on with `PRAVESH_STORE=supabase` (or change STORE_BACKEND in
config.py) and set SUPABASE_URL + SUPABASE_SERVICE_KEY. Table names live in config.

Expected schema (SQL to create it is in the README):

  pravesh_source_calls(ipo_slug text, source_name text, ... , primary key (ipo_slug, source_name))
  pravesh_verdicts(ipo_slug text primary key, ...)
  pravesh_latest(key text primary key, payload jsonb, updated_at timestamptz)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from ..config import SUPABASE
from ..models import SourceCall, VerdictRecord
from .base import Store

log = logging.getLogger(__name__)

PAGE_SIZE = 1000


class SupabaseStore(Store):
    name = "supabase"

    def __init__(self) -> None:
        url = os.getenv(str(SUPABASE["url_env"]), "").strip()
        key = os.getenv(str(SUPABASE["key_env"]), "").strip()
        if not url or not key:
            raise RuntimeError(
                f"{SUPABASE['url_env']} and {SUPABASE['key_env']} must both be set for the Supabase store"
            )
        try:
            from supabase import create_client  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("supabase-py is not installed (pip install supabase)") from exc

        self.client = create_client(url, key)
        self.source_calls_table = str(SUPABASE["source_calls_table"])
        self.verdicts_table = str(SUPABASE["verdicts_table"])
        self.latest_table = str(SUPABASE["latest_table"])
        self.latest_row_key = str(SUPABASE["latest_row_key"])
        log.info("SupabaseStore ready (%s)", url.split("//")[-1])

    # -- io ----------------------------------------------------------------------------

    def _select_all(self, table: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = (
                self.client.table(table).select("*").range(offset, offset + PAGE_SIZE - 1).execute()
            )
            batch = list(response.data or [])
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                return rows
            offset += PAGE_SIZE

    def _upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        if not rows:
            return
        for start in range(0, len(rows), PAGE_SIZE):
            chunk = rows[start : start + PAGE_SIZE]
            self.client.table(table).upsert(chunk, on_conflict=on_conflict).execute()
        log.info("upserted %d row(s) into %s", len(rows), table)

    # -- Store contract ----------------------------------------------------------------

    def load_source_calls(self) -> list[SourceCall]:
        calls: list[SourceCall] = []
        for row in self._select_all(self.source_calls_table):
            try:
                calls.append(SourceCall.from_dict(row))
            except (KeyError, ValueError) as exc:
                log.warning("skipping malformed supabase source call: %s", exc)
        return calls

    def save_source_calls(self, calls: list[SourceCall]) -> None:
        self._upsert(
            self.source_calls_table,
            [c.to_dict() for c in calls],
            on_conflict="ipo_slug,source_name",
        )

    def load_verdicts(self) -> list[VerdictRecord]:
        verdicts: list[VerdictRecord] = []
        for row in self._select_all(self.verdicts_table):
            try:
                verdicts.append(VerdictRecord.from_dict(row))
            except (KeyError, ValueError) as exc:
                log.warning("skipping malformed supabase verdict: %s", exc)
        return verdicts

    def save_verdicts(self, verdicts: list[VerdictRecord]) -> None:
        self._upsert(self.verdicts_table, [v.to_dict() for v in verdicts], on_conflict="ipo_slug")

    def save_latest(self, payload: dict[str, Any]) -> None:
        row = {
            "key": self.latest_row_key,
            "payload": payload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.client.table(self.latest_table).upsert(row, on_conflict="key").execute()
        log.info("wrote latest snapshot to %s", self.latest_table)
