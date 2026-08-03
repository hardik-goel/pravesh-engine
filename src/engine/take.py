"""My take — a reasoned opinion, clearly separated from the evidence.

Scoring is weighted and self-calibrating: once a source has n >= 10 resolved calls, its
weight is multiplied by clamp(accuracy / 60%, 0.5, 1.5). Sources that are absent are
dropped and the remaining weights renormalised, so a missing scraper shifts emphasis
instead of silently scoring zero.

An expert veto NEVER moves the score. It attaches a hard red banner that every surface
renders above My Take, and the reader decides. Both named experts carry the same veto; when
BOTH say AVOID the banner says so explicitly, because two independent experts against one
issue is a materially stronger warning than either alone.

The two experts SPLIT one expert weight budget (config.EXPERT_WEIGHT_BUDGET) rather than
each holding a full one, so adding a voice to the panel never inflates how much personal
opinion counts against the hard numbers.

Every take ends with "Final call is yours." — because it does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from ..config import (
    BASE_WEIGHTS,
    BOTH_EXPERTS_VETO,
    CALIBRATION,
    EXPERT_SHORT_NAMES,
    EXPERT_VETOES,
    EXPERT_WEIGHT_KEYS,
    NO_EVIDENCE,
    PRELIMINARY_EMOJI,
    PRELIMINARY_LABEL,
    SCORE_MODIFIERS,
    SOURCE_GMP,
    SOURCE_QIB,
    TAKE_CLOSER,
    THRESHOLDS,
    VERDICT_BANDS,
    WEIGHT_CALIBRATION_SOURCE,
)
from ..models import IPO, Evidence, EvidenceRow, ScoreComponent, Stance, Take
from .evidence import broker_consensus, broker_rows, expert_rows, row_for
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

        # Named experts, each on their own weight — NO_VIEW is absent, not a zero. An
        # expert who has not spoken simply drops out and the rest renormalise.
        for weight_key, source_name in EXPERT_WEIGHT_KEYS.items():
            expert_row = row_for(evidence, source_name)
            if expert_row is not None and expert_row.stance in STANCE_SCORE:
                add(
                    weight_key,
                    source_name,
                    None,
                    STANCE_SCORE[expert_row.stance],
                    f"called it {expert_row.stance.label}",
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

        # Expert vetoes: banner only, score untouched.
        flags = self._veto_flags(evidence)
        vetoing_experts = [r.source_name for r in expert_rows(evidence) if r.stance.is_avoid_type]

        strongest_for = self._strongest_for(ipo, evidence, components)
        strongest_against = self._strongest_against(ipo, evidence, components, flags)
        paragraph = self._paragraph(
            ipo, str(band["key"]), strongest_for, strongest_against, preliminary, vetoing_experts
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
    def _veto_flags(evidence: Evidence) -> list[str]:
        """Red banners from the named experts. Never touches the score — by design.

        One expert on AVOID gets their own banner. Both on AVOID collapses into a single
        stronger banner that names them together, because two independent experts against
        the same issue is a different message from one, and stacking two identical-looking
        warnings would bury that.
        """
        triggered: list[str] = []
        for veto in EXPERT_VETOES:
            if not veto.get("enabled"):
                continue
            row = row_for(evidence, str(veto["source"]))
            if row is not None and row.stance.value == veto["trigger_stance"]:
                triggered.append(str(veto["banner_text"]))

        if (
            len(triggered) > 1
            and BOTH_EXPERTS_VETO.get("enabled")
            and BOTH_EXPERTS_VETO.get("replaces_individual_banners")
        ):
            return [str(BOTH_EXPERTS_VETO["banner_text"])]
        if len(triggered) > 1 and BOTH_EXPERTS_VETO.get("enabled"):
            return [*triggered, str(BOTH_EXPERTS_VETO["banner_text"])]
        return triggered

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

    @staticmethod
    def _expert_contribution(
        row: EvidenceRow, by_key: dict[str, ScoreComponent], *, fallback: float
    ) -> float:
        """How much this expert actually moved the score, for ranking the reasoning lines."""
        for weight_key, source_name in EXPERT_WEIGHT_KEYS.items():
            if source_name == row.source_name and weight_key in by_key:
                return by_key[weight_key].contribution
        return fallback

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

        bulls_named = [r for r in expert_rows(evidence) if r.stance.is_apply_type]
        for row in bulls_named:
            candidates.append(
                (
                    self._expert_contribution(row, by_key, fallback=12.0),
                    f"{row.source_name} has called it {row.stance.label.lower()} "
                    f"({row.accuracy_label})",
                )
            )
        if len(bulls_named) > 1:
            named = " and ".join(r.source_name for r in bulls_named)
            candidates.append(
                (
                    sum(self._expert_contribution(r, by_key, fallback=12.0) for r in bulls_named),
                    f"both named experts are behind it — {named} have each told viewers to "
                    "subscribe",
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

        bears_named = [r for r in expert_rows(evidence) if r.stance.is_avoid_type]
        if len(bears_named) > 1:
            named = " and ".join(r.source_name for r in bears_named)
            candidates.append(
                (
                    120.0,  # outranks a single expert — two independent AVOIDs is the story
                    f"{named} have BOTH told viewers to avoid it — "
                    + "; ".join(f"{r.source_name} {r.accuracy_label}" for r in bears_named),
                )
            )
        for row in bears_named:
            candidates.append(
                (100.0, f"{row.source_name} has told viewers to avoid it ({row.accuracy_label})")
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
        vetoing_experts: Sequence[str],
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
        if vetoing_experts:
            if len(vetoing_experts) > 1:
                sentences.insert(
                    0,
                    "Read the red banner above first — "
                    f"{' and '.join(vetoing_experts)} are BOTH against this one, and two "
                    "independent experts agreeing is a heavier warning than either alone.",
                )
            else:
                sentences.insert(
                    0,
                    f"Read the red banner above first — {vetoing_experts[0]}, a source with a "
                    "real track record, is against this one.",
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
        for row in expert_rows(evidence):
            if row.stance is not Stance.NO_VIEW:
                bits.append(f"{EXPERT_SHORT_NAMES.get(row.source_name, row.source_name)}: "
                            f"{row.stance.label}")
        consensus = broker_consensus(evidence)
        if int(consensus["n"]):
            bits.append(f"brokers {consensus['bullish']}/{consensus['n']}")
        label = str(band["label"]) if not preliminary else f"{PRELIMINARY_LABEL} — {band['label']}"
        detail = " · ".join(bits) if bits else "no demand data yet"
        return f"{label} ({detail})"


__all__ = ["TakeEngine", "SOURCE_GMP", "SOURCE_QIB"]
