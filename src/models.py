"""Dataclasses shared by every layer of Pravesh.

Every object here is JSON round-trippable via `to_dict()` / `from_dict()` so the store
backend (JSON files or Supabase) never needs bespoke serialisation logic.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

# --------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------


class Segment(str, Enum):
    MAINBOARD = "MAINBOARD"
    SME = "SME"


class IPOStatus(str, Enum):
    UPCOMING = "UPCOMING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"  # bidding done, not yet listed
    LISTED = "LISTED"
    UNKNOWN = "UNKNOWN"


class Stance(str, Enum):
    """What a source said. Ordered loosely from most positive to most negative."""

    APPLY = "APPLY"
    SUBSCRIBE_LONG_TERM = "SUBSCRIBE_LONG_TERM"
    APPLY_LISTING_GAINS = "APPLY_LISTING_GAINS"
    NEUTRAL = "NEUTRAL"
    AVOID = "AVOID"
    NO_VIEW = "NO_VIEW"

    @property
    def is_apply_type(self) -> bool:
        return self in (Stance.APPLY, Stance.SUBSCRIBE_LONG_TERM, Stance.APPLY_LISTING_GAINS)

    @property
    def is_avoid_type(self) -> bool:
        return self is Stance.AVOID

    @property
    def is_scorable(self) -> bool:
        """NEUTRAL and NO_VIEW are excluded from the accuracy ledger."""
        return self.is_apply_type or self.is_avoid_type

    @property
    def label(self) -> str:
        return {
            Stance.APPLY: "Subscribe",
            Stance.SUBSCRIBE_LONG_TERM: "Subscribe — long term",
            Stance.APPLY_LISTING_GAINS: "Subscribe — listing gains",
            Stance.NEUTRAL: "Neutral",
            Stance.AVOID: "Avoid",
            Stance.NO_VIEW: "No view",
        }[self]

    @property
    def polarity(self) -> float:
        """+1 bullish, 0 neutral/no view, -1 bearish. Used by consensus maths."""
        if self is Stance.APPLY:
            return 1.0
        if self is Stance.SUBSCRIBE_LONG_TERM:
            return 0.85
        if self is Stance.APPLY_LISTING_GAINS:
            return 0.7
        if self is Stance.AVOID:
            return -1.0
        return 0.0


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def slugify(name: str) -> str:
    """Stable identity for an IPO across runs and stores."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return cleaned.strip("-") or "unknown"


def _iso(value: Optional[date | datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Core entities
# --------------------------------------------------------------------------------------


@dataclass
class Subscription:
    """Live bidding multiples. None means "not published yet"."""

    qib: Optional[float] = None
    nii: Optional[float] = None
    retail: Optional[float] = None
    employee: Optional[float] = None
    total: Optional[float] = None
    updated_at: Optional[str] = None

    @property
    def has_data(self) -> bool:
        return any(v is not None for v in (self.qib, self.nii, self.retail, self.total))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Subscription":
        raw = raw or {}
        return cls(
            qib=raw.get("qib"),
            nii=raw.get("nii"),
            retail=raw.get("retail"),
            employee=raw.get("employee"),
            total=raw.get("total"),
            updated_at=raw.get("updated_at"),
        )


@dataclass
class IPO:
    """The calendar spine. Everything else is matched onto this by name."""

    name: str
    segment: Segment
    slug: str = ""
    status: IPOStatus = IPOStatus.UNKNOWN
    open_date: Optional[date] = None
    close_date: Optional[date] = None
    allotment_date: Optional[date] = None
    listing_date: Optional[date] = None
    price_band_low: Optional[float] = None
    price_band_high: Optional[float] = None
    lot_size: Optional[int] = None
    min_investment: Optional[float] = None
    issue_size_cr: Optional[float] = None
    fresh_issue_cr: Optional[float] = None
    ofs_cr: Optional[float] = None
    subscription: Subscription = field(default_factory=Subscription)
    gmp: Optional[float] = None
    gmp_pct: Optional[float] = None
    gmp_source: Optional[str] = None
    detail_url: Optional[str] = None
    exchange: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = slugify(self.name)
        if isinstance(self.segment, str):
            self.segment = Segment(self.segment)
        if isinstance(self.status, str):
            self.status = IPOStatus(self.status)

    # -- derived -----------------------------------------------------------------------

    @property
    def is_sme(self) -> bool:
        return self.segment is Segment.SME

    @property
    def ofs_pct(self) -> Optional[float]:
        if self.issue_size_cr and self.ofs_cr is not None and self.issue_size_cr > 0:
            return round(100.0 * self.ofs_cr / self.issue_size_cr, 1)
        if self.issue_size_cr and self.fresh_issue_cr is not None and self.issue_size_cr > 0:
            return round(100.0 * (1 - self.fresh_issue_cr / self.issue_size_cr), 1)
        return None

    @property
    def price_band_label(self) -> str:
        if self.price_band_low and self.price_band_high:
            if abs(self.price_band_low - self.price_band_high) < 0.01:
                return f"₹{self.price_band_high:,.0f}"
            return f"₹{self.price_band_low:,.0f}–₹{self.price_band_high:,.0f}"
        if self.price_band_high:
            return f"₹{self.price_band_high:,.0f}"
        return "—"

    def status_on(self, today: date) -> IPOStatus:
        """Recompute status against a reference date; dates beat whatever was scraped."""
        if self.listing_date and today >= self.listing_date:
            return IPOStatus.LISTED
        if self.close_date and today > self.close_date:
            return IPOStatus.CLOSED
        if self.open_date and self.close_date and self.open_date <= today <= self.close_date:
            return IPOStatus.OPEN
        if self.open_date and today < self.open_date:
            return IPOStatus.UPCOMING
        return self.status

    # -- serialisation -----------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "segment": self.segment.value,
            "status": self.status.value,
            "open_date": _iso(self.open_date),
            "close_date": _iso(self.close_date),
            "allotment_date": _iso(self.allotment_date),
            "listing_date": _iso(self.listing_date),
            "price_band_low": self.price_band_low,
            "price_band_high": self.price_band_high,
            "price_band_label": self.price_band_label,
            "lot_size": self.lot_size,
            "min_investment": self.min_investment,
            "issue_size_cr": self.issue_size_cr,
            "fresh_issue_cr": self.fresh_issue_cr,
            "ofs_cr": self.ofs_cr,
            "ofs_pct": self.ofs_pct,
            "subscription": self.subscription.to_dict(),
            "gmp": self.gmp,
            "gmp_pct": self.gmp_pct,
            "gmp_source": self.gmp_source,
            "detail_url": self.detail_url,
            "exchange": self.exchange,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IPO":
        return cls(
            name=raw["name"],
            segment=Segment(raw.get("segment", "MAINBOARD")),
            slug=raw.get("slug", ""),
            status=IPOStatus(raw.get("status", "UNKNOWN")),
            open_date=_parse_date(raw.get("open_date")),
            close_date=_parse_date(raw.get("close_date")),
            allotment_date=_parse_date(raw.get("allotment_date")),
            listing_date=_parse_date(raw.get("listing_date")),
            price_band_low=raw.get("price_band_low"),
            price_band_high=raw.get("price_band_high"),
            lot_size=raw.get("lot_size"),
            min_investment=raw.get("min_investment"),
            issue_size_cr=raw.get("issue_size_cr"),
            fresh_issue_cr=raw.get("fresh_issue_cr"),
            ofs_cr=raw.get("ofs_cr"),
            subscription=Subscription.from_dict(raw.get("subscription")),
            gmp=raw.get("gmp"),
            gmp_pct=raw.get("gmp_pct"),
            gmp_source=raw.get("gmp_source"),
            detail_url=raw.get("detail_url"),
            exchange=raw.get("exchange"),
        )


@dataclass
class IPODelta:
    """What moved on one IPO since an earlier run of the same trading day.

    Built by ``engine.delta`` against an archived snapshot. Never invented: with no earlier
    snapshot to compare against there is no delta, and every surface omits the line rather
    than printing a change it cannot substantiate.
    """

    ipo_slug: str
    since_label: str  # "since this morning" — phrased from the *baseline* run
    baseline_slot: str = ""
    parts: list[str] = field(default_factory=list)  # "QIB 4.20x → 11.80x", most material first
    is_new: bool = False  # absent from the baseline run entirely

    @property
    def has_content(self) -> bool:
        return self.is_new or bool(self.parts)

    @property
    def line(self) -> str:
        """The one-liner every surface renders. Empty when there is nothing to say."""
        if self.is_new:
            return f"new {self.since_label}"
        if not self.parts:
            return ""
        return " · ".join([self.since_label, *self.parts])

    def to_dict(self) -> dict[str, Any]:
        return {
            "ipo_slug": self.ipo_slug,
            "since_label": self.since_label,
            "baseline_slot": self.baseline_slot,
            "parts": list(self.parts),
            "is_new": self.is_new,
            "line": self.line,
        }


@dataclass
class SourceCall:
    """One named source's stance on one IPO. The atom of the accuracy ledger."""

    source_name: str
    ipo_slug: str
    ipo_name: str
    stance: Stance
    rationale: str = ""
    url: Optional[str] = None
    captured_at: Optional[str] = None
    segment: Segment = Segment.MAINBOARD
    is_synthetic: bool = False  # GMP signal / QIB signal
    # -- provenance, for consumers that grade on what was actually said ----------------
    #: The phrase AS PUBLISHED that produced `stance` ("subscribe for listing gains").
    #: `stance` is our reading of it; this is theirs, unnormalised.
    raw_call: str = ""
    #: When the item was PUBLISHED, if the source states it. None when it does not —
    #: never back-filled with the scrape time, because staleness is measured from here.
    published_at: Optional[str] = None
    #: Who published it ("zeebiz.com", "Zee Business"). Not the person who said it.
    publisher: str = ""
    #: Did ANY discovery route answer for THIS issue? False means we could not check —
    #: which is not the same as "there was nothing to find". A NO_VIEW with
    #: discovery_reachable=False carries no information at all and must never be rendered
    #: as coverage absence. Defaults True: sources that do not do per-issue discovery
    #: (GMP, QIB, the calendar providers) are always "reachable" by construction.
    discovery_reachable: bool = True
    #: Which route produced this call ("Google News RSS"), or None when nothing answered.
    discovery_route: Optional[str] = None
    # Outcome resolution (filled in once the IPO lists)
    resolved: bool = False
    correct: Optional[bool] = None
    listing_gain_pct: Optional[float] = None
    resolved_at: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.stance, str):
            self.stance = Stance(self.stance)
        if isinstance(self.segment, str):
            self.segment = Segment(self.segment)

    @property
    def key(self) -> str:
        return f"{self.ipo_slug}::{self.source_name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "ipo_slug": self.ipo_slug,
            "ipo_name": self.ipo_name,
            "stance": self.stance.value,
            "rationale": self.rationale,
            "url": self.url,
            "captured_at": self.captured_at,
            "segment": self.segment.value,
            "is_synthetic": self.is_synthetic,
            "raw_call": self.raw_call,
            "published_at": self.published_at,
            "publisher": self.publisher,
            "discovery_reachable": self.discovery_reachable,
            "discovery_route": self.discovery_route,
            "resolved": self.resolved,
            "correct": self.correct,
            "listing_gain_pct": self.listing_gain_pct,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourceCall":
        return cls(
            source_name=raw["source_name"],
            ipo_slug=raw["ipo_slug"],
            ipo_name=raw.get("ipo_name", raw["ipo_slug"]),
            stance=Stance(raw.get("stance", "NO_VIEW")),
            rationale=raw.get("rationale", ""),
            url=raw.get("url"),
            captured_at=raw.get("captured_at"),
            segment=Segment(raw.get("segment", "MAINBOARD")),
            is_synthetic=bool(raw.get("is_synthetic", False)),
            raw_call=raw.get("raw_call", ""),
            published_at=raw.get("published_at"),
            publisher=raw.get("publisher", ""),
            discovery_reachable=bool(raw.get("discovery_reachable", True)),
            discovery_route=raw.get("discovery_route"),
            resolved=bool(raw.get("resolved", False)),
            correct=raw.get("correct"),
            listing_gain_pct=raw.get("listing_gain_pct"),
            resolved_at=raw.get("resolved_at"),
        )


@dataclass
class SourceAccuracy:
    """Rolling ledger stats for one source identity."""

    source_name: str
    n_all: int = 0
    correct_all: int = 0
    n_recent: int = 0
    correct_recent: int = 0
    is_synthetic: bool = False

    @property
    def accuracy_all(self) -> Optional[float]:
        return round(100.0 * self.correct_all / self.n_all, 1) if self.n_all else None

    @property
    def accuracy_recent(self) -> Optional[float]:
        return round(100.0 * self.correct_recent / self.n_recent, 1) if self.n_recent else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "n_all": self.n_all,
            "correct_all": self.correct_all,
            "accuracy_all": self.accuracy_all,
            "n_recent": self.n_recent,
            "correct_recent": self.correct_recent,
            "accuracy_recent": self.accuracy_recent,
            "is_synthetic": self.is_synthetic,
        }


@dataclass
class EvidenceRow:
    """One row of the per-IPO evidence table — the product's heart."""

    source_name: str
    stance: Stance
    rationale: str
    accuracy_pct: Optional[float] = None
    accuracy_n: int = 0
    accuracy_label: str = ""
    url: Optional[str] = None
    is_synthetic: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.stance, str):
            self.stance = Stance(self.stance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "stance": self.stance.value,
            "stance_label": self.stance.label,
            "rationale": self.rationale,
            "accuracy_pct": self.accuracy_pct,
            "accuracy_n": self.accuracy_n,
            "accuracy_label": self.accuracy_label,
            "url": self.url,
            "is_synthetic": self.is_synthetic,
        }


@dataclass
class Evidence:
    ipo_slug: str
    rows: list[EvidenceRow] = field(default_factory=list)

    @property
    def named_rows(self) -> list[EvidenceRow]:
        """Human sources only — excludes GMP/QIB synthetic signals."""
        return [r for r in self.rows if not r.is_synthetic]

    def to_dict(self) -> dict[str, Any]:
        return {"ipo_slug": self.ipo_slug, "rows": [r.to_dict() for r in self.rows]}


@dataclass
class ScoreComponent:
    """Audit trail for one weighted input into My Take."""

    key: str
    label: str
    raw_value: Optional[float]
    normalised: float  # 0..1
    base_weight: float
    calibration_multiplier: float
    effective_weight: float
    contribution: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Take:
    """My take: score, band, reasoning, flags. Never the last word."""

    ipo_slug: str
    score: float
    verdict_key: str
    verdict_label: str
    verdict_emoji: str
    verdict_color: str
    paragraph: str
    one_liner: str
    preliminary: bool = False
    has_score: bool = True  # False when nothing has landed yet — we print "—", not 0/100
    flags: list[str] = field(default_factory=list)
    components: list[ScoreComponent] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    strongest_for: str = ""
    strongest_against: str = ""
    created_at: Optional[str] = None

    @property
    def has_veto(self) -> bool:
        return bool(self.flags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ipo_slug": self.ipo_slug,
            "score": self.score,
            "verdict_key": self.verdict_key,
            "verdict_label": self.verdict_label,
            "verdict_emoji": self.verdict_emoji,
            "verdict_color": self.verdict_color,
            "paragraph": self.paragraph,
            "one_liner": self.one_liner,
            "preliminary": self.preliminary,
            "has_score": self.has_score,
            "flags": list(self.flags),
            "components": [c.to_dict() for c in self.components],
            "modifiers": list(self.modifiers),
            "strongest_for": self.strongest_for,
            "strongest_against": self.strongest_against,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Take":
        return cls(
            ipo_slug=raw["ipo_slug"],
            score=raw.get("score", 0.0),
            verdict_key=raw.get("verdict_key", "AVOID"),
            verdict_label=raw.get("verdict_label", ""),
            verdict_emoji=raw.get("verdict_emoji", ""),
            verdict_color=raw.get("verdict_color", "#000000"),
            paragraph=raw.get("paragraph", ""),
            one_liner=raw.get("one_liner", ""),
            preliminary=bool(raw.get("preliminary", False)),
            has_score=bool(raw.get("has_score", True)),
            flags=list(raw.get("flags", [])),
            modifiers=list(raw.get("modifiers", [])),
            strongest_for=raw.get("strongest_for", ""),
            strongest_against=raw.get("strongest_against", ""),
            created_at=raw.get("created_at"),
        )


@dataclass
class VerdictRecord:
    """Persisted my-take history + the eventual listing outcome."""

    ipo_slug: str
    ipo_name: str
    segment: Segment
    verdict_key: str
    score: float
    created_at: str
    listing_date: Optional[str] = None
    issue_price: Optional[float] = None
    listing_price: Optional[float] = None
    listing_gain_pct: Optional[float] = None
    resolved: bool = False
    correct: Optional[bool] = None
    flags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.segment, str):
            self.segment = Segment(self.segment)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["segment"] = self.segment.value
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VerdictRecord":
        return cls(
            ipo_slug=raw["ipo_slug"],
            ipo_name=raw.get("ipo_name", raw["ipo_slug"]),
            segment=Segment(raw.get("segment", "MAINBOARD")),
            verdict_key=raw.get("verdict_key", "AVOID"),
            score=raw.get("score", 0.0),
            created_at=raw.get("created_at", ""),
            listing_date=raw.get("listing_date"),
            issue_price=raw.get("issue_price"),
            listing_price=raw.get("listing_price"),
            listing_gain_pct=raw.get("listing_gain_pct"),
            resolved=bool(raw.get("resolved", False)),
            correct=raw.get("correct"),
            flags=list(raw.get("flags", [])),
        )


@dataclass
class RunResult:
    """Everything one run produced. Serialised straight into data/latest.json."""

    run_at: str
    run_date: str
    ipos: list[IPO] = field(default_factory=list)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    takes: dict[str, Take] = field(default_factory=dict)
    leaderboard: list[SourceAccuracy] = field(default_factory=list)
    own_accuracy: dict[str, Any] = field(default_factory=dict)
    history: list[VerdictRecord] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    sources_ok: list[str] = field(default_factory=list)
    dry_run: bool = False
    # Which of the day's runs this is, and when it happened in market time. Captured once
    # so the email, the ping and latest.json cannot disagree about the timestamp.
    slot: str = "manual"
    run_at_market: str = ""  # ISO 8601 with the market offset
    deltas: dict[str, IPODelta] = field(default_factory=dict)  # by ipo_slug, may be empty
    # The named-expert feed consumed by trinetra-backend. See engine/expert_feed.py.
    expert_calls: list[dict[str, Any]] = field(default_factory=list)
    expert_coverage: list[dict[str, Any]] = field(default_factory=list)
    expert_reachability: list[dict[str, Any]] = field(default_factory=list)
    source_status: list[dict[str, Any]] = field(default_factory=list)
    # False when the calendar answered but carried no dates, so nothing could be placed
    # in a window. An empty report then says nothing about the day and must not be
    # delivered as one — see is_quiet.
    calendar_readable: bool = True

    # -- bucketing used by every surface --------------------------------------------

    def by_status(self, status: IPOStatus) -> list[IPO]:
        return [i for i in self.ipos if i.status is status]

    @property
    def open_ipos(self) -> list[IPO]:
        return self.by_status(IPOStatus.OPEN)

    @property
    def upcoming_ipos(self) -> list[IPO]:
        return self.by_status(IPOStatus.UPCOMING)

    @property
    def watch_ipos(self) -> list[IPO]:
        """Closed but not yet listed — allotment & listing watch."""
        return self.by_status(IPOStatus.CLOSED)

    def closing_tomorrow(self, today: date) -> list[IPO]:
        return [i for i in self.open_ipos if i.close_date and (i.close_date - today).days <= 1]

    @property
    def is_quiet(self) -> bool:
        """A day with nothing in it — which requires having been able to look.

        An unreadable calendar produces the same empty buckets as a genuinely quiet
        day. Reporting the two identically once announced "all quiet" on a day with
        two mainboard issues open and closing, so emptiness alone is not enough.
        """
        return self.calendar_readable and not (self.open_ipos or self.upcoming_ipos or self.watch_ipos)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_at": self.run_at,
            "run_date": self.run_date,
            "ipos": [i.to_dict() for i in self.ipos],
            "evidence": {k: v.to_dict() for k, v in self.evidence.items()},
            "takes": {k: v.to_dict() for k, v in self.takes.items()},
            "leaderboard": [s.to_dict() for s in self.leaderboard],
            "own_accuracy": self.own_accuracy,
            "history": [h.to_dict() for h in self.history],
            "sources_failed": list(self.sources_failed),
            "sources_ok": list(self.sources_ok),
            "dry_run": self.dry_run,
            "slot": self.slot,
            "run_at_market": self.run_at_market,
            "deltas": {k: v.to_dict() for k, v in self.deltas.items()},
            "expert_calls": list(self.expert_calls),
            "expert_coverage": list(self.expert_coverage),
            "expert_reachability": list(self.expert_reachability),
            "source_status": list(self.source_status),
        }
