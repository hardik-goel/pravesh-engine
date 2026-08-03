"""The named-expert feed — what `trinetra-backend` ingests instead of manual entry.

Two blocks, both published in `data/latest.json`:

  * `expert_calls`  — one row per expert view we can actually attribute, in the shape the
                      backend asked for.
  * `source_status` — per source: did it work, and if not, WHY. "no matching item" and
                      "HTTP 403" are different states and the payload has to keep them
                      apart, because a hard block that reads as "no calls today" is the one
                      failure that silently corrupts everything downstream.

Capture rules, held deliberately tight:

  * `call` is VERBATIM — the phrase as published. `stanceNormalised` carries our reading of
    it, in a separate field. We never let our normalisation overwrite what was said.
  * `target` / `stop` are null here and always will be for IPO views: an expert's IPO call
    is "subscribe" or "avoid", not a level. Neither is ever derived from the other, and
    neither is ever derived from GMP or the price band.
  * `seenAt` is the PUBLICATION time or null. Never the scrape time — a two-month-old call
    scraped this morning is two months old, and back-filling `seenAt` with "now" would make
    every stale view look fresh. `capturedAt` carries the scrape time separately.
  * `url` is required. A row without one is not evidence and is dropped here rather than
    shipped for someone else to drop.
  * `NO_VIEW` never becomes a row — it is not a call. It is counted in `coverage` so the
    absence stays visible instead of looking like a gap.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from ..config import EXPERT_SOURCES
from ..models import IPO, SourceCall, Stance

log = logging.getLogger(__name__)

#: IPO expert views do not carry levels. Present, explicit, and always null — so a consumer
#: can tell "this feed has no levels" from "this field was forgotten".
NO_LEVELS: dict[str, None] = {"target": None, "stop": None}


def _row(call: SourceCall, ipo: Optional[IPO]) -> dict[str, Any]:
    return {
        # An IPO has no ticker until it lists, so `symbol` is null by construction here and
        # the join key is the slug. Stated rather than faked with a guessed ticker.
        "symbol": None,
        "ipoSlug": call.ipo_slug,
        "ipoName": call.ipo_name,
        "segment": call.segment.value,
        "expert": call.source_name,
        "call": call.raw_call,  # VERBATIM — not normalised, not collapsed
        "stanceNormalised": call.stance.value,  # our reading, kept separate on purpose
        **NO_LEVELS,
        "rationale": call.rationale,
        "url": call.url,
        "seenAt": call.published_at,  # publication time, or null. Never the scrape time.
        "capturedAt": call.captured_at,
        "source": call.publisher or "",
        "listingDate": ipo.listing_date.isoformat() if ipo and ipo.listing_date else None,
    }


def build_expert_calls(
    ipos: Sequence[IPO], calls: Sequence[SourceCall]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """-> (rows, per-expert coverage). Only attributable, non-NO_VIEW calls become rows."""
    by_slug = {i.slug: i for i in ipos}
    rows: list[dict[str, Any]] = []
    coverage: dict[str, dict[str, Any]] = {
        name: {
            "expert": name,
            "queried": 0,
            "calls": 0,
            "noView": 0,
            # Subset of noView where NOTHING answered about that issue. These are not
            # "he had no view" — they are "we could not check", and the difference is the
            # whole point of this block.
            "unreachable": 0,
            "droppedNoUrl": 0,
        }
        for name in EXPERT_SOURCES
    }

    for call in calls:
        if call.source_name not in coverage:
            continue
        stats = coverage[call.source_name]
        stats["queried"] = int(stats["queried"]) + 1
        if not call.discovery_reachable:
            stats["unreachable"] = int(stats["unreachable"]) + 1
        if call.stance is Stance.NO_VIEW:
            stats["noView"] = int(stats["noView"]) + 1
            continue
        if not call.url:
            # Unattributed. Dropped here, and counted so the drop is visible.
            stats["droppedNoUrl"] = int(stats["droppedNoUrl"]) + 1
            continue
        stats["calls"] = int(stats["calls"]) + 1
        rows.append(_row(call, by_slug.get(call.ipo_slug)))

    rows.sort(key=lambda r: (str(r["expert"]), str(r["ipoName"])))
    log.info(
        "expert feed: %d attributable call(s) from %s",
        len(rows),
        ", ".join(f"{k} {v['calls']}/{v['queried']}" for k, v in coverage.items()),
    )
    return rows, list(coverage.values())


# --------------------------------------------------------------------------------------
# Per-source status — blocked and silent must never look the same
# --------------------------------------------------------------------------------------


def build_source_status(sources: Sequence[Any]) -> list[dict[str, Any]]:
    """One `{source, ok, reason, routes}` per opinion source that ran this run."""
    status: list[dict[str, Any]] = []
    for source in sources:
        failures = list(getattr(source, "failures", []) or [])
        routes = getattr(source, "route_report", {}) or {}
        route_rows = [
            {
                "route": route,
                "attempted": data.get("attempted", 0),
                "answered": data.get("answered", 0),
                "hits": data.get("hits", 0),
                # True => we stopped trying this route mid-run; its zeroes are a give-up,
                # not a full sweep that found nothing.
                "dropped": bool(data.get("dropped", False)),
                "reason": data.get("reason", ""),
            }
            for route, data in routes.items()
        ]
        # A source is ok when it did not record a failure. That is deliberately NOT the same
        # as "it found something" — an expert with no view on today's issues worked fine.
        ok = not failures
        status.append(
            {
                "source": getattr(source, "name", "unknown"),
                "ok": ok,
                "reason": "; ".join(failures) if failures else _route_summary(route_rows),
                "routes": route_rows,
            }
        )
    return status


def _route_summary(route_rows: Sequence[dict[str, Any]]) -> str:
    """Plain sentence for the ok case, so "ok" still says how it got there."""
    if not route_rows:
        return "ok"
    parts = [
        f"{row['route']}: {row.get('reason') or 'no matching item'}" for row in route_rows
    ]
    return "; ".join(parts)


# --------------------------------------------------------------------------------------
# Per-issue reachability — the only honest basis for an empty state
# --------------------------------------------------------------------------------------


def build_expert_reachability(
    ipos: Sequence[IPO], sources: Sequence[Any]
) -> list[dict[str, Any]]:
    """One row per IPO: could we check it, and for which experts.

    Run-level route counts cannot answer this. Google News RSS rate-limits under repeated
    querying, so it routinely answers for some issues in a run and not others — leaving the
    source looking alive in the aggregate while specific issues went genuinely unchecked.
    Rendering those as "no expert view" would state a coverage fact we never established.

    `reachable` is true when AT LEAST ONE expert's discovery answered for that issue, so
    `reachable: false` is unambiguous: nothing could be checked at all. `expertsChecked` vs
    `expertsTotal` carries the partial case, which is the common one here.
    """
    rows: list[dict[str, Any]] = []
    experts = [s for s in sources if getattr(s, "issue_reach", None) is not None]
    if not experts:
        return rows

    for ipo in ipos:
        per_expert: list[dict[str, Any]] = []
        for source in experts:
            reach = source.issue_reach.get(ipo.slug)
            if reach is None:
                continue  # this expert was never asked about this issue (segment/status)
            per_expert.append(
                {
                    "expert": getattr(source, "expert_name", getattr(source, "name", "")),
                    "reachable": bool(reach.get("reachable")),
                    "via": reach.get("foundVia"),
                    "answeredRoutes": reach.get("answeredRoutes", []),
                    "quietRoutes": reach.get("quietRoutes", []),
                }
            )
        if not per_expert:
            continue
        checked = sum(1 for r in per_expert if r["reachable"])
        rows.append(
            {
                # Both spellings: the consumer accepts either and this removes the guess.
                "ipoSlug": ipo.slug,
                "ipo_slug": ipo.slug,
                "ipoName": ipo.name,
                "reachable": checked > 0,
                "expertsChecked": checked,
                "expertsTotal": len(per_expert),
                "routes": per_expert,
            }
        )

    unchecked = sum(1 for r in rows if not r["reachable"])
    partial = sum(1 for r in rows if r["reachable"] and r["expertsChecked"] < r["expertsTotal"])
    log.info(
        "expert reachability: %d issue(s) — %d fully unchecked, %d partly checked",
        len(rows),
        unchecked,
        partial,
    )
    return rows


__all__ = ["build_expert_calls", "build_expert_reachability", "build_source_status"]
