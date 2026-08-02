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
    "timeout_seconds": 10,
    "retries": 2,  # total attempts = retries + 1
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
BASE_WEIGHTS: Final[dict[str, float]] = {
    "qib": 30.0,
    "gmp": 25.0,
    "total_subscription": 15.0,
    "singhvi": 15.0,
    "brokers": 15.0,
}

# Which ledger identity calibrates which weight. None => never calibrated.
WEIGHT_CALIBRATION_SOURCE: Final[dict[str, str | None]] = {
    "qib": SOURCE_QIB,
    "gmp": SOURCE_GMP,
    "total_subscription": None,
    "singhvi": SOURCE_SINGHVI,
    "brokers": "__broker_consensus__",  # aggregate of all broker identities
}

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

# Singhvi veto: never touches the score, always renders as a banner.
SINGHVI_VETO: Final[dict[str, object]] = {
    "enabled": True,
    "trigger_stance": "AVOID",
    "banner_text": "⚠ Anil Singhvi says AVOID",
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
    "subject_template": "{brand} · {date} · {open_count} open · {closing_count} closing soon",
    "subject_urgent_prefix": "⚡ ",
    "quiet_subject_template": "{brand} · {date} · all quiet",
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
