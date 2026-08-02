"""Fold the committed ledger back into the freshly generated one, before the bot commits.

The daily workflow never merges data/ files with git — they are regenerated in full each
run, so a line-by-line merge is meaningless and only produces conflicts. Instead the
workflow resets onto origin, drops this run's files back on top, and calls this script so
that "our version wins" is *always* the correct outcome:

  * data/latest.json        — a full snapshot of today. This run is authoritative, no merge.
  * data/source_calls.json  — accumulates history, so the file we commit must be a superset
  * data/verdicts.json        of whatever is already on the branch. Both are re-merged here
                              through the exact upsert the engine uses (Store.merge_*), which
                              keeps resolved rows frozen and refreshes unresolved ones.

Normally this is a no-op: the run started from the same commit we are pushing onto, so the
engine already loaded and merged that history in memory. It matters when the branch moved
underneath a long run, or when a push race forces the workflow to replay.

    python scripts/reconcile_data.py --ref origin/master
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.models import SourceCall, VerdictRecord  # noqa: E402
from src.store.base import Store  # noqa: E402
from src.store.json_store import JsonStore  # noqa: E402

log = logging.getLogger("pravesh.reconcile")


def _blob_rows(ref: str, path: str) -> list[dict[str, Any]]:
    """Rows of a JSON file as it exists at `ref`. Missing or unreadable => empty."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.info("%s not present at %s — nothing to fold in", path, ref)
        return []
    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        log.warning("%s at %s is not valid JSON (%s); ignoring it", path, ref, exc)
        return []
    if isinstance(raw, dict):
        raw = raw.get("calls") or raw.get("verdicts") or []
    return [row for row in raw if isinstance(row, dict)]


def _repo_rel(path: Path) -> str:
    """git show wants a repo-relative path, always with forward slashes."""
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _parse(rows: list[dict[str, Any]], factory: Any, label: str) -> list[Any]:
    parsed = []
    for row in rows:
        try:
            parsed.append(factory(row))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed %s row from the branch: %s", label, exc)
    return parsed


def reconcile(ref: str, data_dir: str | None = None) -> tuple[int, int]:
    store = JsonStore(data_dir)

    remote_calls = _parse(
        _blob_rows(ref, _repo_rel(store.source_calls_path)),
        SourceCall.from_dict,
        "source call",
    )
    merged_calls = Store.merge_source_calls(remote_calls, store.load_source_calls())
    store.save_source_calls(merged_calls)

    remote_verdicts = _parse(
        _blob_rows(ref, _repo_rel(store.verdicts_path)),
        VerdictRecord.from_dict,
        "verdict",
    )
    merged_verdicts = Store.merge_verdicts(remote_verdicts, store.load_verdicts())
    store.save_verdicts(merged_verdicts)

    log.info(
        "reconciled against %s: %d call(s) (%d on branch), %d verdict(s) (%d on branch)",
        ref,
        len(merged_calls),
        len(remote_calls),
        len(merged_verdicts),
        len(remote_verdicts),
    )
    return len(merged_calls), len(merged_verdicts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reconcile-data", description=__doc__)
    parser.add_argument("--ref", default="origin/master", help="git ref holding the committed ledger")
    parser.add_argument("--data-dir", default=None, help="override the data directory")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    reconcile(args.ref, args.data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
