"""Source ABC + the shared scraping toolkit.

Contract every source honours:
  * `fetch(ipos)` NEVER raises. It returns whatever it managed to get and records its own
    failure in `self.failures`. One dead scraper must never kill a run.
  * `segment_coverage` declares which segments the source speaks to. A source is simply
    not asked about segments it does not cover.
  * All network access goes through `http_get`: timeout and retries per config.HTTP, exponential
    backoff, realistic User-Agent — all from config.HTTP.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional, Sequence

import requests
from bs4 import BeautifulSoup, Tag

from ..config import HTTP, NAME_MATCH, STANCE_KEYWORDS
from ..models import IPO, Segment, SourceCall, Stance

log = logging.getLogger(__name__)

_SESSION: Optional[requests.Session] = None
_LAST_HOST_HIT: dict[str, float] = {}


# --------------------------------------------------------------------------------------
# HTTP outcome recorder
# --------------------------------------------------------------------------------------
#
# Downstream consumers need to tell "this host refused us" apart from "this host answered
# and had nothing". Both look like an empty result at the call site, and reporting them as
# the same thing would let a hard block masquerade as "no calls today". http_get records
# what actually happened per host so any source can quote the real reason.


class HttpOutcome:
    """The last thing a host did to us. `blocked` means it refused, not that it was empty."""

    __slots__ = ("url", "status", "error")

    def __init__(self, url: str, status: Optional[int], error: str = "") -> None:
        self.url = url
        self.status = status
        self.error = error

    @property
    def ok(self) -> bool:
        return self.status is not None and self.status < 400

    @property
    def blocked(self) -> bool:
        """Refused, rate-limited or unreachable — as opposed to answered-but-empty."""
        return not self.ok

    @property
    def reason(self) -> str:
        """Short enough to read in a status payload. The full error is in the log."""
        if self.status is not None:
            return f"HTTP {self.status}"
        return f"{self.error} ({_host_of(self.url)})" if self.error else "unreachable"


_LAST_OUTCOME: dict[str, HttpOutcome] = {}


def last_outcome(url_or_host: str) -> Optional[HttpOutcome]:
    """What this host last did. Accepts a full URL or a bare host."""
    host = _host_of(url_or_host) if "://" in url_or_host else url_or_host
    return _LAST_OUTCOME.get(host)


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": str(HTTP["user_agent"]),
                "Accept-Language": str(HTTP["accept_language"]),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Connection": "keep-alive",
            }
        )
        _SESSION = s
    return _SESSION


def _host_of(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url)
    return match.group(1) if match else url


def _be_polite(url: str) -> None:
    host = _host_of(url)
    delay = float(HTTP["polite_delay_seconds"])
    last = _LAST_HOST_HIT.get(host)
    now = time.monotonic()
    if last is not None and now - last < delay:
        time.sleep(delay - (now - last))
    _LAST_HOST_HIT[host] = time.monotonic()


def http_get(url: str, *, params: dict[str, Any] | None = None) -> Optional[requests.Response]:
    """GET with retries + backoff. Returns None instead of raising."""
    attempts = int(HTTP["retries"]) + 1
    backoff = float(HTTP["backoff_base_seconds"])
    host = _host_of(url)
    for attempt in range(1, attempts + 1):
        try:
            _be_polite(url)
            response = _session().get(url, params=params, timeout=float(HTTP["timeout_seconds"]))
            if response.status_code >= 500:
                raise requests.HTTPError(f"HTTP {response.status_code}")
            if response.status_code >= 400:
                log.warning("GET %s -> HTTP %s (not retrying)", url, response.status_code)
                _LAST_OUTCOME[host] = HttpOutcome(url, response.status_code)
                return None
            _LAST_OUTCOME[host] = HttpOutcome(url, response.status_code)
            return response
        except Exception as exc:  # noqa: BLE001 — deliberate: scrapers never raise upward
            if attempt >= attempts:
                log.warning("GET %s failed after %s attempts: %s", url, attempts, exc)
                _LAST_OUTCOME[host] = HttpOutcome(url, None, type(exc).__name__)
                return None
            sleep_for = backoff**attempt
            log.debug("GET %s attempt %s failed (%s); retrying in %.1fs", url, attempt, exc, sleep_for)
            time.sleep(sleep_for)
    return None


def get_soup(url: str, *, params: dict[str, Any] | None = None) -> Optional[BeautifulSoup]:
    response = http_get(url, params=params)
    if response is None:
        return None
    try:
        return BeautifulSoup(response.text, "lxml")
    except Exception:  # noqa: BLE001 — lxml missing/broken markup falls back to stdlib
        try:
            return BeautifulSoup(response.text, "html.parser")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not parse HTML from %s: %s", url, exc)
            return None


# --------------------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")
_DATE_FORMATS: Sequence[str] = (
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d %b, %Y",
    "%d %B, %Y",
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else value.get_text(" ", strip=True)
    text = text.replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_number(value: Any) -> Optional[float]:
    """First number in a blob. Handles '₹1,234.50', '12.34x', '(-)5%'."""
    text = clean_text(value)
    if not text:
        return None
    text = text.replace("(-)", "-")
    match = _NUM_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_int(value: Any) -> Optional[int]:
    number = parse_number(value)
    return int(number) if number is not None else None


def parse_price_band(value: Any) -> tuple[Optional[float], Optional[float]]:
    """'₹100 to ₹105' / '100-105' / '₹95' -> (low, high)."""
    text = clean_text(value)
    if not text:
        return None, None
    numbers = [float(n.replace(",", "")) for n in _NUM_RE.findall(text)]
    numbers = [n for n in numbers if n > 0]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return min(numbers[:2]), max(numbers[:2])


def parse_date_range(value: Any, today: date) -> tuple[Optional[date], Optional[date]]:
    """'5-7 August' / '30-3 August' / '05 - 07 Aug' -> (open, close).

    The cross-month form ("30-3 August" = 30 Jul to 3 Aug) is normal on Indian IPO
    calendars, so a start day greater than the end day rolls the start back a month.
    """
    text = clean_text(value)
    if not text:
        return None, None
    match = re.match(
        r"^(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:([A-Za-z]{3,9})\s*)?[-–—to]+\s*(\d{1,2})\s*"
        r"(?:st|nd|rd|th)?\s*([A-Za-z]{3,9})?\s*,?\s*(\d{4})?$",
        text.strip(),
    )
    if not match:
        single = parse_date(text, default_year=today.year)
        return single, single
    start_day, start_month, end_day, end_month, year = match.groups()
    end_month = end_month or start_month
    if not end_month:
        return None, None
    year_value = int(year) if year else today.year
    close = parse_date(f"{end_day} {end_month} {year_value}")
    if close is None:
        return None, None
    if start_month:
        open_ = parse_date(f"{start_day} {start_month} {year_value}")
    else:
        open_ = close.replace(day=int(start_day)) if int(start_day) <= close.day else None
        if open_ is None:
            month = close.month - 1 or 12
            year_back = close.year - 1 if close.month == 1 else close.year
            try:
                open_ = date(year_back, month, int(start_day))
            except ValueError:
                open_ = None
    # A calendar printed without a year always means the nearest sensible one.
    if close < today - timedelta(days=180):
        close = close.replace(year=close.year + 1)
        if open_:
            open_ = open_.replace(year=open_.year + 1)
    return open_, close


def parse_date(value: Any, *, default_year: Optional[int] = None) -> Optional[date]:
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text, flags=re.IGNORECASE)
    text = text.replace(",", ", ").strip()
    text = re.sub(r"\s+", " ", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # No year in the string ("12 Aug") — assume the reference year.
    if default_year:
        for fmt in ("%d %b", "%d %B", "%b %d", "%B %d"):
            try:
                parsed = datetime.strptime(text, fmt).date()
                return parsed.replace(year=default_year)
            except ValueError:
                continue
    try:
        from dateutil import parser as dateutil_parser  # local import: optional dependency

        # Parsed twice against different defaults: if the two disagree, the string did not
        # actually carry a full date ("2026" would otherwise silently become *today*).
        first = dateutil_parser.parse(
            text, dayfirst=True, fuzzy=True, default=datetime(2000, 1, 1)
        ).date()
        second = dateutil_parser.parse(
            text, dayfirst=True, fuzzy=True, default=datetime(2001, 6, 15)
        ).date()
        if (first.month, first.day) != (second.month, second.day):
            return None
        if first.year != second.year:
            if not default_year:
                return None
            return first.replace(year=default_year)
        return first
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------------------
# Generic HTML table parsing — shared by every table-scraping provider
# --------------------------------------------------------------------------------------

DEFAULT_TABLE_SELECTORS: dict[str, str] = {
    "table": "table",
    "row": "tr",
    "header_cell": "th",
    "cell": "td",
    "link": "a[href]",
    # Decorative nodes inside a cell, removed before any text is read. Boards hang
    # status pills off the company name ("Behari Lal Engineering CT"), and a name that
    # carries one no longer matches the same issue anywhere else — not the ledger, not
    # the GMP feed, not the previous run. Empty by default; each source names its own.
    "drop": "",
}


def _drop_decoration(soup: BeautifulSoup, selector: str) -> None:
    """Remove decorative nodes in place, so cell text is the data and nothing else."""
    if not selector:
        return
    for node in soup.select(selector):
        node.decompose()


def match_header(header: str, header_keys: dict[str, list[str]]) -> Optional[str]:
    """Map a printed column header onto a canonical field. Longest keyword wins."""
    blob = header.lower()
    hits: list[tuple[int, str]] = []
    for field, keywords in header_keys.items():
        for keyword in keywords:
            if keyword in blob:
                hits.append((len(keyword), field))
    return max(hits)[1] if hits else None


def parse_tables(
    soup: BeautifulSoup,
    header_keys: dict[str, list[str]],
    *,
    base_url: str = "",
    required: Sequence[str] = ("name",),
    selectors: Optional[dict[str, str]] = None,
) -> list[list[dict[str, Any]]]:
    """Every matching table as rows of {canonical_field: text, '_link': href, '_table': i}.

    Handles both `<th>` headers and the very common "first row is really the header but
    uses `<td>`" pattern, and never emits that header row as data.
    """
    sel = {**DEFAULT_TABLE_SELECTORS, **(selectors or {})}
    _drop_decoration(soup, sel["drop"])
    tables: list[list[dict[str, Any]]] = []
    for table_index, table in enumerate(soup.select(sel["table"])):
        header_row: Optional[Tag] = None
        headers = [clean_text(th) for th in table.select(sel["header_cell"])]
        if not headers:
            first = table.find(sel["row"])
            if isinstance(first, Tag):
                header_row = first
                headers = [clean_text(td) for td in first.select(sel["cell"])]
        if not headers:
            continue
        mapped = [match_header(h, header_keys) for h in headers]
        # Some boards label the first column with something unmatchable ("IPO", "Issue").
        # If everything else lined up, the leading column is the company name.
        if mapped and mapped[0] is None and "name" not in mapped and len([m for m in mapped if m]) >= 2:
            mapped[0] = "name"
        present = {m for m in mapped if m}
        if not set(required).issubset(present):
            continue
        rows: list[dict[str, Any]] = []
        for tr in table.select(sel["row"]):
            if header_row is not None and tr is header_row:
                continue
            cells = tr.select(sel["cell"])
            if not cells:
                continue
            # A row can collapse its columns into one cell — chittorgarh's dashboard
            # prints "Skyways Air Services 24 - 27 Aug" under a Company / Issue Date
            # header. That is still data: the source's own splitter recovers the dates
            # from the name. Dropping it cost the entire calendar table, which left a
            # dateless recommendation table further down the page to win the name match
            # and produced a calendar where every issue had no dates. Any OTHER
            # single-cell row is a layout row, not data.
            if len(cells) < 2 and mapped[:1] != ["name"]:
                continue
            row: dict[str, Any] = {"_table": table_index}
            for index, cell in enumerate(cells):
                if index >= len(mapped):
                    break
                field = mapped[index]
                if field:
                    row.setdefault(field, clean_text(cell))
            link = tr.select_one(sel["link"])
            if link is not None and link.get("href"):
                href = str(link["href"])
                row["_link"] = href if href.startswith("http") else (base_url and _join(base_url, href))
            name = clean_text(row.get("name"))
            if not name or match_header(name, header_keys) == "name":
                continue  # header row masquerading as data
            rows.append(row)
        if rows:
            tables.append(rows)
    return tables


def _join(base_url: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base_url, href)


def label_value_pairs(soup: BeautifulSoup, *, selectors: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Detail pages are two-column label/value tables. First value for a label wins."""
    sel = {**DEFAULT_TABLE_SELECTORS, **(selectors or {})}
    pairs: dict[str, str] = {}
    for table in soup.select(sel["table"]):
        for tr in table.select(sel["row"]):
            cells = tr.select(sel["cell"])
            if len(cells) < 2:
                continue
            label = clean_text(cells[0]).lower().rstrip(":").strip()
            value = clean_text(cells[1])
            if label and value and label not in pairs:
                pairs[label] = value
    return pairs


def lookup(pairs: dict[str, str], keywords: Sequence[str]) -> Optional[str]:
    for keyword in keywords:
        for label, value in pairs.items():
            if keyword in label:
                return value
    return None


# --------------------------------------------------------------------------------------
# IPO name matching — the calendar is the spine, everyone else matches onto it
# --------------------------------------------------------------------------------------


def normalise_name(name: str) -> str:
    text = clean_text(name).lower()
    for ch in str(NAME_MATCH["strip_chars"]):
        text = text.replace(ch, " ")
    tokens = [t for t in text.split() if t]
    suffixes = set(NAME_MATCH["strip_suffixes"])  # type: ignore[arg-type]
    tokens = [t for t in tokens if t not in suffixes]
    return " ".join(tokens).strip()


def name_similarity(a: str, b: str) -> float:
    left, right = normalise_name(a), normalise_name(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    # Containment counts as a strong match ("Acme Solar" vs "Acme Solar Energy").
    if left in right or right in left:
        shorter, longer = sorted((left, right), key=len)
        if len(shorter) >= 5:
            return max(0.92, len(shorter) / len(longer))
    return SequenceMatcher(None, left, right).ratio()


def match_ipo(name: str, ipos: Iterable[IPO], *, min_ratio: Optional[float] = None) -> Optional[IPO]:
    """Best fuzzy match of a free-text company name onto the calendar."""
    threshold = float(NAME_MATCH["min_ratio"]) if min_ratio is None else min_ratio
    best: Optional[IPO] = None
    best_score = 0.0
    for ipo in ipos:
        score = name_similarity(name, ipo.name)
        if score > best_score:
            best, best_score = ipo, score
    if best is not None and best_score >= threshold:
        return best
    return None


# --------------------------------------------------------------------------------------
# Stance classification — shared vocabulary from config
# --------------------------------------------------------------------------------------


def classify_stance_verbatim(text: str) -> tuple[Stance, str]:
    """Map free text onto a Stance, and hand back the phrase AS PUBLISHED that decided it.

    The verbatim phrase matters downstream: consumers of the expert feed grade on what the
    person actually said, and normalising (say) "book profit" into "sell" would erase a
    distinction someone trades on. `Stance` is our reading; the second element is theirs.
    """
    cleaned = clean_text(text)
    blob = cleaned.lower()  # same length as `cleaned`, so indices map 1:1
    if not blob:
        return Stance.NO_VIEW, ""
    for stance_key in ("AVOID", "APPLY_LISTING_GAINS", "SUBSCRIBE_LONG_TERM", "NEUTRAL", "APPLY"):
        for phrase in STANCE_KEYWORDS.get(stance_key, []):
            index = blob.find(phrase)
            if index >= 0:
                # "do not subscribe" must not be read as "subscribe"
                if stance_key == "APPLY" and re.search(r"(not|avoid|n't)\s+\w*\s*subscrib", blob):
                    continue
                return Stance(stance_key), cleaned[index : index + len(phrase)]
    return Stance.NO_VIEW, ""


def classify_stance(text: str) -> Stance:
    """Map free text onto a Stance. Order matters: most specific phrases win."""
    return classify_stance_verbatim(text)[0]


def snippet(text: str, limit: int = 180) -> str:
    cleaned = clean_text(text)
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"


# --------------------------------------------------------------------------------------
# Source ABC
# --------------------------------------------------------------------------------------


class Source(ABC):
    """Every scraper implements this. Adding a broker/data provider = one new subclass."""

    #: Stable identity used in logs and the `sources_failed` list.
    name: str = "source"
    #: Which segments this source has anything to say about.
    segment_coverage: tuple[Segment, ...] = (Segment.MAINBOARD, Segment.SME)

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.log = logging.getLogger(f"pravesh.source.{self.name}")

    # -- lifecycle ---------------------------------------------------------------------

    @abstractmethod
    def fetch(self, ipos: Sequence[IPO]) -> list[SourceCall]:
        """Return source calls for the given calendar. Must not raise."""

    def run(self, ipos: Sequence[IPO]) -> list[SourceCall]:
        """Safety net around `fetch` so a source can never break the pipeline."""
        try:
            calls = self.fetch(ipos)
        except Exception as exc:  # noqa: BLE001 — graceful degradation is the whole point
            self.log.exception("unhandled error in %s", self.name)
            self.fail(f"{self.name}: unhandled {type(exc).__name__}")
            return []
        self.log.info("%s produced %d call(s)", self.name, len(calls))
        return calls

    # -- helpers for subclasses --------------------------------------------------------

    def covers(self, ipo: IPO) -> bool:
        return ipo.segment in self.segment_coverage

    def relevant(self, ipos: Sequence[IPO]) -> list[IPO]:
        return [i for i in ipos if self.covers(i)]

    def fail(self, reason: str) -> None:
        self.log.warning("degraded: %s", reason)
        if reason not in self.failures:
            self.failures.append(reason)

    def first_working_soup(self, urls: Sequence[str], **fmt: Any) -> tuple[Optional[BeautifulSoup], Optional[str]]:
        """Try each configured URL in order; return the first that parses."""
        for template in urls:
            url = template.format(**fmt) if fmt else template
            soup = get_soup(url)
            if soup is not None:
                return soup, url
        return None, None

    def first_url_with_rows(
        self,
        urls: Sequence[str],
        header_keys: dict[str, list[str]],
        *,
        required: Sequence[str] = ("name",),
        selectors: Optional[dict[str, str]] = None,
    ) -> tuple[list[list[dict[str, Any]]], Optional[str]]:
        """Try each URL until one yields actual rows.

        A page that returns HTTP 200 with a client-rendered (empty) table is a failure,
        not a success — this is what keeps a JS rewrite on one site from silently
        emptying the whole run.
        """
        for url in urls:
            soup = get_soup(url)
            if soup is None:
                continue
            tables = parse_tables(
                soup, header_keys, base_url=url, required=required, selectors=selectors
            )
            if any(tables):
                return tables, url
            self.log.debug("no rows at %s (client-rendered or markup changed)", url)
        return [], None


class DataProvider(Source):
    """A source of facts rather than opinions: calendar, detail strip, subscription, outcomes.

    Providers are tried in the order listed in config.DATA_PROVIDERS, so a site going
    client-side (as chittorgarh.com did) degrades to the next provider instead of
    emptying the run. Every method is optional — the base returns "nothing".
    """

    def fetch(self, ipos: Sequence[IPO]) -> list[SourceCall]:
        """Data providers hold no opinions; their signals surface via qib_signal/gmp."""
        return []

    def fetch_calendar(self, today: date) -> list[IPO]:
        return []

    def enrich_details(self, ipos: Sequence[IPO], today: date) -> None:
        return None

    def enrich_subscription(self, ipos: Sequence[IPO]) -> int:
        """Returns how many IPOs got live bidding data."""
        return 0

    def fetch_listing_outcomes(self, ipos: Sequence[IPO]) -> dict[str, dict[str, Optional[float]]]:
        return {}
