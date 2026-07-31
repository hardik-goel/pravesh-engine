"""Pravesh orchestrator.

    python -m src.main              # full run: scrape → score → persist → email + telegram
    python -m src.main --dry-run    # writes out/pravesh_preview.html, prints the ping, sends nothing

Pipeline:
    1. chittorgarh builds the calendar (the spine) + detail strip + live subscription
    2. listing outcomes resolve every open call and verdict in the ledger
    3. each enabled source contributes SourceCalls, matched onto the spine by name
    4. evidence tables assemble, my-take scores, both are persisted
    5. data/latest.json is written (the web contract), then email + telegram go out

A dead source degrades the run; it never ends it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional, Sequence

from .clock import stamp, today_market
from .config import (
    BRAND_NAME,
    BRAND_TAGLINE,
    LOGGING,
    PRODUCT_DISCLAIMER,
    SOURCES_ENABLED,
    STORE_BACKEND,
    WINDOWS,
)
from .engine.evidence import build_all as build_evidence_tables
from .engine.source_tracker import SourceTracker
from .engine.take import TakeEngine
from .models import IPO, IPOStatus, RunResult, SourceCall
from .report import email_builder, telegram_ping
from .sources import build_providers, build_sources, provider_chain
from .store.base import Store, build_store

log = logging.getLogger("pravesh.main")

SCHEMA_VERSION = 1


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, str(LOGGING["level"]).upper(), logging.INFO),
        format=str(LOGGING["format"]),
        datefmt=str(LOGGING["datefmt"]),
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# --------------------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------------------


def reporting_universe(ipos: Sequence[IPO], today: date) -> list[IPO]:
    """Issues worth talking about today: open, opening soon, or awaiting listing."""
    horizon = int(WINDOWS["opening_this_week_days"])
    watch = int(WINDOWS["allotment_watch_days"])
    selected: list[IPO] = []
    for ipo in ipos:
        status = ipo.status_on(today)
        ipo.status = status
        if status is IPOStatus.OPEN:
            selected.append(ipo)
        elif status is IPOStatus.UPCOMING and ipo.open_date and (ipo.open_date - today).days <= horizon:
            selected.append(ipo)
        elif status is IPOStatus.CLOSED:
            # Only the recent tail: bidding closed within the watch window, or a listing
            # date still ahead of us. Anything older is history, not today's report.
            closed_days = (today - ipo.close_date).days if ipo.close_date else 10_000
            listing_ahead = (
                ipo.listing_date is not None and 0 <= (ipo.listing_date - today).days <= watch
            )
            if closed_days <= watch or listing_ahead:
                selected.append(ipo)
    selected.sort(key=lambda i: (i.close_date or date.max, i.name))
    return selected


def run_role(
    role: str,
    providers: dict[str, Any],
    call: Callable[[Any], Any],
    sources_ok: list[str],
) -> tuple[Any, list[str]]:
    """Try each provider for a role until one answers.

    Only the *winning* provider's complaints are reported. A documented-dead fallback
    lower in the chain grumbling every morning would train the reader to ignore the
    degraded notice — which is the one line that has to keep meaning something.
    """
    collected: list[str] = []
    for provider in provider_chain(role, providers):
        mark = len(provider.failures)
        result = call(provider)
        raised = provider.failures[mark:]
        if result:
            log.info("%s ← %s", role, provider.name)
            if provider.name not in sources_ok:
                sources_ok.append(provider.name)
            return result, raised
        collected.extend(raised)
        log.warning("%s provider %s returned nothing; trying the next one", role, provider.name)
    return None, collected


def _stubs_for_unresolved(tracker: SourceTracker, known: Sequence[IPO]) -> list[IPO]:
    """Placeholder IPOs so listing outcomes can resolve calls whose issue left the calendar."""
    known_slugs = {i.slug for i in known}
    stubs: dict[str, IPO] = {}
    for record in tracker.verdicts:
        if record.resolved or record.ipo_slug in known_slugs:
            continue
        stubs[record.ipo_slug] = IPO(name=record.ipo_name, segment=record.segment, slug=record.ipo_slug)
    for call in tracker.calls:
        if call.resolved or call.ipo_slug in known_slugs or call.ipo_slug in stubs:
            continue
        stubs[call.ipo_slug] = IPO(name=call.ipo_name, segment=call.segment, slug=call.ipo_slug)
    return list(stubs.values())


# --------------------------------------------------------------------------------------
# Web contract
# --------------------------------------------------------------------------------------


def build_latest_payload(result: RunResult, today: date) -> dict[str, Any]:
    """data/latest.json — documented in the README under "Web contract"."""
    closing = {i.slug for i in result.closing_tomorrow(today)}
    ipos: list[dict[str, Any]] = []
    for ipo in result.ipos:
        evidence = result.evidence.get(ipo.slug)
        take = result.takes.get(ipo.slug)
        payload = ipo.to_dict()
        payload["closing_tomorrow"] = ipo.slug in closing
        payload["evidence"] = evidence.to_dict()["rows"] if evidence else []
        payload["take"] = take.to_dict() if take else None
        payload["flags"] = take.flags if take else []
        ipos.append(payload)

    return {
        "schema_version": SCHEMA_VERSION,
        "brand": {"name": BRAND_NAME, "tagline": BRAND_TAGLINE},
        "generated_at": result.run_at,
        "generated_at_market": stamp(),
        "run_date": result.run_date,
        "counts": {
            "open": len(result.open_ipos),
            "closing_tomorrow": len(closing),
            "upcoming": len(result.upcoming_ipos),
            "watch": len(result.watch_ipos),
        },
        "ipos": ipos,
        "leaderboard": [row.to_dict() for row in result.leaderboard],
        "own_accuracy": result.own_accuracy,
        "history": [record.to_dict() for record in result.history],
        "sources_ok": result.sources_ok,
        "sources_failed": result.sources_failed,
        "disclaimer": PRODUCT_DISCLAIMER,
    }


# --------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------


def run(
    *,
    dry_run: bool = False,
    skip_email: bool = False,
    skip_telegram: bool = False,
    store_backend: Optional[str] = None,
    today: Optional[date] = None,
) -> RunResult:
    today = today or today_market()
    log.info("=== %s run · %s · store=%s ===", BRAND_NAME, today.isoformat(), store_backend or STORE_BACKEND)

    store: Store = build_store(store_backend)
    tracker = SourceTracker(store)

    sources_failed: list[str] = []
    sources_ok: list[str] = []
    providers = build_providers()

    # 1. Spine — first calendar provider that returns rows wins ---------------------
    calendar, calendar_failures = run_role(
        "calendar", providers, lambda p: p.fetch_calendar(today), sources_ok
    )
    calendar = calendar or []
    sources_failed.extend(calendar_failures)
    if not calendar:
        sources_failed.append("calendar: every provider returned zero IPOs")

    universe = reporting_universe(calendar, today)
    log.info("calendar %d → reporting on %d", len(calendar), len(universe))

    # 2. Detail strip — every provider gets a pass, each only fills its own blanks --
    for provider in provider_chain("detail", providers):
        mark = len(provider.failures)
        provider.enrich_details(universe, today)
        sources_failed.extend(provider.failures[mark:])

    # 3. Live subscription — first provider with data wins --------------------------
    _, subscription_failures = run_role(
        "subscription", providers, lambda p: p.enrich_subscription(universe) or None, sources_ok
    )
    sources_failed.extend(subscription_failures)

    # 4. Resolve history against actual listing prices ------------------------------
    resolvable = list(calendar) + _stubs_for_unresolved(tracker, calendar)
    outcomes, listing_failures = run_role(
        "listing", providers, lambda p: p.fetch_listing_outcomes(resolvable) or None, sources_ok
    )
    sources_failed.extend(listing_failures)
    tracker.resolve(outcomes or {})

    # 5. Opinions -------------------------------------------------------------------
    calls: list[SourceCall] = []
    for source in build_sources():
        produced = source.run(universe)
        calls.extend(produced)
        if source.failures:
            sources_failed.extend(source.failures)
        else:
            sources_ok.append(source.name)
    log.info("collected %d source call(s) from %s", len(calls), ", ".join(SOURCES_ENABLED))
    tracker.record_calls(calls)

    # 4. Evidence + take -----------------------------------------------------------
    evidence = build_evidence_tables(universe, tracker.calls_for_universe(universe), tracker)
    takes = TakeEngine(tracker).build_all(universe, evidence)
    tracker.record_takes(takes.values(), universe)

    # 5. Persist -------------------------------------------------------------------
    result = RunResult(
        run_at=datetime.now(timezone.utc).isoformat(),
        run_date=today.isoformat(),
        ipos=universe,
        evidence=evidence,
        takes=takes,
        leaderboard=tracker.top_leaderboard(),
        own_accuracy=tracker.own_accuracy(),
        history=tracker.history(limit=int(WINDOWS["history_days"])),
        sources_failed=sources_failed,
        sources_ok=sources_ok,
        dry_run=dry_run,
    )

    if dry_run:
        log.info("dry run — nothing persisted, nothing sent")
    else:
        tracker.flush()
        store.save_latest(build_latest_payload(result, today))

    # 6. Deliver -------------------------------------------------------------------
    subject = email_builder.build_subject(result, today)
    html_body = email_builder.build_html(result, today)
    message = telegram_ping.build_message(result, today)

    if dry_run:
        path = email_builder.write_preview(html_body)
        print("\n" + "=" * 78)
        print(f"SUBJECT: {subject}")
        print(f"EMAIL:   {path}")
        print("=" * 78)
        print(message)
        print("=" * 78 + "\n")
        preview = build_latest_payload(result, today)
        print(f"latest.json would carry {len(preview['ipos'])} IPO(s), "
              f"{len(preview['leaderboard'])} leaderboard row(s), "
              f"{len(preview['history'])} history row(s)")
    else:
        if not skip_email:
            email_builder.send_email(subject, html_body)
        if not skip_telegram:
            telegram_ping.send(message)

    if sources_failed:
        log.warning("degraded run — %s", "; ".join(sources_failed))
    log.info("=== done: %d IPO(s) reported ===", len(universe))
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pravesh", description=f"{BRAND_NAME} — {BRAND_TAGLINE}")
    parser.add_argument("--dry-run", action="store_true", help="write HTML locally, print the ping, send nothing")
    parser.add_argument("--no-email", action="store_true", help="skip the email send")
    parser.add_argument("--no-telegram", action="store_true", help="skip the telegram ping")
    parser.add_argument("--store", choices=["json", "supabase"], help="override the results store")
    parser.add_argument("--date", help="override today's date (YYYY-MM-DD), for backfills")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    reference = date.fromisoformat(args.date) if args.date else None

    try:
        run(
            dry_run=args.dry_run,
            skip_email=args.no_email,
            skip_telegram=args.no_telegram,
            store_backend=args.store,
            today=reference,
        )
    except Exception as exc:  # noqa: BLE001 — surface the failure loudly, exit non-zero
        log.exception("run failed")
        if not args.dry_run:
            email_builder.send_failure_notice(f"{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
