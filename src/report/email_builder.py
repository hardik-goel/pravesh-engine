"""The email: one self-contained HTML file, inline CSS only.

Light body with a near-black header band — email clients mangle full dark themes, so we
do not try. Layout order is fixed:

  ⚡ Closing Tomorrow → Open Mainboard → Open SME → Opening This Week (PRELIMINARY)
  → Allotment & Listing Watch → footer (leaderboard, my own accuracy, failures, disclaimer)

Every card is: name + verdict badge + Singhvi banner (if vetoed) → evidence table →
My Take paragraph → detail strip.
"""

from __future__ import annotations

import html
import logging
import smtplib
import os
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional, Sequence

from ..clock import human_date, stamp, today_market
from ..config import (
    BRAND_NAME,
    BRAND_TAGLINE,
    EMAIL,
    MARKET,
    PALETTE,
    PRODUCT_DISCLAIMER,
    ACCURACY,
)
from ..models import IPO, Evidence, RunResult, Segment, SourceAccuracy, Stance, Take

log = logging.getLogger(__name__)

P = PALETTE
CUR = str(MARKET["currency_symbol"])

# --------------------------------------------------------------------------------------
# Inline style fragments
# --------------------------------------------------------------------------------------

S = {
    "body": (
        f"margin:0;padding:0;background:{P['page_bg']};"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        f"color:{P['text']};-webkit-text-size-adjust:100%;"
    ),
    "wrap": "width:100%;max-width:680px;margin:0 auto;padding:0 12px 32px 12px;",
    "header": (
        f"background:{P['header_bg']};color:{P['header_fg']};padding:26px 22px;"
        "border-radius:4px 4px 0 0;"
    ),
    "header_title": (
        f"margin:0;font-size:21px;letter-spacing:0.02em;font-weight:600;color:{P['header_fg']};"
    ),
    "header_sub": f"margin:6px 0 0 0;font-size:12px;color:{P['accent']};letter-spacing:0.08em;text-transform:uppercase;",
    "section_label": (
        f"margin:26px 0 10px 0;font-size:11px;letter-spacing:0.14em;text-transform:uppercase;"
        f"color:{P['muted']};font-weight:600;"
    ),
    "card": (
        f"background:{P['card_bg']};border:1px solid {P['hairline']};border-radius:4px;"
        "padding:18px 18px 14px 18px;margin:0 0 14px 0;"
    ),
    "card_name": "margin:0;font-size:17px;font-weight:650;line-height:1.3;",
    "badge": (
        "display:inline-block;padding:4px 10px;border-radius:3px;font-size:11px;font-weight:700;"
        "letter-spacing:0.06em;color:#FFFFFF;white-space:nowrap;"
    ),
    "banner": (
        f"background:{P['danger_bg']};color:{P['danger_fg']};border-left:3px solid {P['danger_fg']};"
        "padding:9px 12px;margin:12px 0 0 0;font-size:13px;font-weight:700;border-radius:2px;"
    ),
    "table": "width:100%;border-collapse:collapse;margin:14px 0 0 0;font-size:13px;",
    "th": (
        f"text-align:left;padding:6px 8px;border-bottom:1px solid {P['hairline']};"
        f"color:{P['muted']};font-size:10px;letter-spacing:0.1em;text-transform:uppercase;font-weight:600;"
    ),
    "td": f"padding:8px;border-bottom:1px solid {P['hairline']};vertical-align:top;line-height:1.45;",
    "take_box": (
        f"margin:14px 0 0 0;padding:12px 14px;background:{P['page_bg']};"
        f"border-left:3px solid {P['accent']};border-radius:2px;font-size:13.5px;line-height:1.6;"
    ),
    "take_label": (
        f"font-size:10px;letter-spacing:0.14em;text-transform:uppercase;color:{P['muted']};"
        "font-weight:700;display:block;margin-bottom:5px;"
    ),
    "strip": f"margin:12px 0 0 0;font-size:12px;color:{P['muted']};line-height:1.7;",
    "strip_key": f"color:{P['muted']};",
    "strip_val": f"color:{P['text']};font-weight:600;",
    "sme_tag": (
        f"display:inline-block;background:{P['sme_bg']};color:{P['sme_fg']};font-size:10px;"
        "font-weight:700;letter-spacing:0.08em;padding:2px 6px;border-radius:2px;margin-left:6px;"
    ),
    "footer": (
        f"margin:26px 0 0 0;padding:18px;background:{P['card_bg']};border:1px solid {P['hairline']};"
        f"border-radius:4px;font-size:12px;color:{P['muted']};line-height:1.7;"
    ),
    "muted_small": f"font-size:11px;color:{P['muted']};",
}

STANCE_COLORS: dict[Stance, str] = {
    Stance.APPLY: "#1E7A4B",
    Stance.SUBSCRIBE_LONG_TERM: "#1E7A4B",
    Stance.APPLY_LISTING_GAINS: "#1F5F9E",
    Stance.NEUTRAL: "#6B6B63",
    Stance.AVOID: "#9E2B25",
    Stance.NO_VIEW: "#9A958A",
}


def e(value: object) -> str:
    return html.escape(str(value), quote=True)


def money(value: Optional[float], *, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{CUR}{value:,.0f}{suffix}"


# --------------------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------------------


def _badge(take: Take) -> str:
    return (
        f'<span style="{S["badge"]}background:{e(take.verdict_color)};">'
        f"{e(take.verdict_emoji)} {e(take.verdict_label)}</span>"
    )


def _banners(take: Take) -> str:
    return "".join(f'<div style="{S["banner"]}">{e(flag)}</div>' for flag in take.flags)


def _evidence_table(evidence: Optional[Evidence]) -> str:
    if evidence is None or not evidence.rows:
        return (
            f'<p style="{S["strip"]}">No named source has published a view on this issue yet. '
            "That absence is itself information.</p>"
        )
    rows = []
    for row in evidence.rows:
        colour = STANCE_COLORS.get(row.stance, P["muted"])
        name = e(row.source_name)
        if row.url:
            name = f'<a href="{e(row.url)}" style="color:{P["text"]};text-decoration:underline;">{name}</a>'
        synthetic = (
            f'<span style="{S["muted_small"]}"> · signal</span>' if row.is_synthetic else ""
        )
        rows.append(
            f"<tr>"
            f'<td style="{S["td"]}width:26%;"><strong>{name}</strong>{synthetic}</td>'
            f'<td style="{S["td"]}width:18%;color:{colour};font-weight:700;">{e(row.stance.label)}</td>'
            f'<td style="{S["td"]}">{e(row.rationale)}</td>'
            f'<td style="{S["td"]}width:18%;white-space:nowrap;">{e(row.accuracy_label)}</td>'
            f"</tr>"
        )
    return (
        f'<table style="{S["table"]}" role="presentation">'
        f'<tr><th style="{S["th"]}">Source</th><th style="{S["th"]}">Stance</th>'
        f'<th style="{S["th"]}">Why</th><th style="{S["th"]}">Its accuracy</th></tr>'
        f"{''.join(rows)}</table>"
    )


def _take_box(take: Take) -> str:
    score = f"{take.score:.0f}/100" if take.has_score else "no score yet"
    return (
        f'<div style="{S["take_box"]}">'
        f'<span style="{S["take_label"]}">My take · {e(score)}</span>'
        f"{e(take.paragraph)}</div>"
    )


def _detail_strip(ipo: IPO) -> str:
    book = ipo.subscription
    subs: list[str] = []
    for label, value in (
        ("QIB", book.qib),
        ("NII", book.nii),
        ("Retail", book.retail),
        ("Total", book.total),
    ):
        if value is not None:
            subs.append(f"{label} {value:.2f}x")
    parts: list[tuple[str, str]] = [
        ("Band", ipo.price_band_label),
        ("Lot", f"{ipo.lot_size:,}" if ipo.lot_size else "—"),
        ("Min investment", money(ipo.min_investment)),
        ("Issue size", f"{CUR}{ipo.issue_size_cr:,.0f} {MARKET['size_unit']}" if ipo.issue_size_cr else "—"),
        (
            "Fresh / OFS",
            f"{ipo.fresh_issue_cr:,.0f} / {ipo.ofs_cr:,.0f} {MARKET['size_unit']}"
            + (f" ({ipo.ofs_pct:.0f}% OFS)" if ipo.ofs_pct is not None else "")
            if ipo.fresh_issue_cr is not None and ipo.ofs_cr is not None
            else "—",
        ),
        ("Subscription", " · ".join(subs) if subs else "not published yet"),
        (
            "GMP",
            f"{CUR}{ipo.gmp:,.0f} ({ipo.gmp_pct:+.1f}%) · indicative"
            if ipo.gmp is not None and ipo.gmp_pct is not None
            else "—",
        ),
        (
            "Dates",
            f"opens {human_date(ipo.open_date)} · closes {human_date(ipo.close_date)} · "
            f"allotment {human_date(ipo.allotment_date)} · lists {human_date(ipo.listing_date)}",
        ),
    ]
    cells = " &nbsp;·&nbsp; ".join(
        f'<span style="{S["strip_key"]}">{e(k)}</span> <span style="{S["strip_val"]}">{e(v)}</span>'
        for k, v in parts
    )
    return f'<div style="{S["strip"]}">{cells}</div>'


def _card(ipo: IPO, evidence: Optional[Evidence], take: Optional[Take], *, urgent: bool = False) -> str:
    sme_tag = f'<span style="{S["sme_tag"]}">SME · HIGHER RISK</span>' if ipo.is_sme else ""
    urgency = "⚡ " if urgent else ""
    badge = _badge(take) if take else ""
    banner = _banners(take) if take else ""
    take_box = _take_box(take) if take else ""
    return (
        f'<div style="{S["card"]}">'
        f'<table role="presentation" style="width:100%;border-collapse:collapse;"><tr>'
        f'<td style="vertical-align:top;padding:0;">'
        f'<p style="{S["card_name"]}">{urgency}{e(ipo.name)}{sme_tag}</p></td>'
        f'<td style="vertical-align:top;padding:0;text-align:right;white-space:nowrap;">{badge}</td>'
        f"</tr></table>"
        f"{banner}"
        f"{_evidence_table(evidence)}"
        f"{take_box}"
        f"{_detail_strip(ipo)}"
        f"</div>"
    )


def _section(label: str, sublabel: str, body: str) -> str:
    if not body:
        return ""
    sub = f' <span style="{S["muted_small"]}">— {e(sublabel)}</span>' if sublabel else ""
    return f'<p style="{S["section_label"]}">{e(label)}{sub}</p>{body}'


def _leaderboard(rows: Sequence[SourceAccuracy]) -> str:
    if not rows:
        return (
            f"<div>Source leaderboard: not enough resolved calls yet "
            f"(need n≥{ACCURACY['leaderboard_min_n']} per source).</div>"
        )
    items = " &nbsp;·&nbsp; ".join(
        f'<span style="{S["strip_val"]}">{e(r.source_name)}</span> {r.accuracy_all:.0f}% (n={r.n_all})'
        for r in rows
    )
    return f"<div><strong>Source leaderboard</strong> &nbsp;{items}</div>"


def _footer(result: RunResult) -> str:
    failures = ""
    if result.sources_failed:
        failures = (
            f'<div style="color:{P["danger_fg"]};margin-top:6px;">⚠ Degraded this run: '
            f"{e('; '.join(result.sources_failed))}</div>"
        )
    own = result.own_accuracy.get("label", "insufficient history (n=0)")
    return (
        f'<div style="{S["footer"]}">'
        f"{_leaderboard(result.leaderboard)}"
        f'<div style="margin-top:6px;"><strong>My own rolling accuracy</strong> &nbsp;{e(own)}</div>'
        f"{failures}"
        f'<div style="margin-top:10px;">{e(PRODUCT_DISCLAIMER)}</div>'
        f'<div style="margin-top:6px;">Generated {e(stamp())} · {e(BRAND_NAME)}</div>'
        f"</div>"
    )


# --------------------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------------------


def _document(inner: str, preheader: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{e(BRAND_NAME)}</title></head>"
        f'<body style="{S["body"]}">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{e(preheader)}</div>'
        f'<div style="{S["wrap"]}">{inner}</div></body></html>'
    )


def _header(subtitle: str) -> str:
    return (
        f'<div style="{S["header"]}">'
        f'<p style="{S["header_title"]}">{e(BRAND_NAME)}</p>'
        f'<p style="{S["header_sub"]}">{e(subtitle)}</p></div>'
    )


def build_html(result: RunResult, today: Optional[date] = None) -> str:
    """Full report. Assumes result.ipos already carries evidence + takes."""
    today = today or today_market()
    if result.is_quiet:
        return build_quiet_html(result, today)

    def cards(ipos: Sequence[IPO], *, urgent: bool = False) -> str:
        return "".join(
            _card(ipo, result.evidence.get(ipo.slug), result.takes.get(ipo.slug), urgent=urgent)
            for ipo in ipos
        )

    closing = result.closing_tomorrow(today)
    closing_slugs = {i.slug for i in closing}
    open_main = [i for i in result.open_ipos if not i.is_sme and i.slug not in closing_slugs]
    open_sme = [i for i in result.open_ipos if i.is_sme and i.slug not in closing_slugs]
    upcoming = [
        i for i in result.upcoming_ipos if i.open_date and (i.open_date - today).days <= 7
    ]

    body = [
        _header(f"{human_date(today, '%d %b %Y')} · {len(result.open_ipos)} open · {len(closing)} closing soon"),
        _section("⚡ Closing tomorrow", "last window to apply", cards(closing, urgent=True)),
        _section("Open · Mainboard", "", cards(open_main)),
        _section("Open · SME", "higher risk, thinner liquidity", cards(open_sme)),
        _section("Opening this week", "preliminary — no bidding data yet", cards(upcoming)),
        _section("Allotment & listing watch", "closed, awaiting listing", _watch_table(result)),
        _footer(result),
    ]
    preheader = (
        f"{len(result.open_ipos)} open, {len(closing)} closing soon. Evidence table, my take, your call."
    )
    return _document("".join(body), preheader)


def _watch_table(result: RunResult) -> str:
    rows = []
    for ipo in result.watch_ipos:
        take = result.takes.get(ipo.slug)
        verdict = f"{take.verdict_emoji} {take.verdict_label}" if take else "—"
        sub = ipo.subscription.total
        sub_label = f"{sub:.2f}x" if sub is not None else "—"
        sme_tag = f'<span style="{S["sme_tag"]}">SME</span>' if ipo.is_sme else ""
        rows.append(
            "<tr>"
            f'<td style="{S["td"]}"><strong>{e(ipo.name)}</strong>{sme_tag}</td>'
            f'<td style="{S["td"]}">{e(sub_label)}</td>'
            f'<td style="{S["td"]}">{e(human_date(ipo.allotment_date))}</td>'
            f'<td style="{S["td"]}">{e(human_date(ipo.listing_date))}</td>'
            f'<td style="{S["td"]}">{e(verdict)}</td>'
            "</tr>"
        )
    if not rows:
        return ""
    return (
        f'<div style="{S["card"]}"><table style="{S["table"]}" role="presentation">'
        f'<tr><th style="{S["th"]}">IPO</th><th style="{S["th"]}">Final subs</th>'
        f'<th style="{S["th"]}">Allotment</th><th style="{S["th"]}">Listing</th>'
        f'<th style="{S["th"]}">My call was</th></tr>'
        f"{''.join(rows)}</table></div>"
    )


def build_quiet_html(result: RunResult, today: Optional[date] = None) -> str:
    """Silence must always mean 'nothing to do', never 'the scraper died'."""
    today = today or today_market()
    failures = ""
    if result.sources_failed:
        failures = (
            f'<p style="{S["strip"]}color:{P["danger_fg"]};">⚠ Some sources were unreachable this run: '
            f"{e('; '.join(result.sources_failed))}</p>"
        )
    inner = (
        _header(f"{human_date(today, '%d %b %Y')} · all quiet")
        + f'<div style="{S["card"]}"><p style="margin:0;font-size:14px;">'
        f"{e(str(EMAIL['quiet_body']))}</p>{failures}</div>"
        + _footer(result)
    )
    return _document(inner, str(EMAIL["quiet_body"]))


# --------------------------------------------------------------------------------------
# Subject + delivery
# --------------------------------------------------------------------------------------


def build_subject(result: RunResult, today: Optional[date] = None) -> str:
    today = today or today_market()
    date_label = human_date(today, "%d %b")
    if result.is_quiet:
        return str(EMAIL["quiet_subject_template"]).format(brand=BRAND_NAME, date=date_label)
    closing = result.closing_tomorrow(today)
    subject = str(EMAIL["subject_template"]).format(
        brand=BRAND_NAME,
        date=date_label,
        open_count=len(result.open_ipos),
        closing_count=len(closing),
    )
    if closing:
        subject = f"{EMAIL['subject_urgent_prefix']}{subject}"
    return subject


def write_preview(html_body: str, path: Optional[str] = None) -> Path:
    target = Path(path or str(EMAIL["dry_run_path"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html_body, encoding="utf-8")
    log.info("dry run — email written to %s", target)
    return target


def send_email(subject: str, html_body: str) -> bool:
    """Gmail SMTP over STARTTLS. Returns False instead of raising."""
    address = os.getenv(str(EMAIL["address_env"]), "").strip()
    password = os.getenv(str(EMAIL["password_env"]), "").strip()
    recipients_raw = os.getenv(str(EMAIL["recipient_env"]), "").strip()
    if not (address and password and recipients_raw):
        log.error(
            "email not sent: %s / %s / %s must all be set",
            EMAIL["address_env"],
            EMAIL["password_env"],
            EMAIL["recipient_env"],
        )
        return False

    recipients = [r.strip() for r in recipients_raw.replace(";", ",").split(",") if r.strip()]
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"{EMAIL['sender_name']} <{address}>"
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(_plain_text_fallback(subject), "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(str(EMAIL["smtp_host"]), int(EMAIL["smtp_port"]), timeout=30) as server:
            server.ehlo()
            if EMAIL["use_starttls"]:
                server.starttls()
                server.ehlo()
            server.login(address, password)
            server.sendmail(address, recipients, message.as_string())
        log.info("email sent to %s", ", ".join(recipients))
        return True
    except Exception as exc:  # noqa: BLE001 — delivery failure must not kill the run
        log.error("email send failed: %s", exc)
        return False


def _plain_text_fallback(subject: str) -> str:
    return (
        f"{subject}\n\n{BRAND_TAGLINE}\n\n"
        "This report is HTML — open it in a client that renders HTML to see the evidence tables.\n\n"
        f"{PRODUCT_DISCLAIMER}\n"
    )


def send_failure_notice(reason: str) -> bool:
    """Plain-text alert used by the workflow's failure step."""
    subject = f"⚠ {BRAND_NAME} · run failed"
    body = (
        f"{BRAND_NAME} failed to complete its run.\n\n{reason}\n\n"
        "Silence means breakage — check the GitHub Actions log."
    )
    address = os.getenv(str(EMAIL["address_env"]), "").strip()
    password = os.getenv(str(EMAIL["password_env"]), "").strip()
    recipients_raw = os.getenv(str(EMAIL["recipient_env"]), "").strip()
    if not (address and password and recipients_raw):
        log.error("failure notice not sent: mail secrets missing")
        return False
    recipients = [r.strip() for r in recipients_raw.replace(";", ",").split(",") if r.strip()]
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = f"{EMAIL['sender_name']} <{address}>"
    message["To"] = ", ".join(recipients)
    try:
        with smtplib.SMTP(str(EMAIL["smtp_host"]), int(EMAIL["smtp_port"]), timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(address, password)
            server.sendmail(address, recipients, message.as_string())
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("failure notice send failed: %s", exc)
        return False


__all__ = [
    "build_html",
    "build_quiet_html",
    "build_subject",
    "send_email",
    "send_failure_notice",
    "write_preview",
]
