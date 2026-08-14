"""Should the scheduled fallback actually send a report for this slot?

    python scripts/slot_guard.py --slot morning

The backend trigger (trinetra-backend, lib/praveshTrigger.js) is the primary schedule, and
it only fires while the Render instance happens to be awake inside the slot window. A free
instance sleeps after ~15 minutes idle and its own keep-alive cannot wake it, so on any day
nothing external hits the backend between 09:00 and 10:30 IST, no morning report exists —
which is what happened on 2026-08-12 and 2026-08-14.

GitHub's cron then runs late rather than never: it drifts by hours but it does arrive. So
the workflow carries a cron per slot, timed AFTER that slot's window closes, and this guard
decides whether it has anything to do. It answers one question: has a report already gone
out today at or after this slot's window opened?

    yes -> skip, and say which run covered it. The trigger did its job, or the user ran it
           by hand; a second report to the same inbox is noise.
    no  -> run. Late is worth sending; silent is not.

Any archived run counts, whatever its slot label, because a manual run at 09:33 covers the
morning just as well as one labelled `morning` does. Prints `run=true|false` to
$GITHUB_OUTPUT when it exists, and the reasoning to stdout either way.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.clock import market_tz, today_market  # noqa: E402
from src.config import SLOT_WINDOW_START  # noqa: E402

HISTORY_DIR = Path("data/history")


def window_start(slot: str) -> datetime:
    """When this slot's window opens today, in market time."""
    hh, mm = (int(part) for part in SLOT_WINDOW_START[slot].split(":"))
    return datetime.combine(today_market(), time(hh, mm), tzinfo=market_tz())


def _generated_at(path: Path) -> Optional[datetime]:
    """When the archived run happened, market time. None if the file is unreadable."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    stamp = payload.get("generated_at_ist") or payload.get("generated_at")
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:  # a naive stamp is market time by convention
        moment = moment.replace(tzinfo=market_tz())
    return moment.astimezone(market_tz())


def covering_run(slot: str, history_dir: Path = HISTORY_DIR) -> Optional[tuple[Path, datetime]]:
    """The earliest of today's archived runs that already covers this slot, if any."""
    opened = window_start(slot)
    found: list[tuple[Path, datetime]] = []
    for path in sorted(history_dir.glob(f"{today_market().isoformat()}-*.json")):
        moment = _generated_at(path)
        if moment is not None and moment >= opened:
            found.append((path, moment))
    return found[0] if found else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="slot_guard", description=__doc__)
    parser.add_argument("--slot", required=True, choices=sorted(SLOT_WINDOW_START))
    parser.add_argument("--history-dir", default=str(HISTORY_DIR))
    args = parser.parse_args(argv)

    covered = covering_run(args.slot, Path(args.history_dir))
    should_run = covered is None

    if covered is None:
        print(
            f"No run archived today at or after {window_start(args.slot):%H:%M} IST — "
            f"the {args.slot} report has not gone out. Running the fallback."
        )
    else:
        path, moment = covered
        print(
            f"{path.name} already ran at {moment:%H:%M} IST, at or after the {args.slot} "
            "window opened. Nothing to do."
        )

    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"run={'true' if should_run else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
