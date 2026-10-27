"""Digest rendering: one structure, plain-text and HTML views. All source text is escaped.

Layout, shortest path to "what happened and what do I do by when":
- Upcoming deadlines: every verified future deadline in the digest, once per date and wording,
  with the entries it comes from.
- Priority (high): full entries with AI summary, action, deadlines and any document briefing.
- Worth a look (medium): one line per entry (AI summary, else the description), headed by
  filing type when three or more share one.
- Other activity: grouped counts plus single lines, with one link to the whole range.
- Older filings newly on CourtListener: entries filed more than BACKFILL_DAYS before CourtListener
  changed them (bulk RECAP uploads of a case's history). One count line per case, never counted
  as priority or put on the deadline board.
Links point to human-readable CourtListener pages and, when available, the RECAP PDF.
"""
import html
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src.notify.grouping import classify, group, summary_line

SITE = "https://www.courtlistener.com"
STORAGE = "https://storage.courtlistener.com/"
TIMEZONE = ZoneInfo("America/Los_Angeles")
PDF_PATH = re.compile(r"recap/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.pdf")
SECTIONS = (("priority", "Priority — review first"), ("review", "Worth a look"), ("other", "Other activity"))
FOOTER = ('Why you got this: you follow these cases. "Priority" means court-issued wording at the start of the '
          "description, or an AI reading backed by a quote verified against the entry.",
          "Automated alert. Not legal advice. CourtListener data can be delayed or incomplete; "
          "verify against the source before acting.")
AI_NOTE = ("AI summaries and actions are machine-generated and may be wrong. Deadlines are shown only when "
           "the quoted text, including the date, appears in the entry.")
LATE_NOTE = ("NEW ON COURTLISTENER: filed earlier (date shown) but only now available on CourtListener, "
             "for example when an older entry is added to RECAP.")
LATE_DAYS = 7
BACKFILL_DAYS = 30
BACKFILL_NOTE = ("Older filings: entries filed more than a month before they appeared on CourtListener, usually "
                 "a bulk upload of the case's history. They are counted, not ranked.")
MAX_REFS = 6
SENTENCE_END = re.compile(r"\.\s+(?=[A-Z])")
# Periods that do not end a sentence in court text ("Dr. Ward", "Charleston v. Meta", "No. 5").
ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "v", "vs", "no", "nos", "inc", "co", "corp", "ltd", "llc", "jr", "sr",
                 "st", "u.s", "e.g", "i.e", "al", "ret", "hon", "dkt", "ecf", "ex", "fed", "r", "civ", "p", "cir"}
MIN_HEADING = 3
DOCUMENT_NOTE = ("Points read from the document are shown only when their quote, and any date, appears on "
                 "the page shown (p.); which event or party a date belongs to is the AI's reading.")


def one_line(text, limit=None):
    """Collapse whitespace; when longer than limit, cut at a word boundary and add an ellipsis."""
    text = " ".join(str(text or "").split())
    if limit is None or len(text) <= limit:
        return text
    cut = text[:limit - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut).rstrip(" ,.;:") + "…"


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


def days_late(event):
    """Days between filing and CourtListener's change; large values mean a RECAP backfill."""
    try:
        return (datetime.fromisoformat(event.source_modified_at).date()
                - date.fromisoformat(event.payload.get("date_filed"))).days
    except (TypeError, ValueError):
        return None


def change_label(event):
    if event.kind == "added":
        # An entry filed long before CourtListener changed it was added late (RECAP backfill), not filed today.
        return "NEW ON COURTLISTENER" if (days_late(event) or 0) > LATE_DAYS else "NEW"
    before = event.before or {}
    if before.get("description") != event.payload.get("description"):
        return "UPDATED · description changed"
    return "UPDATED · documents or details changed"


def section(importance):
    return "priority" if importance == "high" else "review" if importance == "medium" else "other"


def ai_fields(analysis):
    llm = (analysis.details or {}).get("llm") or {}
    if llm.get("status") != "ok":
        return None, []
    deadlines = [{"date": filed(d.get("date")), "what": one_line(d.get("what")), "quote": one_line(d.get("quote")),
                  "iso": d.get("date")} for d in llm.get("deadlines") or [] if isinstance(d, dict)]
    return one_line(llm.get("action")) or None, deadlines


def document_fields(analysis):
    """Verified full-document briefing, if any: points, disputes, proposed dates grouped by event."""
    doc = (analysis.details or {}).get("document") or {}
    if doc.get("status") != "ok":
        return None
    proposed = {}
    for item in doc.get("proposed_dates") or []:
        event = one_line(item.get("event"))
        proposed.setdefault(event.lower(), {"event": event, "dates": []})["dates"].append(
            {"party": one_line(item.get("party")), "date": filed(item.get("date")), "page": item.get("page")})
    return {"type": one_line(doc.get("document_type")), "overview": one_line(doc.get("overview")),
            "action": one_line(doc.get("action")) or None,
            "points": [{"text": one_line(i.get("point")), "page": i.get("page")} for i in doc.get("key_points") or []],
            "disputes": [{"issue": one_line(d.get("issue")),
                          "positions": [{"party": one_line(p.get("party")), "text": one_line(p.get("position")),
                                         "page": p.get("page")} for p in d.get("positions") or []]}
                         for d in doc.get("disputes") or []],
            "deadlines": [{"date": filed(d.get("date")), "what": one_line(d.get("what")), "quote": one_line(d.get("quote")),
                           "page": d.get("page"), "iso": d.get("date")} for d in doc.get("deadlines") or []],
            "proposed": list(proposed.values()),
            "truncated": (doc.get("pages_read"), doc.get("pages")) if doc.get("truncated") else None}


def page(value):
    return f" (p. {value})" if type(value) is int else ""


def build(items, infos, today=None):
    """Group items into cases -> sections -> entries, in reading order. today marks past deadlines."""
    cases = {}
    for event, analysis in items:
        info = infos.get(event.docket_id) or {}
        row = event.payload
        case = cases.setdefault(event.docket_id, {
            "title": one_line(info.get("case_name")) or f"Docket {event.docket_id}",
            "meta": " · ".join(one_line(x) for x in (info.get("docket_number"), info.get("court")) if x),
            "url": docket_url(event.docket_id, info), "backfill": [], **{key: [] for key, _ in SECTIONS}})
        number = row.get("entry_number")
        action, deadlines = ai_fields(analysis)
        summary, document = one_line(analysis.summary) or None, document_fields(analysis)
        if document:
            # The full document supersedes the entry reading; keep entry deadlines it does not cover.
            known = {d["iso"] for d in document["deadlines"]}
            summary, action = document["overview"] or summary, document["action"] or action
            deadlines = document["deadlines"] + [d for d in deadlines if d["iso"] not in known]
        for d in deadlines:
            d["passed"] = bool(today and (d.get("iso") or "9999") < today.isoformat())
        backfill = (days_late(event) or 0) > BACKFILL_DAYS
        case["backfill" if backfill else section(analysis.importance)].append({
            "importance": analysis.importance,
            "case": case["title"],
            "number": number if type(number) is int else None, "filed": filed(row.get("date_filed")),
            "iso_filed": row.get("date_filed") if isinstance(row.get("date_filed"), str) else None,
            "label": change_label(event), "description": one_line(row.get("description")),
            "summary": summary, "action": action, "deadlines": deadlines, "document": document,
            "url": entry_url(event.docket_id, info, number), "pdf": pdf_url(row)})
    ordered = sorted(cases.values(), key=lambda c: c["title"].lower())
    for case in ordered:
        for key in [key for key, _ in SECTIONS] + ["backfill"]:
            case[key].sort(key=lambda e: (e["number"] is None, e["number"] or 0))
    return ordered


def deadline_board(cases, today):
    """Future deadlines from Priority and Worth a look, merged when the same wording repeats on
    a date (e.g. one briefing schedule on 13 motions). Returns [{"date", "label", "refs"}]."""
    board = {}
    for case in cases:
        for e in case["priority"] + case["review"]:
            for d in e["deadlines"]:
                try:
                    day = date.fromisoformat(d.get("iso") or "")
                except ValueError:
                    continue
                if day < today:
                    continue
                key = (day, " ".join(d["quote"].lower().split()).rstrip(". "))
                item = board.setdefault(key, {"date": filed(d["iso"]), "what": d["what"], "quote": d["quote"], "refs": {}})
                refs = item["refs"].setdefault(case["title"], [])
                if e["number"] not in refs:
                    refs.append(e["number"])
    rows = []
    for (day, _), item in sorted(board.items(), key=lambda kv: kv[0]):
        parts = []
        for title, numbers in item["refs"].items():
            shown = ", ".join(f"#{n or '?'}" for n in numbers[:MAX_REFS])
            shown += f" and {len(numbers) - MAX_REFS} more" if len(numbers) > MAX_REFS else ""
            parts.append(f"{one_line(title, 32)} {shown}" if len(cases) > 1 else shown)
        # A shared clerk line ("Responses due by 10/29/2026.") reads better than one entry's paraphrase.
        many = sum(len(numbers) for numbers in item["refs"].values()) > 1
        label = one_line(item["quote"], 90) if many else item["what"]
        rows.append({"date": item["date"], "label": label, "refs": "; ".join(parts)})
    return rows


def review_blocks(entries):
    """Worth-a-look entries as [(heading or None, entries)]: a heading for a filing type shared by
    MIN_HEADING or more entries, then the rest without one."""
    kinds = {}
    for e in entries:
        kinds.setdefault(classify(e["description"])[0] if e["description"] else None, []).append(e)
    blocks = [(f"{kind[0].upper()}{kind[1:]} ({len(members)})", members) for kind, members in kinds.items()
              if kind and len(members) >= MIN_HEADING]
    grouped = {id(e) for _, members in blocks for e in members}
    rest = [e for e in entries if id(e) not in grouped]
    # Under headed groups, an unheaded tail would read as part of the last group.
    return blocks + ([(f"Other ({len(rest)})" if blocks else None, rest)] if rest else [])


def short_line(e):
    """Worth-a-look line: the first sentence of the AI summary (later sentences mostly repeat the
    deadlines already on the board), else the description."""
    return one_line(first_sentence(e["summary"]) if e["summary"] else e["description"], 160)


def first_sentence(text):
    for match in SENTENCE_END.finditer(text):
        word = text[:match.start()].rsplit(" ", 1)[-1].lower().strip("(")
        if word not in ABBREVIATIONS and len(word) > 1:
            return text[:match.start() + 1]
    return text


def range_url(case, key="other"):
    numbers = [e["number"] for e in case[key] if e["number"] is not None]
    if len(numbers) < 2:
        return None, 0
    return f"{case['url']}?entry_gte={min(numbers)}&entry_lte={max(numbers)}", len(case[key])


def backfill_line(case):
    """One line per case for older filings: how many, which dates, and what kind."""
    entries = case["backfill"]
    days = sorted(e["iso_filed"] for e in entries if e["iso_filed"])
    span = f", filed {filed(days[0])} – {filed(days[-1])}" if days else ""
    kinds = (("high", "court order or notice", "court orders or notices"),
             ("medium", "party motion or brief", "party motions and briefs"), ("low", "other filing", "other filings"))
    counts = [f"{n} {one if n == 1 else many}" for level, one, many in kinds
              if (n := sum(e["importance"] == level for e in entries))]
    noun = "older entry" if len(entries) == 1 else "older entries"
    return f"{len(entries)} {noun}{span}: " + ", ".join(counts)


def render_digest(items, infos=None, now=None, remaining=0):
    """items: [(Event, Analysis)] for one recipient; remaining: pending items left for the next
    digest. Returns (subject, text, html)."""
    now = (now or datetime.now(timezone.utc)).astimezone(TIMEZONE)
    cases = build(items, infos or {}, now.date())
    counts = {key: sum(len(c[key]) for c in cases) for key, _ in SECTIONS}
    older = sum(len(c["backfill"]) for c in cases)
    lead = next((c for key in ("priority", "review", "other") for c in cases if c[key]), cases[0])
    if counts["priority"]:
        label = plural(counts["priority"], "priority item")
    elif sum(counts.values()):
        label = plural(sum(counts.values()), "update")
    else:
        label = f"{plural(older, 'older filing')} added"
    more = f" +{plural(len(cases) - 1, 'case')}" if len(cases) > 1 else ""
    subject = one_line(f"[{label}] {one_line(lead['title'], 70)}{more} — {now:%b} {now.day}")
    stamp = f"{now:%b} {now.day}, {now.year}, {now:%I}".replace(", 0", ", ") + f":{now:%M %p} PT"
    words = {"priority": "priority item", "review": "worth a look", "other": "other filing"}
    parts = [plural(counts[k], words[k]) if k != "review" else f"{counts[k]} worth a look" for k, _ in SECTIONS if counts[k]]
    parts += [f"{plural(older, 'older filing')} newly on CourtListener"] if older else []
    headline = " · ".join(parts + [plural(len(cases), "case")])
    if remaining:
        headline += f" · {remaining} more in the next digest"
    entries = [e for c in cases for key, _ in SECTIONS for e in c[key]]
    footer = FOOTER + ((AI_NOTE,) if any(e["summary"] for e in entries) else ()) + \
        ((DOCUMENT_NOTE,) if any(e["document"] for e in entries) else ()) + \
        ((LATE_NOTE,) if any(e["label"] == "NEW ON COURTLISTENER" for e in entries) else ()) + \
        ((BACKFILL_NOTE,) if older else ())
    board = deadline_board(cases, now.date())
    return subject, text_view(cases, headline, stamp, footer, board), html_view(cases, headline, stamp, footer, board)


def full_text(e):
    """Every detail of one entry (Priority, or a Worth-a-look entry with a document briefing)."""
    lines = []
    lines.append(f"  #{e['number'] or '?'} · Filed {e['filed']} · {e['label']}")
    lines.append(f"  {one_line(e['description'], 240) if e['summary'] else e['description']}")
    doc = e["document"]
    if e["summary"]:
        kind = f" ({doc['type']})" if doc and doc["type"] else ""
        lines.append(f"  AI summary{kind}: {e['summary']}")
    if doc:
        lines += [f"    • {p['text']}{page(p['page'])}" for p in doc["points"]]
        if doc["disputes"]:
            lines.append("  Disputed:")
        for d in doc["disputes"]:
            lines.append(f"    • {d['issue']}")
            lines += [f"        {p['party']}: {p['text']}{page(p['page'])}" for p in d["positions"]]
        if doc["proposed"]:
            lines.append("  Proposed dates (not yet set by the court):")
        for g in doc["proposed"]:
            pages = sorted({d["page"] for d in g["dates"] if type(d["page"]) is int})
            lines.append(f"    • {g['event']} — " + " · ".join(f"{d['party']}: {d['date']}" for d in g["dates"])
                         + (f" (p. {', '.join(map(str, pages))})" if pages else ""))
    if e["action"]:
        lines.append(f"  Action: {e['action']}")
    for d in e["deadlines"]:
        lines.append(f"  Deadline{' (passed)' if d.get('passed') else ''}: {d['date']} — {d['what']} (\"{d['quote']}\"){page(d.get('page'))}")
    if doc and doc["truncated"]:
        lines.append(f"  Read pages 1–{doc['truncated'][0]} of {doc['truncated'][1]}; the rest was not analyzed.")
    lines.append(f"  View on CourtListener: {e['url']}")
    if e["pdf"]:
        lines.append(f"  PDF: {e['pdf']}")
    lines.append("")
    return lines


def text_view(cases, headline, stamp, footer, board=()):
    rule = "─" * 72
    lines = [f"{headline}    {stamp}", rule]
    if board:
        lines += ["", "UPCOMING DEADLINES", ""]
        lines += [f"  {d['date']:<13} {d['label']} — {d['refs']}" for d in board]
        lines += ["", rule]
    for key, title in SECTIONS:
        if not any(c[key] for c in cases):
            continue
        lines += ["", title.upper(), ""]
        for case in (c for c in cases if c[key]):
            if key == "other":
                groups, singles, undescribed = group(case[key])
                url, count = range_url(case)
                lines.append(case["title"] + (f" ({case['meta']})" if case["meta"] else ""))
                lines += [f"  • {summary_line(g)}" for g in groups]
                for e in singles:
                    lines.append(f"  • #{e['number'] or '?'} · {e['filed']} · {e['label']} · {one_line(e['description'], 160)}")
                    if not url:
                        lines.append(f"    {e['url']}")
                if undescribed:
                    lines.append(f"  • {(f'{undescribed} entry' if undescribed == 1 else f'{undescribed} entries')} without a description yet")
                if url:
                    lines.append(f"  View these {count} entries on CourtListener: {url}")
                lines.append("")
                continue
            lines += [case["title"]] + ([case["meta"]] if case["meta"] else []) + [""]
            if key == "review":
                for heading, members in review_blocks(case[key]):
                    if heading:
                        lines.append(f"  {heading}")
                    for e in members:
                        if e["document"]:
                            lines += full_text(e)
                            continue
                        lines.append(f"  • #{e['number'] or '?'} · {e['filed']} · {short_line(e)}")
                        lines.append(f"    {e['url']}")
                lines.append("")
                continue
            for e in case[key]:
                lines += full_text(e)
        lines.append(rule)
    if any(c["backfill"] for c in cases):
        lines += ["", "OLDER FILINGS NEWLY ON COURTLISTENER", ""]
        for case in (c for c in cases if c["backfill"]):
            url, _ = range_url(case, "backfill")
            lines.append(case["title"] + (f" ({case['meta']})" if case["meta"] else ""))
            lines.append(f"  {backfill_line(case)}")
            lines += [f"  View them on CourtListener: {url}"] if url else []
            lines.append("")
        lines.append(rule)
    lines += [""] + list(footer)
    return "\n".join(lines)


def full_html(e, color):
    """HTML counterpart of full_text()."""
    esc = lambda value: html.escape(str(value), quote=True)
    link = lambda url, text: f'<a href="{esc(url)}" style="color:#1a56db">{esc(text)}</a>'
    muted = "color:#6b7280;font-size:13px"
    links = link(e["url"], "View on CourtListener") + (" · " + link(e["pdf"], "PDF") if e["pdf"] else "")
    ai = ""
    doc = e["document"]
    pg = lambda value: f' <span style="{muted}">p. {esc(value)}</span>' if type(value) is int else ""
    if e["summary"]:
        kind = f' <span style="{muted}">({esc(doc["type"])})</span>' if doc and doc["type"] else ""
        ai += f'<p style="margin:4px 0"><b>AI summary:</b> {esc(e["summary"])}{kind}</p>'
    if doc and doc["points"]:
        ai += '<ul style="margin:4px 0;padding-left:20px">' + "".join(
            f'<li style="margin:2px 0">{esc(p["text"])}{pg(p["page"])}</li>' for p in doc["points"]) + "</ul>"
    if doc and doc["disputes"]:
        ai += '<p style="margin:6px 0 2px"><b>Disputed</b></p><ul style="margin:0 0 4px;padding-left:20px">' + "".join(
            f'<li style="margin:2px 0">{esc(d["issue"])}<br>' + "<br>".join(
                f'<span style="{muted}">{esc(p["party"])}:</span> {esc(p["text"])}{pg(p["page"])}'
                for p in d["positions"]) + "</li>" for d in doc["disputes"]) + "</ul>"
    if doc and doc["proposed"]:
        ai += (f'<p style="margin:6px 0 2px"><b>Proposed dates</b> <span style="{muted}">(not yet set by the court)</span></p>'
               '<table style="border-collapse:collapse;font-size:13px;margin:0 0 4px">' + "".join(
                   f'<tr><td style="padding:2px 12px 2px 0;vertical-align:top">{esc(g["event"])}</td><td style="padding:2px 0">'
                   + "<br>".join(f'{esc(d["party"])}: {esc(d["date"])}{pg(d["page"])}' for d in g["dates"])
                   + "</td></tr>" for g in doc["proposed"]) + "</table>")
    if e["action"]:
        ai += f'<p style="margin:4px 0"><b>Action:</b> {esc(e["action"])}</p>'
    for d in e["deadlines"]:
        ai += (f'<p style="margin:4px 0"><b>Deadline{" (passed)" if d.get("passed") else ""}: {esc(d["date"])}</b> — {esc(d["what"])} '
               f'<span style="{muted}">“{esc(d["quote"])}”</span>{pg(d.get("page"))}</p>')
    if doc and doc["truncated"]:
        ai += (f'<p style="margin:4px 0;{muted}">Read pages 1–{esc(doc["truncated"][0])} of '
               f'{esc(doc["truncated"][1])}; the rest was not analyzed.</p>')
    return (f'<div style="border-left:3px solid {color};padding:4px 0 4px 10px;margin:0 0 12px">'
            f'<p style="margin:0;{muted}">#{esc(e["number"] or "?")} · Filed {esc(e["filed"])} · {esc(e["label"])}</p>'
            f'<p style="margin:4px 0">{esc(one_line(e["description"], 240) if e["summary"] else e["description"])}</p>'
            f'{ai}<p style="margin:4px 0">{links}</p></div>')


def html_view(cases, headline, stamp, footer, board=()):
    esc = lambda value: html.escape(str(value), quote=True)
    link = lambda url, text: f'<a href="{esc(url)}" style="color:#1a56db">{esc(text)}</a>'
    muted = "color:#6b7280;font-size:13px"
    colors = {"priority": "#b91c1c", "review": "#b45309", "other": "#111827"}
    parts = [f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;color:#111827;line-height:1.45">',
             f'<p style="margin:0 0 4px;font-weight:bold">{esc(headline)}</p><p style="margin:0 0 16px;{muted}">{esc(stamp)}</p>']
    if board:
        parts.append('<h2 style="font-size:16px;margin:20px 0 8px">Upcoming deadlines</h2>'
                     '<table style="border-collapse:collapse;font-size:14px">' + "".join(
                         f'<tr><td style="padding:2px 12px 2px 0;vertical-align:top;white-space:nowrap"><b>{esc(d["date"])}</b></td>'
                         f'<td style="padding:2px 0">{esc(d["label"])} <span style="{muted}">— {esc(d["refs"])}</span></td></tr>'
                         for d in board) + "</table>")
    for key, title in SECTIONS:
        if not any(c[key] for c in cases):
            continue
        parts.append(f'<h2 style="font-size:16px;margin:20px 0 8px;color:{colors[key]}">{esc(title)}</h2>')
        for case in (c for c in cases if c[key]):
            if key == "other":
                meta = f' <span style="{muted}">({esc(case["meta"])})</span>' if case["meta"] else ""
                parts.append(f'<p style="margin:8px 0 2px"><b>{esc(case["title"])}</b>{meta}</p><ul style="margin:0 0 8px;padding-left:20px">')
                groups, singles, undescribed = group(case[key])
                parts += [f'<li style="margin:2px 0">{esc(summary_line(g))}</li>' for g in groups]
                for e in singles:
                    parts.append(f'<li style="margin:2px 0">#{esc(e["number"] or "?")} · {esc(e["filed"])} · {esc(e["label"])} · '
                                 f'{esc(one_line(e["description"], 160))} {link(e["url"], "View")}</li>')
                if undescribed:
                    parts.append(f'<li style="margin:2px 0;{muted}">{esc(f"{undescribed} entry" if undescribed == 1 else f"{undescribed} entries")} without a description yet</li>')
                parts.append("</ul>")
                url, count = range_url(case)
                if url:
                    parts.append(f'<p style="margin:0 0 8px">{link(url, f"View these {count} entries on CourtListener")}</p>')
                continue
            parts.append(f'<h3 style="font-size:15px;margin:12px 0 2px">{link(case["url"], case["title"])}</h3>')
            if case["meta"]:
                parts.append(f'<p style="margin:0 0 8px;{muted}">{esc(case["meta"])}</p>')
            if key == "review":
                for heading, members in review_blocks(case[key]):
                    if heading:
                        parts.append(f'<p style="margin:8px 0 2px;font-weight:bold">{esc(heading)}</p>')
                    parts += [full_html(e, colors[key]) for e in members if e["document"]]
                    compact = [e for e in members if not e["document"]]
                    if compact:
                        parts.append(f'<ul style="margin:0 0 8px;padding-left:20px">' + "".join(
                            f'<li style="margin:3px 0"><span style="{muted}">#{esc(e["number"] or "?")} · {esc(e["filed"])}</span> '
                            f'{esc(short_line(e))} {link(e["url"], "View")}' + (" · " + link(e["pdf"], "PDF") if e["pdf"] else "")
                            + "</li>" for e in compact) + "</ul>")
                continue
            for e in case[key]:
                parts.append(full_html(e, colors[key]))
    if any(c["backfill"] for c in cases):
        parts.append('<h2 style="font-size:16px;margin:20px 0 8px;color:#6b7280">Older filings newly on CourtListener</h2>')
        for case in (c for c in cases if c["backfill"]):
            url, _ = range_url(case, "backfill")
            parts.append(f'<p style="margin:8px 0 2px"><b>{esc(case["title"])}</b></p>'
                         f'<p style="margin:0 0 8px">{esc(backfill_line(case))}' + (f' {link(url, "View")}' if url else "") + "</p>")
    parts.append(f'<hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0">'
                 + "".join(f'<p style="margin:0 0 6px;{muted}">{esc(line)}</p>' for line in footer) + "</div>")
    return "".join(parts)
