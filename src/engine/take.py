"""My take — a reasoned opinion, clearly separated from the evidence.

Scoring is weighted and self-calibrating: once a source has n >= 10 resolved calls, its
weight is multiplied by clamp(accuracy / 60%, 0.5, 1.5). Sources that are absent are
dropped and the remaining weights renormalised, so a missing scraper shifts emphasis
instead of silently scoring zero.

The Singhvi veto NEVER moves the score. It attaches a hard red banner that every surface
renders above My Take, and the reader decides.

Every take ends with "Final call is yours." — because it does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from ..config import (
    BASE_WEIGHTS,
    CALIBRATION,
    NO_EVIDENCE,
    PRELIMINARY_EMOJI,
    PRELIMINARY_LABEL,
    SCORE_MODIFIERS,
    SINGHVI_VETO,
    SOURCE_GMP,
    SOURCE_QIB,
    SOURCE_SINGHVI,
    TAKE_CLOSER,
    THRESHOLDS,
    VERDICT_BANDS,
    WEIGHT_CALIBRATION_SOURCE,
)
from ..models import IPO, Evidence, ScoreComponent, Stance, Take
from .evidence import broker_consensus, broker_rows, row_for
from .source_tracker import SourceTracker

log = logging.getLogger(__name__)

#: Where a source's stance sits on the 0..1 scoring scale.
STANCE_SCORE: dict[Stance, float] = {
    Stance.APPLY: 1.0,
    Stance.SUBSCRIBE_LONG_TERM: 0.9,
    Stance.APPLY_LISTING_GAINS: 0.75,
    Stance.NEUTRAL: 0.5,
    Stance.AVOID: 0.0,
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _band_for(score: float) -> dict[str, object]:
    for band in VERDICT_BANDS:
        if score >= float(band["min"]):  # type: ignore[arg-type]
            return band
    return VERDICT_BANDS[-1]


def _band_rank(key: str) -> int:
    order = [str(b["key"]) for b in VERDICT_BANDS]
    return order.index(key) if key in order else len(order)


class TakeEngine:
    """Builds one Take per IPO from its evidence table + the accuracy ledger."""

    def __init__(self, tracker: SourceTracker) -> None:
        self.tracker = tracker

    # ---------------------------------------------------------------------------------
    # Calibration
    # ---------------------------------------------------------------------------------

    def calibration_multiplier(self, weight_key: str) -> tuple[float, str]:
        """clamp(accuracy / baseline, 0.5, 1.5) once the source has enough history."""
        source_name = WEIGHT_CALIBRATION_SOURCE.get(weight_key)
        if not source_name:
            return 1.0, "not calibrated"
        stats = self.tracker.accuracy(source_name)
        minimum = int(CALIBRATION["min_resolved_calls"])
        if stats.n_all < minimum or stats.accuracy_all is None:
            return 1.0, f"uncalibrated (n={stats.n_all} < {minimum})"
        multiplier = _clamp(
            stats.accuracy_all / float(CALIBRATION["baseline_accuracy_pct"]),
            float(CALIBRATION["multiplier_min"]),
            float(CALIBRATION["multiplier_max"]),
        )
        return round(multiplier, 3), f"calibrated ×{multiplier:.2f} on {stats.accuracy_all:.0f}% (n={stats.n_all})"

    # ---------------------------------------------------------------------------------
    # Components
    # ---------------------------------------------------------------------------------

    def _components(self, ipo: IPO, evidence: Evidence) -> list[ScoreComponent]:
        components: list[ScoreComponent] = []

        def add(key: str, label: str, raw: Optional[float], normalised: Optional[float], note: str) -> None:
            if normalised is None:
                return
            multiplier, calibration_note = self.calibration_multiplier(key)
            base = float(BASE_WEIGHTS[key])
            effective = round(base * multiplier, 3)
            components.append(
                ScoreComponent(
                    key=key,
                    label=label,
                    raw_value=raw,
                    normalised=round(_clamp(normalised, 0.0, 1.0), 4),
                    base_weight=base,
                    calibration_multiplier=multiplier,
                    effective_weight=effective,
                    contribution=0.0,  # filled in after renormalisation
                    note=f"{note} · {calibration_note}",
                )
            )

        book = ipo.subscription

        # QIB
        if book.qib is not None:
            add(
                "qib",
                "QIB subscription",
                book.qib,
                book.qib / float(THRESHOLDS["qib_full_credit_x"]),
                f"{book.qib:.2f}x vs {THRESHOLDS['qib_full_credit_x']:.0f}x for full credit",
            )

        # GMP
        if ipo.gmp_pct is not None:
            add(
                "gmp",
                "Grey market premium",
                ipo.gmp_pct,
                max(0.0, ipo.gmp_pct) / float(THRESHOLDS["gmp_full_credit_pct"]),
                f"{ipo.gmp_pct:+.1f}% vs {THRESHOLDS['gmp_full_credit_pct']:.0f}% for full credit (indicative)",
            )

        # Total subscription
        if book.total is not None:
            add(
                "total_subscription",
                "Overall subscription",
                book.total,
                book.total / float(THRESHOLDS["total_sub_full_credit_x"]),
                f"{book.total:.2f}x overall",
            )

        # Singhvi — NO_VIEW is absent, not a zero
        singhvi_row = row_for(evidence, SOURCE_SINGHVI)
        if singhvi_row is not None and singhvi_row.stance in STANCE_SCORE:
            add(
                "singhvi",
                "Anil Singhvi",
                None,
                STANCE_SCORE[singhvi_row.stance],
                f"called it {singhvi_row.stance.label}",
            )

        # Broker consensus
        consensus = broker_consensus(evidence)
        if int(consensus["n"]) > 0:
            add(
                "brokers",
                "Broker consensus",
                float(consensus["score"]),
                float(consensus["score"]),
                f"{consensus['bullish']}/{consensus['n']} bullish, accuracy-weighted",
            )

        return components

    # ---------------------------------------------------------------------------------
    # Scoring
    # ---------------------------------------------------------------------------------

    def build(self, ipo: IPO, evidence: Evidence) -> Take:
        components = self._components(ipo, evidence)
        modifiers: list[str] = []

        total_weight = sum(c.effective_weight for c in components)
        if total_weight <= 0:
            # Nothing has landed: no bidding data, no GMP, nobody on record. Scoring that
            # 0/100 would read as AVOID, which would be a lie. We say so instead.
            return self._no_evidence_take(ipo)

        for component in components:
            component.contribution = round(
                100.0 * component.normalised * component.effective_weight / total_weight, 2
            )
        score = sum(c.contribution for c in components)

        # Modifier: OFS-heavy issue — promoters cashing out, not capital going in.
        ofs_pct = ipo.ofs_pct
        if ofs_pct is not None and ofs_pct > float(THRESHOLDS["ofs_heavy_pct"]):
            score += float(SCORE_MODIFIERS["ofs_heavy_penalty"])
            modifiers.append(
                f"{SCORE_MODIFIERS['ofs_heavy_penalty']:+.0f} — {ofs_pct:.0f}% of the issue is offer-for-sale"
            )

        # Modifier: SME issues carry structurally higher risk.
        if ipo.is_sme:
            score += float(SCORE_MODIFIERS["sme_penalty"])
            modifiers.append(f"{SCORE_MODIFIERS['sme_penalty']:+.0f} — SME segment risk")

        score = round(_clamp(score, 0.0, 100.0), 1)
        band = _band_for(score)

        # SME cap: never better than RISKY unless the score is genuinely high.
        if ipo.is_sme and score < float(SCORE_MODIFIERS["sme_apply_override_score"]):
            risky = next(b for b in VERDICT_BANDS if b["key"] == "RISKY")
            if _band_rank(str(band["key"])) < _band_rank("RISKY"):
                band = risky
                modifiers.append(
                    f"SME capped at RISKY (needs {SCORE_MODIFIERS['sme_apply_override_score']:.0f}+ to rate higher)"
                )

        preliminary = not ipo.subscription.has_data
        if preliminary:
            modifiers.append("PRELIMINARY — bidding data not published yet")

        # Singhvi veto: banner only, score untouched.
        flags: list[str] = []
        if SINGHVI_VETO["enabled"]:
            singhvi_row = row_for(evidence, SOURCE_SINGHVI)
            if singhvi_row is not None and singhvi_row.stance.value == SINGHVI_VETO["trigger_stance"]:
                flags.append(str(SINGHVI_VETO["banner_text"]))

        strongest_for = self._strongest_for(ipo, evidence, components)
        strongest_against = self._strongest_against(ipo, evidence, components, flags)
        paragraph = self._paragraph(
            ipo, str(band["key"]), strongest_for, strongest_against, preliminary, flags
        )

        verdict_label = str(band["label"])
        verdict_emoji = str(band["emoji"])
        if preliminary:
            verdict_label = f"{PRELIMINARY_LABEL} — {verdict_label}"
            verdict_emoji = PRELIMINARY_EMOJI

        take = Take(
            ipo_slug=ipo.slug,
            score=score,
            verdict_key=str(band["key"]),
            verdict_label=verdict_label,
            verdict_emoji=verdict_emoji,
            verdict_color=str(band["color"]),
            paragraph=paragraph,
            one_liner=self._one_liner(ipo, evidence, band, preliminary),
            preliminary=preliminary,
            flags=flags,
            components=components,
            modifiers=modifiers,
            strongest_for=strongest_for,
            strongest_against=strongest_against,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info("take for %s: %s (%.1f)", ipo.name, take.verdict_label, take.score)
        return take

    @staticmethod
    def _no_evidence_take(ipo: IPO) -> Take:
        sme_note = (
            " It is an SME issue, so thin coverage is normal — that is a reason for caution, "
            "not comfort."
            if ipo.is_sme
            else ""
        )
        paragraph = (
            f"Nothing has landed on {ipo.name} yet — no bidding data, no grey-market print and no "
            "named analyst on record. There is no honest score to give here, so this is a "
            f"placeholder, not a call.{sme_note} Check back once the book opens. {TAKE_CLOSER}"
        )
        return Take(
            ipo_slug=ipo.slug,
            score=0.0,
            verdict_key=str(NO_EVIDENCE["key"]),
            verdict_label=str(NO_EVIDENCE["label"]),
            verdict_emoji=str(NO_EVIDENCE["emoji"]),
            verdict_color=str(NO_EVIDENCE["color"]),
            paragraph=paragraph,
            one_liner=str(NO_EVIDENCE["label"]),
            preliminary=True,
            has_score=False,
            strongest_for="nothing yet",
            strongest_against="no data of any kind has been published",
            modifiers=["No scorable input available"],
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def build_all(self, ipos: Sequence[IPO], evidence: dict[str, Evidence]) -> dict[str, Take]:
        return {
            ipo.slug: self.build(ipo, evidence.get(ipo.slug, Evidence(ipo_slug=ipo.slug)))
            for ipo in ipos
        }

    # ---------------------------------------------------------------------------------
    # Reasoning — cite the actual evidence, never just the numbers
    # ---------------------------------------------------------------------------------

    def _strongest_for(
        self, ipo: IPO, evidence: Evidence, components: Sequence[ScoreComponent]
    ) -> str:
        candidates: list[tuple[float, str]] = []
        by_key = {c.key: c for c in components}
        book = ipo.subscription

        qib = by_key.get("qib")
        if qib and book.qib is not None and book.qib >= float(THRESHOLDS["qib_apply_x"]):
            candidates.append(
                (
                    qib.contribution,
                    f"institutions have taken the QIB book to {book.qib:.1f}x, which is real money "
                    "committing before listing",
                )
            )
        gmp = by_key.get("gmp")
        if gmp and ipo.gmp_pct is not None and ipo.gmp_pct >= float(THRESHOLDS["gmp_neutral_pct"]):
            candidates.append(
                (
                    gmp.contribution,
                    f"the grey market is quoting a {ipo.gmp_pct:.0f}% premium (indicative and unofficial, "
                    "but it is the only forward-looking price we get)",
                )
            )
        total = by_key.get("total_subscription")
        if total and book.total is not None and book.total >= 3.0:
            candidates.append(
                (total.contribution, f"the book is {book.total:.1f}x subscribed overall")
            )

        singhvi_row = row_for(evidence, SOURCE_SINGHVI)
        if singhvi_row is not None and singhvi_row.stance.is_apply_type:
            candidates.append(
                (
                    by_key["singhvi"].contribution if "singhvi" in by_key else 12.0,
                    f"Anil Singhvi has called it {singhvi_row.stance.label.lower()} "
                    f"({singhvi_row.accuracy_label})",
                )
            )

        bulls = [r for r in broker_rows(evidence) if r.stance.is_apply_type]
        if bulls:
            named = ", ".join(r.source_name for r in bulls[:3])
            consensus = broker_consensus(evidence)
            candidates.append(
                (
                    by_key["brokers"].contribution if "brokers" in by_key else 8.0,
                    f"{consensus['bullish']} of {consensus['n']} named brokerages back the issue "
                    f"({named})",
                )
            )

        if ipo.ofs_pct is not None and ipo.ofs_pct < 40 and ipo.issue_size_cr:
            candidates.append(
                (6.0, f"only {ipo.ofs_pct:.0f}% is offer-for-sale, so most of the raise funds the business")
            )

        if not candidates:
            return "there is no strong positive signal on the table yet"
        return max(candidates, key=lambda c: c[0])[1]

    def _strongest_against(
        self,
        ipo: IPO,
        evidence: Evidence,
        components: Sequence[ScoreComponent],
        flags: Sequence[str],
    ) -> str:
        candidates: list[tuple[float, str]] = []
        book = ipo.subscription

        singhvi_row = row_for(evidence, SOURCE_SINGHVI)
        if singhvi_row is not None and singhvi_row.stance.is_avoid_type:
            candidates.append(
                (100.0, f"Anil Singhvi has told viewers to avoid it ({singhvi_row.accuracy_label})")
            )

        bears = [r for r in broker_rows(evidence) if r.stance.is_avoid_type]
        if bears:
            named = ", ".join(r.source_name for r in bears[:3])
            candidates.append((40.0, f"{named} explicitly say avoid"))

        if book.qib is not None and book.qib < float(THRESHOLDS["qib_neutral_x"]):
            candidates.append(
                (
                    35.0,
                    f"the QIB book is only {book.qib:.1f}x — institutions are not showing up, which is the "
                    "single most reliable warning sign in this market",
                )
            )
        if ipo.gmp_pct is not None and ipo.gmp_pct < float(THRESHOLDS["gmp_neutral_pct"]):
            candidates.append(
                (30.0, f"the grey market premium is only {ipo.gmp_pct:.0f}%, so no listing pop is being priced in")
            )
        if ipo.ofs_pct is not None and ipo.ofs_pct > float(THRESHOLDS["ofs_heavy_pct"]):
            candidates.append(
                (
                    28.0,
                    f"{ipo.ofs_pct:.0f}% of the issue is promoters and existing holders selling down, not "
                    "capital going into the company",
                )
            )
        if ipo.is_sme:
            candidates.append(
                (
                    20.0,
                    "it is an SME issue — thin post-listing liquidity, larger lot size and far less disclosure "
                    "than a mainboard name",
                )
            )
        if not book.has_data:
            candidates.append((15.0, "bidding data has not been published yet, so demand is unproven"))
        if not evidence.named_rows:
            candidates.append((10.0, "no named analyst or brokerage has gone on record on this issue"))

        if not candidates:
            return "nothing in the evidence stands out as a red flag, which is itself worth double-checking"
        return max(candidates, key=lambda c: c[0])[1]

    @staticmethod
    def _paragraph(
        ipo: IPO,
        verdict_key: str,
        strongest_for: str,
        strongest_against: str,
        preliminary: bool,
        flags: Sequence[str],
    ) -> str:
        closer = {
            "APPLY": "On balance the case for applying holds up, with that risk priced in rather than ignored.",
            "LISTING_GAINS": (
                "That reads as a listing-day trade rather than something to hold — take the pop if it comes "
                "and reassess."
            ),
            "RISKY": (
                "That is a thin case: worth a shot only if you carry genuine risk appetite and can sit on the "
                "position if listing disappoints."
            ),
            "AVOID": "That is not enough to justify locking up money and taking listing risk here.",
        }.get(verdict_key, "The evidence is mixed.")

        sentences = [
            f"The strongest argument for {ipo.name} is that {strongest_for}.",
            f"Against it: {strongest_against}.",
            closer,
        ]
        if preliminary:
            sentences.insert(
                2,
                "Bidding has not opened yet, so this is a preliminary read that will move once QIB and "
                "grey-market numbers land.",
            )
        if flags:
            sentences.insert(
                0,
                "Read the red banner above first — a source with a real track record is against this one.",
            )
        return " ".join(sentences) + f" {TAKE_CLOSER}"

    @staticmethod
    def _one_liner(ipo: IPO, evidence: Evidence, band: dict[str, object], preliminary: bool) -> str:
        """Compact summary for Telegram."""
        bits: list[str] = []
        if ipo.subscription.qib is not None:
            bits.append(f"QIB {ipo.subscription.qib:.1f}x")
        if ipo.subscription.total is not None:
            bits.append(f"total {ipo.subscription.total:.1f}x")
        if ipo.gmp_pct is not None:
            bits.append(f"GMP {ipo.gmp_pct:+.0f}%")
        singhvi_row = row_for(evidence, SOURCE_SINGHVI)
        if singhvi_row is not None and singhvi_row.stance is not Stance.NO_VIEW:
            bits.append(f"Singhvi: {singhvi_row.stance.label}")
        consensus = broker_consensus(evidence)
        if int(consensus["n"]):
            bits.append(f"brokers {consensus['bullish']}/{consensus['n']}")
        label = str(band["label"]) if not preliminary else f"{PRELIMINARY_LABEL} — {band['label']}"
        detail = " · ".join(bits) if bits else "no demand data yet"
        return f"{label} ({detail})"


__all__ = ["TakeEngine", "SOURCE_GMP", "SOURCE_QIB"]
