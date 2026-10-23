"""Digest rendering: one structure, plain-text and HTML views. All source text is escaped.

Court-issued items (importance high) come first, other activity follows on one line each.
Links point to human-readable CourtListener pages and, when available, the RECAP PDF.
"""
import html
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

SITE = "https://www.courtlistener.com"
STORAGE = "https://storage.courtlistener.com/"
TIMEZONE = ZoneInfo("America/Los_Angeles")
PDF_PATH = re.compile(r"recap/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.pdf")
FOOTER = ('Why you got this: you follow these cases. "Court order" means the docket description starts with '
          "court-issued wording; it is a rule-based screen, not legal analysis.",
          "Automated alert. Not legal advice. CourtListener data can be delayed or incomplete; "
          "verify against the source before acting.")


def one_line(text, limit=None):
    text = " ".join(str(text or "").split())
    return text if limit is None or len(text) <= limit else text[:limit - 1].rstrip() + "…"


def plural(n, word):
    return f"{n} {word}{'s' * (n != 1)}"


def filed(value):
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return "date unknown"
    return f"{day:%b} {day.day}, {day.year}"


def docket_url(docket_id, info):
    return SITE + (info.get("absolute_url") or f"/docket/{docket_id}/")


def entry_url(docket_id, info, number):
    base = docket_url(docket_id, info)
    return f"{base}?entry_gte={number}&entry_lte={number}" if type(number) is int else base


def pdf_url(payload):
    """Main document first; only available files with a recognised RECAP storage path."""
    docs = [d for d in payload.get("recap_documents") or [] if isinstance(d, dict)]
    for doc in sorted(docs, key=lambda d: d.get("attachment_number") or 0):
        path = doc.get("filepath_local")
        if doc.get("is_available") and isinstance(path, str) and PDF_PATH.fullmatch(path):
            return STORAGE + path
    return None


def change_label(event):
    if event.kind == "added":
        return "NEW"
    before = event.before or {}
    if before.get("description") != event.payload.get("description"):
        return "UPDATED · description changed"
    return "UPDATED · documents or details changed"


def build(items, infos):
    """Group items into sections -> cases -> entries, in reading order."""
    cases = {}
    for event, analysis in items:
        info = infos.get(event.docket_id) or {}
        row = event.payload
        cases.setdefault(event.docket_id, {
            "title": one_line(info.get("case_name")) or f"Docket {event.docket_id}",
            "meta": " · ".join(one_line(x) for x in (info.get("docket_number"), info.get("court")) if x),
            "url": docket_url(event.docket_id, info), "orders": [], "other": []})
        number = row.get("entry_number")
        cases[event.docket_id]["orders" if analysis.importance == "high" else "other"].append({
            "number": number if type(number) is int else None, "filed": filed(row.get("date_filed")),
            "label": change_label(event), "description": one_line(row.get("description")),
            "summary": one_line(analysis.summary) or None,
            "url": entry_url(event.docket_id, info, number), "pdf": pdf_url(row)})
    ordered = sorted(cases.values(), key=lambda c: c["title"].lower())
    for case in ordered:
        for key in ("orders", "other"):
            case[key].sort(key=lambda e: (e["number"] is None, e["number"] or 0))
    return ordered


def render_digest(items, infos=None, now=None):
    """items: [(Event, Analysis)] for one recipient. Returns (subject, text, html)."""
    cases = build(items, infos or {})
    now = (now or datetime.now(timezone.utc)).astimezone(TIMEZONE)
    orders = sum(len(c["orders"]) for c in cases)
    other = sum(len(c["other"]) for c in cases)
    lead = next((c for c in cases if c["orders"]), cases[0])
    label = plural(orders, "court order") if orders else plural(orders + other, "update")
    more = f" +{plural(len(cases) - 1, 'case')}" if len(cases) > 1 else ""
    subject = one_line(f"[{label}] {one_line(lead['title'], 70)}{more} — {now:%b} {now.day}")
    stamp = f"{now:%b} {now.day}, {now.year}, {now:%I}".replace(", 0", ", ") + f":{now:%M %p} PT"
    counts = [plural(orders, "court order") if orders else "", plural(other, "other filing") if other else ""]
    headline = " · ".join([c for c in counts if c] + [plural(len(cases), "case")])
    return subject, text_view(cases, headline, stamp), html_view(cases, headline, stamp)


def text_view(cases, headline, stamp):
    rule = "─" * 72
    lines = [f"{headline}    {stamp}", rule]
    if any(c["orders"] for c in cases):
        lines += ["", "COURT ORDERS — review first", ""]
        for case in (c for c in cases if c["orders"]):
            lines += [case["title"]] + ([case["meta"]] if case["meta"] else []) + [""]
            for e in case["orders"]:
                lines.append(f"  #{e['number'] or '?'} · Filed {e['filed']} · {e['label']}")
                lines.append(f"  {e['description']}")
                if e["summary"]:
                    lines.append(f"  Summary: {e['summary']}")
                lines.append(f"  View on CourtListener: {e['url']}")
                if e["pdf"]:
                    lines.append(f"  PDF: {e['pdf']}")
                lines.append("")
        lines.append(rule)
    if any(c["other"] for c in cases):
        lines += ["", "OTHER ACTIVITY", ""]
        for case in (c for c in cases if c["other"]):
            lines.append(case["title"] + (f" ({case['meta']})" if case["meta"] else ""))
            for e in case["other"]:
                lines.append(f"  #{e['number'] or '?'} · {e['filed']} · {e['label']} · {one_line(e['description'], 140)}")
                lines.append(f"    {e['url']}")
            lines.append("")
        lines.append(rule)
    lines += [""] + list(FOOTER)
    return "\n".join(lines)


def html_view(cases, headline, stamp):
    esc = lambda value: html.escape(str(value), quote=True)
    link = lambda url, text: f'<a href="{esc(url)}" style="color:#1a56db">{esc(text)}</a>'
    muted = "color:#6b7280;font-size:13px"
    parts = [f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;color:#111827;line-height:1.45">',
             f'<p style="margin:0 0 4px;font-weight:bold">{esc(headline)}</p><p style="margin:0 0 16px;{muted}">{esc(stamp)}</p>']
    if any(c["orders"] for c in cases):
        parts.append('<h2 style="font-size:16px;margin:16px 0 8px;color:#b91c1c">Court orders — review first</h2>')
        for case in (c for c in cases if c["orders"]):
            parts.append(f'<h3 style="font-size:15px;margin:12px 0 2px">{link(case["url"], case["title"])}</h3>')
            if case["meta"]:
                parts.append(f'<p style="margin:0 0 8px;{muted}">{esc(case["meta"])}</p>')
            for e in case["orders"]:
                links = link(e["url"], "View on CourtListener") + (" · " + link(e["pdf"], "PDF") if e["pdf"] else "")
                summary = f'<p style="margin:4px 0"><b>Summary:</b> {esc(e["summary"])}</p>' if e["summary"] else ""
                parts.append(f'<div style="border-left:3px solid #b91c1c;padding:4px 0 4px 10px;margin:0 0 12px">'
                             f'<p style="margin:0;{muted}">#{esc(e["number"] or "?")} · Filed {esc(e["filed"])} · {esc(e["label"])}</p>'
                             f'<p style="margin:4px 0">{esc(e["description"])}</p>{summary}<p style="margin:4px 0">{links}</p></div>')
    if any(c["other"] for c in cases):
        parts.append('<h2 style="font-size:16px;margin:20px 0 8px">Other activity</h2>')
        for case in (c for c in cases if c["other"]):
            meta = f' <span style="{muted}">({esc(case["meta"])})</span>' if case["meta"] else ""
            parts.append(f'<p style="margin:8px 0 2px"><b>{esc(case["title"])}</b>{meta}</p><ul style="margin:0 0 8px;padding-left:20px">')
            for e in case["other"]:
                parts.append(f'<li style="margin:2px 0">#{esc(e["number"] or "?")} · {esc(e["filed"])} · {esc(e["label"])} · '
                             f'{esc(one_line(e["description"], 140))} {link(e["url"], "View")}</li>')
            parts.append("</ul>")
    parts.append(f'<hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0">'
                 + "".join(f'<p style="margin:0 0 6px;{muted}">{esc(line)}</p>' for line in FOOTER) + "</div>")
    return "".join(parts)
