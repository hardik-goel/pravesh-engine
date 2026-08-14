"""Single source of truth for every tunable in Pravesh.

Nothing in this repo should hard-code a weight, threshold, URL, timeout, country or
exchange assumption. If you find one outside this file, it is a bug.

The engine is designed to be white-labelled: change MARKET, SOURCES_ENABLED and the
BRAND_* values and the same pipeline runs for a different exchange.
"""

from __future__ import annotations

import os
from typing import Final

# --------------------------------------------------------------------------------------
# Brand / market
# --------------------------------------------------------------------------------------

BRAND_NAME: Final[str] = "Trinetra Pravesh"
BRAND_TAGLINE: Final[str] = "IPO intelligence — evidence first."
PRODUCT_DISCLAIMER: Final[str] = (
    "Informational only. Not investment advice. Not a recommendation to buy or sell any "
    "security. Do your own research — the final call is yours."
)

# Market assumptions live here so the engine can be repointed at another exchange.
MARKET: Final[dict[str, object]] = {
    "country": "IN",
    "currency_symbol": "₹",
    "currency_code": "INR",
    "size_unit": "Cr",  # issue sizes are quoted in crore
    "timezone": "Asia/Kolkata",
    "tz_label": "IST",
    "segments": ["MAINBOARD", "SME"],
    "trading_days": [0, 1, 2, 3, 4],  # Mon-Fri, used for "closing tomorrow" logic
}

# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------

HTTP: Final[dict[str, object]] = {
    # ipowatch.in — the calendar spine — intermittently connect-times-out from GitHub's
    # runners: three 10s attempts failed at 04:04 on 2026-08-14 and the same host answered
    # fine fifty minutes later, and again for the GMP page inside the failing run. The
    # cost of one slow attempt is seconds; the cost of losing the spine is the whole
    # report, so this waits rather than gives up. A run is ~3 min against the workflow's
    # 25 min timeout-minutes, so the extra patience is affordable.
    "timeout_seconds": 25,
    "retries": 3,  # total attempts = retries + 1
    "backoff_base_seconds": 1.5,  # attempt N sleeps backoff_base ** N
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "accept_language": "en-IN,en;q=0.9",
    "polite_delay_seconds": 0.8,  # between requests to the same host
}

# --------------------------------------------------------------------------------------
# Source endpoints — swap a broken host here, never in the scraper body
# --------------------------------------------------------------------------------------

SOURCE_URLS: Final[dict[str, list[str]]] = {
    "chittorgarh_mainboard": [
        "https://www.chittorgarh.com/report/mainboard-ipo-list-in-india-bse-nse/83/",
        "https://www.chittorgarh.com/ipo/ipo_dashboard.asp",
    ],
    "chittorgarh_sme": [
        "https://www.chittorgarh.com/report/sme-ipo-list-in-india-bse-sme-nse-emerge/84/",
    ],
    "chittorgarh_subscription": [
        "https://www.chittorgarh.com/report/live-ipo-subscription-status-bidding-detail/21/",
    ],
    "chittorgarh_listing": [
        "https://www.chittorgarh.com/report/ipo-listing-day-gain-loss-performance/25/",
    ],
    "ipowatch_calendar": [
        "https://ipowatch.in/upcoming-ipo-calendar-ipo-list/",
    ],
    "ipowatch_listing": [
        "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/",
    ],
    "gmp_primary": [
        "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/",
    ],
    "gmp_fallback": [
        "https://www.investorgain.com/report/live-ipo-gmp/331/",
    ],
    "singhvi_search": [
        "https://www.zeebiz.com/search?q={query}",
    ],
    "singhvi_fallback_search": [
        "https://html.duckduckgo.com/html/?q={query}",
    ],
    # Sandeep Jain appears on the same network, but the endpoints are listed per source so a
    # host that breaks for one expert can be repointed without touching the other.
    "sandeep_jain_search": [
        "https://www.zeebiz.com/search?q={query}",
    ],
    "sandeep_jain_fallback_search": [
        "https://html.duckduckgo.com/html/?q={query}",
    ],
    "broker_roundups": [
        "https://www.moneycontrol.com/news/tags/ipo.html",
        "https://economictimes.indiatimes.com/markets/ipos/fpos",
        "https://www.livemint.com/market/ipo",
    ],
    "broker_search_fallback": [
        "https://html.duckduckgo.com/html/?q={query}",
    ],
    # Google News RSS: server-rendered XML, not IP-blocked, carries headline + publisher.
    "news_search": [
        "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en",
    ],
    # Publisher topic pages: server-rendered and give DIRECT article URLs, which is what
    # the broker extractor needs (Google News links are un-dereferenceable wrappers).
    "topic_search": [
        "https://economictimes.indiatimes.com/topic/{slug}",
        "https://www.moneycontrol.com/news/tags/{slug}.html",
        "https://www.livemint.com/topic/{slug}",
    ],
}

# Which opinion sources run. Comment one out and the engine degrades gracefully around it.
SOURCES_ENABLED: Final[list[str]] = [
    "gmp",
    "qib_signal",
    "singhvi",
    "sandeep_jain",
    "brokers",
]

# Fact providers, tried in order until one returns data. This is the whole answer to
# "a site went client-side" — reorder the list, do not rewrite the engine.
#
# As of 2026-07-31: chittorgarh.com's report pages are a client-rendered Next.js app and
# return no rows, so ipowatch leads the calendar and NSE's own API leads subscription.
DATA_PROVIDERS: Final[dict[str, list[str]]] = {
    "calendar": ["ipowatch", "chittorgarh"],
    "detail": ["ipowatch", "chittorgarh"],
    "subscription": ["nse_subscription", "chittorgarh"],
    "listing": ["ipowatch", "chittorgarh"],
}

# --------------------------------------------------------------------------------------
# Canonical source identities (accuracy ledger keys)
# --------------------------------------------------------------------------------------

SOURCE_GMP: Final[str] = "GMP signal"
SOURCE_QIB: Final[str] = "QIB signal"
SOURCE_SINGHVI: Final[str] = "Anil Singhvi"
SOURCE_SANDEEP_JAIN: Final[str] = "Sandeep Jain"

# Named market experts, as opposed to brokerage houses. They sit at the top of the evidence
# table, are pooled separately from the broker consensus, and each carries its own veto.
# Adding a third expert is: one identity above, one entry here, one weight in BASE_WEIGHTS,
# one scraper, one line in sources.REGISTRY.
EXPERT_SOURCES: Final[tuple[str, ...]] = (SOURCE_SINGHVI, SOURCE_SANDEEP_JAIN)

# Short forms for the one place space is charged by the character (the Telegram one-liner).
# "Jain" alone would be ambiguous in this market, so his stays full.
EXPERT_SHORT_NAMES: Final[dict[str, str]] = {
    SOURCE_SINGHVI: "Singhvi",
    SOURCE_SANDEEP_JAIN: "Sandeep Jain",
}

# Alias map — every variant collapses to one ledger identity.
BROKER_ALIASES: Final[dict[str, str]] = {
    "anand rathi": "Anand Rathi",
    "anand rathi research": "Anand Rathi",
    "anand rathi share and stock brokers": "Anand Rathi",
    "sbi securities": "SBI Securities",
    "sbicap securities": "SBI Securities",
    "sbicaps": "SBI Securities",
    "ventura": "Ventura Securities",
    "ventura securities": "Ventura Securities",
    "swastika": "Swastika Investmart",
    "swastika investmart": "Swastika Investmart",
    "choice": "Choice Broking",
    "choice broking": "Choice Broking",
    "choice equity broking": "Choice Broking",
    "reliance securities": "Reliance Securities",
    "bp equities": "BP Equities",
    "bp wealth": "BP Equities",
    "canara bank securities": "Canara Bank Securities",
    "marwadi": "Marwadi Financial Services",
    "marwadi financial services": "Marwadi Financial Services",
    "marwadi shares": "Marwadi Financial Services",
    "geojit": "Geojit Financial Services",
    "geojit financial services": "Geojit Financial Services",
    "nirmal bang": "Nirmal Bang",
    "arihant capital": "Arihant Capital",
    "hem securities": "Hem Securities",
    "indsec": "Indsec Securities",
    "master capital": "Master Capital Services",
    "master capital services": "Master Capital Services",
    "stoxbox": "StoxBox",
    "bajaj broking": "Bajaj Broking",
    "angel one": "Angel One",
    "motilal oswal": "Motilal Oswal",
    "icici direct": "ICICI Direct",
    "icicidirect": "ICICI Direct",
    "kotak securities": "Kotak Securities",
    "hdfc securities": "HDFC Securities",
    "axis capital": "Axis Capital",
    "sharekhan": "Sharekhan",
    "religare broking": "Religare Broking",
    "lkp securities": "LKP Securities",
    "sushil finance": "Sushil Finance",
    "sbi cap": "SBI Securities",
    "elara capital": "Elara Capital",
    "systematix": "Systematix",
    "mehta equities": "Mehta Equities",
    "aum capital": "AUM Capital",
    "deven choksey": "DR Choksey FinServ",
    "dr choksey finserv": "DR Choksey FinServ",
}

# Names that look like brokers in text but aren't (guards the extractor).
BROKER_NAME_BLOCKLIST: Final[set[str]] = {
    "the company",
    "the issue",
    "the ipo",
    "grey market",
    "bse",
    "nse",
    "sebi",
    "moneycontrol",
    "economic times",
    "livemint",
    "zee business",
}

# --------------------------------------------------------------------------------------
# Stance classification vocabulary (shared by singhvi.py and brokers.py)
# --------------------------------------------------------------------------------------

STANCE_KEYWORDS: Final[dict[str, list[str]]] = {
    "AVOID": [
        "avoid",
        "do not subscribe",
        "don't subscribe",
        "not subscribe",
        "stay away",
        "skip the ipo",
        "negative view",
    ],
    "APPLY_LISTING_GAINS": [
        "subscribe for listing gain",
        "subscribe for listing gains",
        "apply for listing gain",
        "apply for listing gains",
        "listing gain only",
        "listing gains only",
        "short term gains",
        "only for listing",
    ],
    "SUBSCRIBE_LONG_TERM": [
        "subscribe for long term",
        "subscribe with a long term",
        "long term investors",
        "long-term investors",
        "long term view",
    ],
    "APPLY": [
        "subscribe",
        "apply",
        "positive view",
        "recommend the issue",
    ],
    "NEUTRAL": [
        "neutral",
        "may subscribe",
        "risk takers",
        "high risk appetite",
        "wait and watch",
    ],
}

# --------------------------------------------------------------------------------------
# Signal thresholds
# --------------------------------------------------------------------------------------

THRESHOLDS: Final[dict[str, float]] = {
    # Synthetic-source trigger levels
    "gmp_apply_pct": 15.0,  # GMP >= 15% of upper band => implied Apply
    "gmp_neutral_pct": 5.0,  # 5-15% => Neutral, below => implied Avoid
    "qib_apply_x": 10.0,  # QIB >= 10x => implied Apply
    "qib_neutral_x": 3.0,  # 3-10x => Neutral, below => implied Avoid
    # Outcome resolution
    "listing_gain_success_pct": 5.0,  # apply-type correct if listing gain >= +5%
    # Score inputs
    "qib_full_credit_x": 20.0,  # QIB subscription that earns full QIB points
    "gmp_full_credit_pct": 40.0,  # GMP % that earns full GMP points
    "total_sub_full_credit_x": 25.0,  # total subscription that earns full points
    "ofs_heavy_pct": 75.0,  # OFS share above which we penalise
}

# --------------------------------------------------------------------------------------
# "My take" scoring
# --------------------------------------------------------------------------------------

# Base weights. Must sum to 100 for readability; the engine renormalises anyway.
#
# The named-expert allocation is 15 in total and does not grow when an expert is added —
# Singhvi and Sandeep Jain SPLIT the old 15, they do not each get it. One more voice on the
# panel must not mean more influence for personal opinion over the hard numbers.
BASE_WEIGHTS: Final[dict[str, float]] = {
    "qib": 30.0,
    "gmp": 25.0,
    "total_subscription": 15.0,
    "singhvi": 7.5,
    "sandeep_jain": 7.5,
    "brokers": 15.0,
}

#: Total base weight the named experts may hold between them. Asserted at import.
EXPERT_WEIGHT_BUDGET: Final[float] = 15.0

# Which ledger identity calibrates which weight. None => never calibrated.
# Each expert calibrates independently once they clear CALIBRATION["min_resolved_calls"],
# so a good record lifts one without lifting the other.
WEIGHT_CALIBRATION_SOURCE: Final[dict[str, str | None]] = {
    "qib": SOURCE_QIB,
    "gmp": SOURCE_GMP,
    "total_subscription": None,
    "singhvi": SOURCE_SINGHVI,
    "sandeep_jain": SOURCE_SANDEEP_JAIN,
    "brokers": "__broker_consensus__",  # aggregate of all broker identities
}

#: weight key -> ledger identity, for the expert weights only. Used by take.py.
EXPERT_WEIGHT_KEYS: Final[dict[str, str]] = {
    "singhvi": SOURCE_SINGHVI,
    "sandeep_jain": SOURCE_SANDEEP_JAIN,
}

assert (
    abs(sum(BASE_WEIGHTS[k] for k in EXPERT_WEIGHT_KEYS) - EXPERT_WEIGHT_BUDGET) < 1e-9
), "named experts must SPLIT EXPERT_WEIGHT_BUDGET, not each be given it"

CALIBRATION: Final[dict[str, float]] = {
    "min_resolved_calls": 10,  # n >= 10 before a weight is scaled
    "baseline_accuracy_pct": 60.0,  # accuracy / baseline => multiplier
    "multiplier_min": 0.5,
    "multiplier_max": 1.5,
}

SCORE_MODIFIERS: Final[dict[str, float]] = {
    "ofs_heavy_penalty": -10.0,
    "sme_penalty": -10.0,
    "sme_apply_override_score": 80.0,  # SME capped at RISKY unless score >= this
}

# Verdict bands, evaluated top-down on the final score.
VERDICT_BANDS: Final[list[dict[str, object]]] = [
    {"min": 70.0, "key": "APPLY", "label": "APPLY", "emoji": "🟢", "color": "#1E7A4B"},
    {"min": 50.0, "key": "LISTING_GAINS", "label": "LISTING GAINS ONLY", "emoji": "🔵", "color": "#1F5F9E"},
    {"min": 35.0, "key": "RISKY", "label": "RISKY — high risk appetite only", "emoji": "🟡", "color": "#9A7407"},
    {"min": 0.0, "key": "AVOID", "label": "AVOID", "emoji": "🔴", "color": "#9E2B25"},
]

PRELIMINARY_LABEL: Final[str] = "PRELIMINARY"
PRELIMINARY_EMOJI: Final[str] = "⚪"

# When literally nothing has landed yet we refuse to print a score — a 0/100 on an issue
# nobody has data for reads as "AVOID", which would be a lie.
NO_EVIDENCE: Final[dict[str, str]] = {
    "key": "PRELIMINARY",
    "label": "PRELIMINARY — no evidence yet",
    "emoji": "⚪",
    "color": "#6B6B63",
}

# Expert vetoes: never touch the score, always render as a banner above My Take.
#
# Parity is deliberate — both named experts get the same treatment, in declaration order.
# A veto is a warning to the reader, not a secret adjustment to the number.
EXPERT_VETOES: Final[list[dict[str, object]]] = [
    {
        "source": SOURCE_SINGHVI,
        "enabled": True,
        "trigger_stance": "AVOID",
        "banner_text": "⚠ Anil Singhvi says AVOID",
        "affects_score": False,
    },
    {
        "source": SOURCE_SANDEEP_JAIN,
        "enabled": True,
        "trigger_stance": "AVOID",
        "banner_text": "⚠ Sandeep Jain says AVOID",
        "affects_score": False,
    },
]

# Two independent experts against the same issue is a materially stronger signal than one,
# and the banner has to say so rather than leaving the reader to notice two stacked lines.
BOTH_EXPERTS_VETO: Final[dict[str, object]] = {
    "enabled": True,
    "banner_text": (
        "⚠⚠ BOTH Anil Singhvi and Sandeep Jain say AVOID — two independent experts "
        "against this issue"
    ),
    "replaces_individual_banners": True,
    "affects_score": False,
}

TAKE_CLOSER: Final[str] = "Final call is yours."

# --------------------------------------------------------------------------------------
# Accuracy ledger display
# --------------------------------------------------------------------------------------

ACCURACY: Final[dict[str, object]] = {
    "min_n_for_percentage": 5,  # below this, show "insufficient history"
    "recent_window": 15,  # "last-15" rolling accuracy
    "insufficient_label": "insufficient history",
    "leaderboard_size": 5,
    "leaderboard_min_n": 5,
}

# --------------------------------------------------------------------------------------
# IPO name matching (chittorgarh calendar is the spine; every other source matches to it)
# --------------------------------------------------------------------------------------

NAME_MATCH: Final[dict[str, object]] = {
    "min_ratio": 0.80,  # difflib ratio below which we refuse the match
    "strip_suffixes": [
        "limited",
        "ltd",
        "ltd.",
        "pvt",
        "private",
        "india",
        "ipo",
        "sme ipo",
        "mainboard ipo",
        "industries",
        "enterprises",
        "corporation",
        "corp",
        "company",
        "co",
        "&",
        "and",
    ],
    "strip_chars": ".,'\"()[]-–—/",
}

# --------------------------------------------------------------------------------------
# Reporting windows
# --------------------------------------------------------------------------------------

WINDOWS: Final[dict[str, int]] = {
    "opening_this_week_days": 7,  # upcoming IPOs shown as PRELIMINARY
    "allotment_watch_days": 7,  # allotment/listing lookahead
    "listing_resolution_lookback_days": 45,  # how far back we chase listing prices
    "history_days": 180,  # history returned in latest.json / web tab
}

# --------------------------------------------------------------------------------------
# Run slots — the same weekday can produce more than one report
# --------------------------------------------------------------------------------------

# Subscription multiples and GMP move a lot between the open and mid-afternoon, so an open
# IPO is worth reading twice. Every surface says which run it is looking at, because "3.2x"
# means something different at 09:00 than at 15:00.
#
# The slot arrives as PRAVESH_SLOT, set by whoever dispatched the run. Declaration order is
# chronological order: a run's delta baseline is the most recent EARLIER slot of the same
# trading day, so adding a midday slot is an edit here and nothing else.
SLOT_ENV: Final[str] = "PRAVESH_SLOT"
DEFAULT_SLOT: Final[str] = "manual"

RUN_SLOTS: Final[dict[str, dict[str, str]]] = {
    "morning": {"label": "morning report", "since": "since this morning"},
    "afternoon": {"label": "afternoon update", "since": "since this afternoon"},
    "manual": {"label": "manual run", "since": "since the last run"},
}

SLOT_HEADLINE: Final[dict[str, str]] = {
    "template": "{date} · {time} {tz} · {label}",
    "date_format": "%d %b",
    "time_format": "%H:%M",
}

# What counts as "moved" between two runs of the same day. Below these the number is
# rounding noise, and printing it would manufacture a story out of nothing.
DELTA: Final[dict[str, object]] = {
    "subscription_min_change_x": 0.05,
    "gmp_min_change_pct": 0.5,
    "max_parts": 4,  # per IPO, most material first — the full numbers are in the strip
    "now_open_label": "now open",
    "now_closing_label": "now closing",
}

# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------

# ONE-LINE SWITCH: "json" (default, files in data/) or "supabase".
STORE_BACKEND: Final[str] = os.getenv("PRAVESH_STORE", "json")

JSON_STORE: Final[dict[str, str]] = {
    "data_dir": "data",
    "verdicts_file": "verdicts.json",
    "source_calls_file": "source_calls.json",
    "latest_file": "latest.json",
}

# Every run's payload is archived under its (date, slot) so the afternoon update can say
# what moved, and so the Track Record view reads a real history instead of one snapshot.
HISTORY_STORE: Final[dict[str, object]] = {
    "dir": "history",  # under JSON_STORE["data_dir"]
    "filename": "{run_date}-{slot}.json",
    "retain_days": 90,
    "supabase_key_prefix": "run:",  # rows in the latest table: run:2026-08-03:afternoon
}

SUPABASE: Final[dict[str, str]] = {
    "url_env": "SUPABASE_URL",
    "key_env": "SUPABASE_SERVICE_KEY",
    "verdicts_table": os.getenv("PRAVESH_VERDICTS_TABLE", "pravesh_verdicts"),
    "source_calls_table": os.getenv("PRAVESH_SOURCE_CALLS_TABLE", "pravesh_source_calls"),
    "latest_table": os.getenv("PRAVESH_LATEST_TABLE", "pravesh_latest"),
    "latest_row_key": "current",
}

# --------------------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------------------

EMAIL: Final[dict[str, object]] = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_timeout_seconds": 30,
    "use_starttls": True,
    "app_password_length": 16,  # what Google issues — a different length means a wrong secret
    "address_env": "GMAIL_ADDRESS",
    "password_env": "GMAIL_APP_PASSWORD",
    "recipient_env": "RECIPIENT_EMAIL",
    "sender_name": BRAND_NAME,
    # {headline} is "03 Aug · 15:04 IST · afternoon update" — which run this is, always.
    "subject_template": "{brand} · {headline} · {open_count} open · {closing_count} closing soon",
    "subject_urgent_prefix": "⚡ ",
    "quiet_subject_template": "{brand} · {headline} · all quiet",
    "quiet_body": "No open, closing or listing IPOs today. Nothing to act on.",
    "dry_run_path": "out/pravesh_preview.html",
}

TELEGRAM: Final[dict[str, object]] = {
    "api_base": "https://api.telegram.org",
    "token_env": "TELEGRAM_BOT_TOKEN",  # shared with the Trinetra stock bot
    "chat_env": "TELEGRAM_CHAT_ID",
    "max_chars": 4096,
    # Telegram's hard ceiling is 4096; we start dropping the least urgent IPO lines well
    # before that so the message ends on a clean "…+N more", never mid-word.
    "soft_max_chars": 4000,
    "parse_mode": "HTML",
    "disable_web_page_preview": True,
    "closing_tomorrow_prefix": "⚡ ",
    "footer": "Full report in your inbox.",
}

# Email palette — light body, near-black header (dark mode in mail clients is a minefield).
PALETTE: Final[dict[str, str]] = {
    "page_bg": "#F4F3EF",
    "card_bg": "#FFFFFF",
    "header_bg": "#131311",
    "header_fg": "#F4F3EF",
    "accent": "#C9A961",  # aged brass — shared with trinetra-web
    "text": "#1A1A17",
    "muted": "#6B6B63",
    "hairline": "#E2E0D8",
    "danger_bg": "#FBE9E7",
    "danger_fg": "#9E2B25",
    "sme_bg": "#FDF6E3",
    "sme_fg": "#7A5B00",
}

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

LOGGING: Final[dict[str, str]] = {
    "level": os.getenv("PRAVESH_LOG_LEVEL", "INFO"),
    "format": "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
    "datefmt": "%Y-%m-%d %H:%M:%S",
}
