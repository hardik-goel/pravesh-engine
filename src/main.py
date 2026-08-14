"""Pravesh orchestrator.

    python -m src.main                     # full run: scrape → score → persist → email + telegram
    python -m src.main --dry-run           # writes out/pravesh_preview.html, prints the ping, sends nothing
    python -m src.main --slot afternoon    # label the run (default: PRAVESH_SLOT, else manual)

Pipeline:
    1. chittorgarh builds the calendar (the spine) + detail strip + live subscription
    2. listing outcomes resolve every open call and verdict in the ledger
    3. each enabled source contributes SourceCalls, matched onto the spine by name
    4. evidence tables assemble, my-take scores, both are persisted
    5. the day's earlier run is diffed in, so an afternoon report says what moved
    6. data/latest.json is written (the web contract) and the run is archived under
       (date, slot), then email + telegram go out

A dead source degrades the run; it never ends it. A missing baseline costs the delta line
and nothing else.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional, Sequence

from . import slots
from .clock import now_market, stamp, today_market
from .config import (
    BRAND_NAME,
    BRAND_TAGLINE,
    HISTORY_STORE,
    LOGGING,
    PRODUCT_DISCLAIMER,
    RUN_SLOTS,
    SOURCES_ENABLED,
    STORE_BACKEND,
    WINDOWS,
)
from .engine import delta as delta_engine
from .engine.evidence import build_all as build_evidence_tables
from .engine.expert_feed import (
    build_expert_calls,
    build_expert_reachability,
    build_source_status,
)
from .engine.source_tracker import SourceTracker
from .engine.take import TakeEngine
from .models import IPO, IPOStatus, RunResult, SourceCall
from .report import email_builder, telegram_ping
from .sources import build_providers, build_sources, provider_chain
from .store.base import Store, build_store

log = logging.getLogger("pravesh.main")

# 2 (2026-08-03): added `expert_calls`, `expert_coverage` and `source_status` for the
#   trinetra-backend ingest.
# 3 (2026-08-03): added `expert_reachability` — per-ISSUE discovery state, so an empty
#   result can be told apart from an unchecked one per IPO rather than per source.
# 4 (2026-08-14): added `calendar_readable` — the same distinction one level up. Zero
#   IPOs with a readable calendar is a quiet day; zero with an unreadable one is a blind
#   run, and they had been publishing as the identical payload.
# Every bump is purely ADDITIVE: every earlier key is unchanged and still present, so an
# older consumer keeps working. The number moves so consumers can feature-detect on it
# rather than on the presence of a key.
SCHEMA_VERSION = 4


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

    Everything up to and including the winner is reported. A provider only speaks if it
    ran, and it only runs if every provider preferred over it came back empty — so the
    preferred one dying is exactly the news worth printing, and it was previously the
    one thing thrown away. A documented-dead fallback lower in the chain never runs
    while the one above it works, so it still cannot train the reader to ignore the
    degraded notice — the one line that has to keep meaning something.
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
            return result, collected + raised
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
        delta = result.deltas.get(ipo.slug)
        payload = ipo.to_dict()
        payload["closing_tomorrow"] = ipo.slug in closing
        payload["evidence"] = evidence.to_dict()["rows"] if evidence else []
        payload["take"] = take.to_dict() if take else None
        payload["flags"] = take.flags if take else []
        # null when this is the day's first run, or when nothing moved. Never a fabricated
        # "no change" — the absence is the statement.
        payload["delta"] = delta.to_dict() if delta and delta.has_content else None
        ipos.append(payload)

    return {
        "schema_version": SCHEMA_VERSION,
        "brand": {"name": BRAND_NAME, "tagline": BRAND_TAGLINE},
        "generated_at": result.run_at,
        "generated_at_market": stamp(when=result.run_at_market),
        "generated_at_ist": result.run_at_market,
        "run_date": result.run_date,
        "slot": result.slot,
        "slot_label": slots.label(result.slot),
        "slot_headline": slots.headline(result.slot, result.run_at_market),
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
        # False when the calendar answered but carried no dates. `counts` then reads as a
        # quiet day and is not one: a consumer must render "could not read the calendar",
        # never "no IPOs today". Added in schema 4.
        "calendar_readable": result.calendar_readable,
        # Machine-readable ingest surface for trinetra-backend. `source_status` exists so a
        # hard block can never be read as "no calls today" — see engine/expert_feed.py.
        "expert_calls": result.expert_calls,
        "expert_coverage": result.expert_coverage,
        # Per-ISSUE reachability. Source-level flags cannot say whether a SPECIFIC issue
        # was checked, and an empty state rendered off the aggregate is wrong exactly when
        # discovery is intermittent — which is the normal case here.
        "expert_reachability": result.expert_reachability,
        "source_status": result.source_status,
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
    slot: Optional[str] = None,
) -> RunResult:
    today = today or today_market()
    slot = slots.normalise(slot if slot is not None else slots.current())
    run_at_market = now_market()
    log.info(
        "=== %s run · %s · slot=%s (%s) · store=%s ===",
        BRAND_NAME,
        today.isoformat(),
        slot,
        slots.label(slot),
        store_backend or STORE_BACKEND,
    )

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

    # A calendar can answer and still be useless. On 2026-08-14 the spine timed out, the
    # fallback returned 20 rows with no dates on any of them, and every row was filtered
    # out for having no dates — which the report then delivered as "all quiet", on a day
    # two mainboard issues were open and closing. Silence must mean nothing is happening;
    # it must never mean the dates never arrived.
    dated = [i for i in calendar if i.open_date or i.close_date or i.listing_date]
    calendar_readable = bool(dated)
    if calendar and not dated:
        sources_failed.append(
            f"calendar: {len(calendar)} row(s) arrived with no dates on any of them — "
            "the calendar could not be read, so today's silence carries no information"
        )
    elif calendar and not universe:
        log.info("calendar is readable and genuinely has nothing in today's windows")

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
    opinion_sources = build_sources()  # kept: their per-route report feeds source_status
    for source in opinion_sources:
        produced = source.run(universe)
        calls.extend(produced)
        if source.failures:
            sources_failed.extend(source.failures)
        else:
            sources_ok.append(source.name)
    log.info("collected %d source call(s) from %s", len(calls), ", ".join(SOURCES_ENABLED))
    tracker.record_calls(calls)

    # 5b. The named-expert feed the backend ingests. Built from this run's calls only —
    # the ledger's older calls belong to earlier runs and would re-publish as if new.
    expert_calls, expert_coverage = build_expert_calls(universe, calls)
    expert_reachability = build_expert_reachability(universe, opinion_sources)
    source_status = build_source_status(opinion_sources)

    # 4. Evidence + take -----------------------------------------------------------
    evidence = build_evidence_tables(universe, tracker.calls_for_universe(universe), tracker)
    takes = TakeEngine(tracker).build_all(universe, evidence)
    tracker.record_takes(takes.values(), universe)

    # 5. What moved since this morning ----------------------------------------------
    # Read before anything is written, so the baseline is the *previous* run and not the
    # one we are about to save over.
    baseline, baseline_slot = delta_engine.load_baseline(store, today.isoformat(), slot)
    deltas = delta_engine.build_all(
        universe,
        baseline,
        baseline_slot,
        closing_slugs={
            i.slug for i in universe if i.close_date and (i.close_date - today).days <= 1
        },
    )
    if baseline_slot:
        log.info(
            "delta baseline: today's %s run · %d of %d IPO(s) moved",
            baseline_slot,
            len(deltas),
            len(universe),
        )
    else:
        log.info("no earlier run recorded for %s — this report carries no since-line", today.isoformat())

    # 6. Persist -------------------------------------------------------------------
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
        slot=slot,
        run_at_market=run_at_market.isoformat(),
        deltas=deltas,
        expert_calls=expert_calls,
        expert_coverage=expert_coverage,
        expert_reachability=expert_reachability,
        source_status=source_status,
        calendar_readable=calendar_readable,
    )

    if dry_run:
        log.info("dry run — nothing persisted, nothing sent")
    else:
        tracker.flush()
        payload = build_latest_payload(result, today)
        store.save_latest(payload)
        # Archived per (date, slot): the next run's delta baseline, and the history the
        # Track Record view reads.
        store.save_run_snapshot(result.run_date, slot, payload)
        store.prune_run_snapshots(int(HISTORY_STORE["retain_days"]))

    # 7. Deliver -------------------------------------------------------------------
    # The email always goes out, including on a quiet day — silence has to mean "nothing
    # to act on", never "the job broke". Every reason for not sending is logged below.
    subject = email_builder.build_subject(result, today)
    html_body = email_builder.build_html(result, today)
    message = telegram_ping.build_message(result, today)
    log.info(
        "delivery: %s %s · %d IPO(s) · email=%s telegram=%s",
        "quiet" if result.is_quiet else "full",
        slots.label(slot),
        len(universe),
        "skipped (dry run)" if dry_run else ("skipped (--no-email)" if skip_email else "sending"),
        "skipped (dry run)" if dry_run else ("skipped (--no-telegram)" if skip_telegram else "sending"),
    )

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
        if not skip_email and not email_builder.send_email(subject, html_body):
            log.error("the report was NOT emailed — see the email log lines above for why")
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
    parser.add_argument(
        "--slot",
        choices=list(RUN_SLOTS),
        help="which of the day's runs this is (default: PRAVESH_SLOT, else manual)",
    )
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
            slot=args.slot,
        )
    except Exception as exc:  # noqa: BLE001 — surface the failure loudly, exit non-zero
        log.exception("run failed")
        if not args.dry_run:
            email_builder.send_failure_notice(f"{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
