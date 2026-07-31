"""Default store: plain JSON files under data/, committed back by the workflow.

Zero infrastructure, fully diffable, and the raw GitHub URL of data/latest.json is the
contract the web tab reads. Writes are atomic (temp file + replace) so a killed run never
leaves a half-written ledger.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from ..config import JSON_STORE
from ..models import SourceCall, VerdictRecord
from .base import Store

log = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class JsonStore(Store):
    name = "json"

    def __init__(self, data_dir: str | os.PathLike[str] | None = None) -> None:
        base = Path(data_dir) if data_dir else _repo_root() / str(JSON_STORE["data_dir"])
        self.data_dir = base
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.source_calls_path = self.data_dir / str(JSON_STORE["source_calls_file"])
        self.verdicts_path = self.data_dir / str(JSON_STORE["verdicts_file"])
        self.latest_path = self.data_dir / str(JSON_STORE["latest_file"])

    # -- io ----------------------------------------------------------------------------

    def _read(self, path: Path) -> Any:
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("could not read %s (%s); treating as empty", path.name, exc)
            return []

    def _write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        )
        try:
            with handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=False)
                handle.write("\n")
            os.replace(handle.name, path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise
        log.info("wrote %s", path.relative_to(_repo_root()) if path.is_relative_to(_repo_root()) else path)

    # -- Store contract ----------------------------------------------------------------

    def load_source_calls(self) -> list[SourceCall]:
        raw = self._read(self.source_calls_path)
        rows = raw.get("calls", []) if isinstance(raw, dict) else raw
        calls: list[SourceCall] = []
        for row in rows:
            try:
                calls.append(SourceCall.from_dict(row))
            except (KeyError, ValueError) as exc:
                log.warning("skipping malformed source call row: %s", exc)
        return calls

    def save_source_calls(self, calls: list[SourceCall]) -> None:
        payload = [c.to_dict() for c in calls]
        payload.sort(key=lambda row: (row.get("captured_at") or "", row["ipo_slug"], row["source_name"]))
        self._write(self.source_calls_path, payload)

    def load_verdicts(self) -> list[VerdictRecord]:
        raw = self._read(self.verdicts_path)
        rows = raw.get("verdicts", []) if isinstance(raw, dict) else raw
        verdicts: list[VerdictRecord] = []
        for row in rows:
            try:
                verdicts.append(VerdictRecord.from_dict(row))
            except (KeyError, ValueError) as exc:
                log.warning("skipping malformed verdict row: %s", exc)
        return verdicts

    def save_verdicts(self, verdicts: list[VerdictRecord]) -> None:
        payload = [v.to_dict() for v in verdicts]
        payload.sort(key=lambda row: (row.get("created_at") or "", row["ipo_slug"]))
        self._write(self.verdicts_path, payload)

    def save_latest(self, payload: dict[str, Any]) -> None:
        self._write(self.latest_path, payload)
