# Trinetra Pravesh — IPO intelligence engine

**Evidence first, not verdict first.** For every live IPO, Pravesh builds a table of named
sources — Anil Singhvi, each brokerage that went on record, plus two synthetic signals
(GMP, QIB) — showing what each said *and how often that source has actually been right*.
Only then does it give **My Take**: a scored, reasoned opinion that names the strongest
evidence for and against, and always ends with **"Final call is yours."**

Pravesh is a standalone Python service in the Trinetra family. It does not live inside the
Node backend (that handles live stock data). It shares exactly two things with Trinetra:
the same Telegram bot token/channel, and the visual language of the web tab.

```
9:00 IST, weekdays  →  scrape  →  evidence table  →  my take  →  email + telegram
                                        ↓
                             data/*.json  →  trinetra-web "Pravesh" tab
```

---

## Architecture

```mermaid
flowchart TD
    subgraph providers["Fact providers (config.DATA_PROVIDERS, tried in order)"]
        IW[ipowatch.py<br/>calendar · detail · listing outcomes]
        CG[chittorgarh.py<br/>calendar · detail · subscription]
        NSE[nse_subscription.py<br/>live QIB/NII/Retail via NSE API]
    end

    subgraph opinions["Opinion sources (config.SOURCES_ENABLED)"]
        SI[singhvi.py<br/>Zee Business · DuckDuckGo fallback]
        BR[brokers.py<br/>Moneycontrol / ET / Livemint roundups]
        GM[gmp.py<br/>synthetic 'GMP signal']
        QI[qib_signal.py<br/>synthetic 'QIB signal']
    end

    providers -->|IPO calendar = the spine| UNI[reporting universe]
    UNI --> opinions
    opinions -->|SourceCall| LED[(engine/source_tracker.py<br/>accuracy ledger)]
    LED --> EV[engine/evidence.py<br/>evidence table]
    EV --> TK[engine/take.py<br/>weighted, self-calibrating score]
    LED -->|weights ×accuracy| TK
    TK --> RES[RunResult]
    EV --> RES
    RES --> ST[(store/<br/>JsonStore · SupabaseStore)]
    RES --> EM[report/email_builder.py]
    RES --> TG[report/telegram_ping.py]
    ST --> WEB[data/latest.json → trinetra-web Pravesh tab]
```

Everything tunable — weights, thresholds, source lists, URLs, provider order, store
choice, market/exchange assumptions — lives in **`src/config.py`**. Adding a brokerage or
swapping a data source is one file plus one registry line, never a rewrite.

---

## Repo layout

```
pravesh-engine/
├── .github/workflows/pravesh-daily.yml
├── src/
│   ├── main.py                    orchestrator + --dry-run
│   ├── config.py                  ALL tunables
│   ├── models.py                  IPO · SourceCall · Evidence · Take · RunResult
│   ├── clock.py                   market-timezone helpers (IST by default)
│   ├── sources/
│   │   ├── base.py                Source + DataProvider ABCs, HTTP, table/date parsing
│   │   ├── ipowatch.py            calendar spine · detail strip · listing outcomes
│   │   ├── chittorgarh.py         same roles (fallback — see "When scrapers break")
│   │   ├── nse_subscription.py    live QIB/NII/Retail/Total from NSE's own API
│   │   ├── search.py              discovery chain: Google News RSS · topic pages · DDG
│   │   ├── gmp.py                 GMP + synthetic "GMP signal"
│   │   ├── qib_signal.py          synthetic "QIB signal"
│   │   ├── singhvi.py             Anil Singhvi (Zee Business)
│   │   └── brokers.py             named brokerages from roundups
│   ├── engine/
│   │   ├── evidence.py            per-IPO evidence table
│   │   ├── take.py                self-calibrating score + reasoned paragraph
│   │   └── source_tracker.py      accuracy ledger + leaderboard
│   ├── store/
│   │   ├── base.py                Store ABC + merge rules + factory
│   │   ├── json_store.py          DEFAULT — data/*.json
│   │   └── supabase_store.py      drop-in upgrade
│   └── report/
│       ├── email_builder.py       inline-CSS HTML + Gmail SMTP
│       └── telegram_ping.py       ≤4096-char ping
├── data/
│   ├── verdicts.json              my-take history + listing outcomes
│   ├── source_calls.json          every source call + its outcome
│   └── latest.json                the web contract
└── requirements.txt
```

Four files are additions to the original spec, for reasons documented below:
`ipowatch.py` / `nse_subscription.py` (chittorgarh.com went client-side),
`search.py` (zeebiz.com and DuckDuckGo block this crawler, so discovery needed more than
one route) and `clock.py` (so the IST assumption lives in exactly one place). All four are
plain `Source` / `DataProvider` implementations wired through `config.py` — see
"When scrapers break".

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.main --dry-run      # writes out/pravesh_preview.html, prints the ping, sends nothing
python -m src.main                # the real thing
```

Useful flags: `--no-email`, `--no-telegram`, `--store json|supabase`, `--date YYYY-MM-DD`
(backfill), `-v` (debug logging).

### Gmail app password

1. Enable 2-Step Verification on the sending Google account.
2. <https://myaccount.google.com/apppasswords> → create an app password ("Pravesh").
3. Use that 16-character string as `GMAIL_APP_PASSWORD` — never the account password.

### Secrets (Settings → Secrets and variables → Actions → Secrets)

| Secret | Required | What it is |
|---|---|---|
| `GMAIL_ADDRESS` | yes | Sending Gmail address |
| `GMAIL_APP_PASSWORD` | yes | 16-char app password from above (spaces are stripped, so paste it as Google shows it) |
| `RECIPIENT_EMAIL` | yes | Recipient(s) — one address, or many separated by commas, semicolons, spaces or newlines |
| `TELEGRAM_BOT_TOKEN` | yes | **Same token as the Trinetra stock bot** |
| `TELEGRAM_CHAT_ID` | yes | Same channel/chat id as Trinetra |
| `SUPABASE_URL` | only for Supabase store | Project URL |
| `SUPABASE_SERVICE_KEY` | only for Supabase store | Service role key |

Repository **variable** (not secret): `PRAVESH_STORE` = `json` (default) or `supabase`.

To add or change readers later, edit `RECIPIENT_EMAIL` alone — nothing in the code needs
touching. Every send logs which addresses it resolved, so the run log always shows where
the report went.
The workflow only commits `data/*.json` back when it is not `supabase`.

### Enabling the workflow

Push to `main`, open the **Actions** tab, enable workflows, then run **Pravesh Daily** via
`workflow_dispatch` once (tick *dry run* for a no-send rehearsal; the preview HTML is
uploaded as an artifact). After that it runs at `30 3 * * 1-5` — 09:00 IST, weekdays.
`permissions: contents: write` is required so the JSON store can commit results back.

---

## The source accuracy ledger

Every call any source makes is written to `data/source_calls.json` keyed on
`(ipo_slug, source_name)`. When the IPO lists, each call is graded against the real
listing gain:

* apply-type call correct if listing gain **≥ +5 %**
* avoid-type call correct if listing gain **≤ +5 %**
* `NEUTRAL` / `NO_VIEW` are **excluded** — a non-call cannot be right or wrong
* a resolved call is frozen: a source cannot quietly rewrite its own history

Accuracy is shown all-time and over the last 15 resolved calls, **always with n**. Below
**n = 5** we print *"insufficient history"*, never a percentage — a 100 % from two calls is
noise dressed as signal. The leaderboard (top 5) sits in the email footer and the web tab.

My own take is held to the same standard: `verdicts.json` records every non-preliminary
call and grades it identically (`RISKY` is excluded — it is an explicit refusal to call).

### Self-calibration

Base weights: **QIB 30 · GMP 25 · total subscription 15 · Singhvi 15 · broker consensus 15**.
Once a source has **n ≥ 10** resolved calls, its weight is multiplied by
`clamp(accuracy / 60 %, 0.5, 1.5)`. Absent sources are dropped and the rest renormalised,
so a dead scraper shifts emphasis instead of silently scoring zero.

Modifiers: OFS > 75 % ⇒ −10 · SME ⇒ −10 and capped at RISKY unless the score ≥ 80 ·
no bidding data yet ⇒ PRELIMINARY. When *nothing* has landed — no bidding, no GMP, nobody
on record — Pravesh prints **"PRELIMINARY — no evidence yet"** and no score at all, because
a 0/100 there would read as AVOID, which would be a lie.

Bands: **≥70 🟢 APPLY · 50–69 🔵 LISTING GAINS ONLY · 35–49 🟡 RISKY · <35 🔴 AVOID**.

### The Singhvi veto rule

If Anil Singhvi says **AVOID**, the score does **not** change. Instead a hard red banner —
`⚠ Anil Singhvi says AVOID` — is attached to the IPO and rendered *above* My Take in every
surface (email card, Telegram line, web card). The reasoning behind the number stays
honest; the warning is impossible to miss; the decision stays yours.

---

## Web contract — `data/latest.json`

Written every run (raw GitHub URL is the default feed for the web tab).

```jsonc
{
  "schema_version": 1,
  "brand": { "name": "Trinetra Pravesh", "tagline": "…" },
  "generated_at": "2026-07-31T03:30:11+00:00",   // UTC ISO
  "generated_at_market": "31 Jul 2026, 09:00 IST",
  "run_date": "2026-07-31",
  "counts": { "open": 3, "closing_tomorrow": 1, "upcoming": 2, "watch": 1 },

  "ipos": [{
    "name": "Acme Solar Industries Limited",
    "slug": "acme-solar-industries-limited",     // stable id across runs
    "segment": "MAINBOARD" | "SME",
    "status": "OPEN" | "UPCOMING" | "CLOSED" | "LISTED" | "UNKNOWN",
    "closing_tomorrow": true,
    "open_date": "2026-07-30", "close_date": "2026-08-01",
    "allotment_date": "2026-08-04", "listing_date": "2026-08-06",
    "price_band_low": 280, "price_band_high": 294, "price_band_label": "₹280–₹294",
    "lot_size": 51, "min_investment": 14994,
    "issue_size_cr": 1200, "fresh_issue_cr": 200, "ofs_cr": 1000, "ofs_pct": 83.3,
    "subscription": { "qib": 24.3, "nii": 11.2, "retail": 4.1, "employee": null,
                      "total": 13.8, "updated_at": "NSE · ACME" },
    "gmp": 52, "gmp_pct": 17.7, "gmp_source": "https://ipowatch.in/…",
    "detail_url": "https://…", "exchange": "BSE, NSE",

    "evidence": [{                                // the product's heart
      "source_name": "Anil Singhvi",
      "stance": "APPLY" | "SUBSCRIBE_LONG_TERM" | "APPLY_LISTING_GAINS"
              | "NEUTRAL" | "AVOID" | "NO_VIEW",
      "stance_label": "Subscribe",
      "rationale": "Zee Business: “…”",
      "accuracy_pct": 71.4,                        // null when n < 5
      "accuracy_n": 14,
      "accuracy_label": "71% (n=14)",              // or "insufficient history (n=2)"
      "url": "https://…",
      "is_synthetic": false                        // true for GMP/QIB signals
    }],

    "take": {
      "score": 61.8, "has_score": true,            // has_score=false ⇒ render "—", not 0
      "verdict_key": "LISTING_GAINS",
      "verdict_label": "LISTING GAINS ONLY", "verdict_emoji": "🔵",
      "verdict_color": "#1F5F9E",
      "paragraph": "The strongest argument … Final call is yours.",
      "one_liner": "LISTING GAINS ONLY (QIB 24.3x · GMP +18%)",
      "preliminary": false,
      "flags": ["⚠ Anil Singhvi says AVOID"],      // render above My Take
      "components": [{ "key": "qib", "label": "QIB subscription", "raw_value": 24.3,
                       "normalised": 1.0, "base_weight": 30, "calibration_multiplier": 1.1,
                       "effective_weight": 33, "contribution": 28.4, "note": "…" }],
      "modifiers": ["-10 — 83% of the issue is offer-for-sale"],
      "strongest_for": "…", "strongest_against": "…",
      "created_at": "2026-07-31T03:30:11+00:00"
    },
    "flags": ["⚠ Anil Singhvi says AVOID"]         // mirror of take.flags
  }],

  "leaderboard": [{ "source_name": "Anil Singhvi", "n_all": 14, "correct_all": 10,
                    "accuracy_all": 71.4, "n_recent": 14, "correct_recent": 10,
                    "accuracy_recent": 71.4, "is_synthetic": false }],
  "own_accuracy": { "n_all": 9, "accuracy_all": 66.7, "n_recent": 9,
                    "accuracy_recent": 66.7, "label": "67% (n=9)" },
  "history": [{ "ipo_slug": "…", "ipo_name": "…", "segment": "MAINBOARD",
                "verdict_key": "APPLY", "score": 74.2, "created_at": "…",
                "listing_date": "2026-07-10", "issue_price": 294,
                "listing_price": 331.5, "listing_gain_pct": 12.76,
                "resolved": true, "correct": true, "flags": [] }],
  "sources_ok": ["ipowatch", "nse_subscription", "gmp"],
  "sources_failed": ["singhvi: … "],               // non-empty ⇒ show a degraded notice
  "disclaimer": "Informational only. Not investment advice. …"
}
```

Consumers must treat every scalar as nullable, and must show `accuracy_label` verbatim
rather than recomputing a percentage.

---

## JSON ↔ Supabase

Default is `JsonStore` (files in `data/`, committed back by the workflow — zero infra,
fully diffable, and the raw GitHub URL is the web feed).

Switch with **one line**: `PRAVESH_STORE=supabase` (env or repo variable), or edit
`STORE_BACKEND` in `config.py`. Set `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`. If the client
or credentials are missing, the engine logs the error and falls back to JSON rather than
losing a run. Table names are configurable in `config.SUPABASE`.

```sql
create table pravesh_source_calls (
  ipo_slug text not null, source_name text not null,
  ipo_name text, stance text, rationale text, url text,
  captured_at timestamptz, segment text, is_synthetic boolean default false,
  resolved boolean default false, correct boolean,
  listing_gain_pct double precision, resolved_at timestamptz,
  primary key (ipo_slug, source_name)
);

create table pravesh_verdicts (
  ipo_slug text primary key, ipo_name text, segment text,
  verdict_key text, score double precision, created_at timestamptz,
  listing_date date, issue_price double precision, listing_price double precision,
  listing_gain_pct double precision, resolved boolean default false,
  correct boolean, flags jsonb default '[]'::jsonb
);

create table pravesh_latest (
  key text primary key, payload jsonb not null, updated_at timestamptz
);
```

---

## When scrapers break

Every scraper isolates its CSS selectors and column-header vocabulary in a `SELECTORS` /
`HEADER_KEYS` / `DETAIL_KEYS` block at the top of the file. Open the page, compare the
column headings, add the new wording. Nothing below those blocks should need editing.

| Symptom in the log | File to patch |
|---|---|
| `ipowatch: calendar table empty` | `sources/ipowatch.py` → `SELECTORS`, `HEADER_KEYS` |
| `ipowatch: detail page unparsed` | `sources/ipowatch.py` → `DETAIL_KEYS` |
| `chittorgarh: no rows for mainboard` | `sources/chittorgarh.py` → `SELECTORS`, `HEADER_KEYS` |
| `nse: current-issue feed empty` | `sources/nse_subscription.py` → `ENDPOINTS`, `CATEGORY_KEYS` |
| `gmp: … returned no rows` | `sources/gmp.py` → `SELECTORS`, `GMP_HEADER_KEYS` |
| `singhvi: every discovery route … returned nothing` | `sources/singhvi.py` → `SELECTORS`, `QUERY_TEMPLATES`; `sources/search.py` |
| `brokers: no roundup article reachable` | `sources/brokers.py` → `SELECTORS`, `ALLOWED_DOMAINS`, `FIRM_PATTERN`; `sources/search.py` |

**Known gaps, stated plainly** (verified 2026-07-31, all degrade to "not published yet"
rather than to a guess):

* **BSE SME bidding data.** NSE's API covers mainboard and NSE Emerge; no server-rendered
  source for BSE SME subscription was found. Those issues stay `PRELIMINARY` until they
  close, scored on GMP and published views alone.
* **Singhvi article bodies.** zeebiz.com returns 403 to this crawler, so his stance is
  classified from the headline plus the news summary. Hindi-language headlines classify as
  `NO_VIEW` rather than being machine-translated into a call.
* **Google News links** are wrappers that cannot be de-referenced server-side; they are
  used for headline evidence, never for full-text extraction.

**A whole site going client-side is not a code change — it is a config change.** As of
2026-07-31, `chittorgarh.com`'s report pages became a client-rendered Next.js app that
returns HTTP 200 with no table in the HTML. `config.DATA_PROVIDERS` was reordered so
`ipowatch` leads the calendar and NSE's own API leads subscription; `chittorgarh.py` stays
in the chain and takes over again the moment it returns rows. Providers are tried in order
until one yields actual rows — a 200 with an empty table counts as a failure, which is what
stops a silent JS rewrite from emptying the report.

**Silence always means breakage, never "nothing happened":** zero relevant IPOs still sends
a one-line *all quiet* email and ping, every degraded source is named in the footer, and
the workflow's `if: failure()` job emails a plain-text failure notice with the run URL.

---

## Honesty rules (do not "improve" these away)

1. The evidence table comes first; My Take is visually separate and always ends with
   **"Final call is yours."**
2. Accuracy is always shown with `n`; under n = 5 it reads *insufficient history*.
3. `NO_VIEW` is first-class and never penalised — Singhvi simply does not cover most SME
   issues, and pretending otherwise would fabricate a signal.
4. GMP is labelled indicative and unofficial everywhere it appears.
5. The Singhvi veto warns without secretly moving the number.
6. No evidence ⇒ no score, not a zero.

*Informational only. Not investment advice.*
