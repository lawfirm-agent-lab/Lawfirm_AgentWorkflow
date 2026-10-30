"""Self-contained HTML report: one ranked table per expert.

Columns follow the requested output: Case | Match judgment | Match reasons | Sources | Email draft.
Everything shown is either verified source text (quotes, with page) or labelled AI reading.
"""
from html import escape

from src.matching.match import LABELS

SHOWN_REASONS = 3  # strongest first; the rest are one click away

STYLE = """
:root{--bg:#fff;--fg:#1d2329;--muted:#5f6b76;--line:#d9dee3;--card:#f6f8fa;--quote:#eef2f6;
--high:#0f7b3f;--medium:#9a6700;--low:#57606a;--none:#8c959f;--insufficient:#6639ba;--warn:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#0f1419;--fg:#e6edf3;--muted:#9aa7b3;--line:#30363d;--card:#161b22;
--quote:#1c232b;--high:#3fb950;--medium:#d29922;--low:#9aa7b3;--none:#6e7681;--insufficient:#a371f7;--warn:#f85149}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}
main{max-width:1400px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 4px}
.muted{color:var(--muted)}.expert{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:8px 0 12px}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:1100px}th,td{border-top:1px solid var(--line);padding:10px 8px;vertical-align:top;text-align:left}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}td.case{width:22%}td.judge{width:16%}td.reasons{width:32%}td.src{width:12%}td.mail{width:18%}
.tier{display:inline-block;font-weight:600;border-radius:12px;padding:1px 10px;color:#fff;font-size:12px}
.t-high{background:var(--high)}.t-medium{background:var(--medium)}.t-low{background:var(--low)}.t-none{background:var(--none)}.t-insufficient{background:var(--insufficient)}
.tag{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:0 8px;margin:2px 2px 0 0;font-size:12px;color:var(--muted)}
.reason{margin:0 0 10px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:4px}
q,.q{display:block;background:var(--quote);border-left:3px solid var(--line);padding:4px 8px;font-size:12px;quotes:none;border-radius:4px}
.lbl{font-size:11px;color:var(--muted);text-transform:uppercase}.stated{color:var(--high);font-weight:600}.inferred{color:var(--medium);font-weight:600}
.warn{color:var(--warn);font-size:12px}details{margin-top:6px}summary{cursor:pointer;color:var(--muted)}
pre{white-space:pre-wrap;font:13px/1.5 inherit;font-family:inherit;background:var(--quote);padding:8px;border-radius:4px;margin:6px 0 0}
a{color:inherit}ul{margin:4px 0;padding-left:18px}.legend{font-size:12px;color:var(--muted);margin-top:16px}
button.copy{margin-top:6px;font:inherit;font-size:12px;padding:2px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
nav{display:flex;gap:16px;align-items:center;margin-bottom:12px;font-size:13px;flex-wrap:wrap}
nav.tabs{gap:4px;border-bottom:1px solid var(--line);padding-bottom:8px;margin-bottom:16px}
.tab{padding:4px 10px;border-radius:6px;text-decoration:none}.tab.on{background:var(--fg);color:var(--bg)}
h3{font-size:15px;margin:20px 0 4px}details.group{margin-top:16px}details.group>summary{font-size:15px}
.filters{margin:10px 0}.chip{font:inherit;font-size:12px;margin:2px;padding:2px 10px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
.chip.on{background:var(--fg);color:var(--bg)}.score{font-size:12px;margin-top:4px}code{font-size:12px}
"""
SCRIPT = """function copyDraft(b){navigator.clipboard.writeText(b.previousElementSibling.innerText).then(function(){
b.textContent='Copied';setTimeout(function(){b.textContent='Copy'},1500)})}
function filterRows(b){var s=b.closest('section'),t=b.dataset.tag;
s.querySelectorAll('.chip').forEach(function(c){c.classList.toggle('on',c===b)});
s.querySelectorAll('tr[data-tags]').forEach(function(r){r.hidden=!!t&&r.dataset.tags.split('|').indexOf(t)<0})}"""


def e(value):
    return escape(str(value or ""))


def tier(value):
    return f'<span class="tier t-{value}">{e(LABELS[value])}</span>'


def tags(values):
    return "".join(f'<span class="tag">{e(v)}</span>' for v in values)


def case_cell(case, profile):
    link = f'<a href="{e(case["url"])}">{e(case["name"])}</a>' if case.get("url") else e(case["name"])
    meta = " · ".join(e(v) for v in (case.get("court"), case.get("docket_number"), case.get("filed") and f"filed {case['filed']}") if v)
    if not profile:
        return f"<b>{link}</b><div class='muted'>{meta}</div><div class='muted'>{e(case.get('note'))}</div>"
    issues = "".join(f"<li>{e(i['issue'])} <span class='muted'>({i['id']}, p. {i['page']})</span></li>" for i in profile["issues"])
    areas = "".join(f"<li>{e(x['area'])} — <span class='{x['basis']}'>{x['basis']}</span>: {e(x['why'])} "
                    f"<span class='muted'>({x['id']}, p. {x['page']})</span></li>" for x in profile["expertise"])
    return (f"<b>{link}</b><div class='muted'>{meta}</div><div>{tags([profile['case_type']] + profile['domains'])}</div>"
            f"<p>{e(profile['summary'])}</p>"
            f"<details><summary>Case profile ({len(profile['issues'])} issues, {len(profile['expertise'])} expertise areas)</summary>"
            f"<div class='lbl'>Disputed issues</div><ul>{issues}</ul><div class='lbl'>Expertise possibly relevant</div><ul>{areas}</ul>"
            f"<div class='lbl'>Technology</div>{tags(profile['technology'])}</details>")


def reasons_cell(match, doc_label):
    if not match["matches"]:
        gaps = "".join(f"<li>{e(g)}</li>" for g in match["gaps"])
        return f"<span class='muted'>No specific match.</span>{f'<ul>{gaps}</ul>' if gaps else ''}"
    rows = []
    for m in match["matches"]:
        x, p = m["expert"], m["point"]
        what = p.get("issue") or p.get("area")
        rows.append(f"<div class='reason'><b>{e(m['strength']).capitalize()}</b> · {e(m['reason'])}<div class='pair'>"
                    f"<div><span class='lbl'>CV {x['id']}, p. {x['page']}</span><q>{e(x['quote'])}</q></div>"
                    f"<div><span class='lbl'>{e(doc_label)} {p['id']}, p. {p['page']}</span><q>{e(p['quote'])}</q>"
                    f"<span class='muted'>{e(what)}</span></div></div></div>")
    gaps = "".join(f"<li>{e(g)}</li>" for g in match["gaps"])
    shown, more = rows[:SHOWN_REASONS], rows[SHOWN_REASONS:]
    rest = f"<details><summary>{len(more)} more reason{'s' if len(more) > 1 else ''}</summary>{''.join(more)}</details>" if more else ""
    return "".join(shown) + rest + (f"<div class='lbl'>Gaps</div><ul>{gaps}</ul>" if gaps else "")


def sources_cell(case, match, profile):
    cv_pages = sorted({m["expert"]["page"] for m in match["matches"]})
    doc_pages = sorted({m["point"]["page"] for m in match["matches"]})
    doc = f'<a href="{e(case["document_url"])}">{e(case["document_label"])}</a>' if case.get("document_url") else e(case.get("document_label"))
    parts = [f"<div class='lbl'>Case</div>{doc}" + (f"<div class='muted'>pp. {', '.join(map(str, doc_pages))}</div>" if doc_pages else "")]
    if profile:
        parts.append(f"<div class='muted'>{profile['pages_read']} of {profile['pages']} pages read</div>")
    if cv_pages:
        parts.append(f"<div class='lbl'>Expert CV</div><div class='muted'>pp. {', '.join(map(str, cv_pages))}</div>")
    if case.get("url"):
        parts.append(f"<div class='lbl'>Docket</div><a href='{e(case['url'])}'>CourtListener</a>")
    return "".join(parts)


def mail_cell(match, profile):
    notes = "".join(f"<div class='warn'>{e(n)}</div>" for n in match["notes"])
    if not match["email"]:
        return f"<span class='muted'>No draft (tier below Medium).</span>{notes}"
    mail = match["email"]
    counsel = [c for c in (profile or {}).get("counsel", []) if not mail.get("to_party") or (c.get("party") or "") == mail["to_party"]]
    to = "; ".join(f"{c['name']}{', ' + c['firm'] if c.get('firm') else ''}" for c in counsel[:4]) or "counsel of record"
    return (f"<div><span class='lbl'>To</span> {e(to)}{' (' + e(mail['to_party']) + ')' if mail.get('to_party') else ''}</div>"
            f"<div><span class='lbl'>Subject</span> {e(mail['subject'])}</div>"
            f"<details><summary>Show draft</summary><pre>{e(mail['body'])}</pre>"
            f"<button type='button' class='copy' onclick='copyDraft(this)'>Copy</button></details>{notes}")


GROUPS = ("high", "medium", "low", "insufficient", "none")
HEAD = ("<thead><tr><th>#</th><th>Case</th><th>Match judgment</th><th>Match reasons (CV ↔ case document)</th>"
        "<th>Sources</th><th>Email draft</th></tr></thead>")


def score_line(result):
    s = result.get("score")
    if not s or result["match"]["tier"] in ("insufficient", "none"):
        return ""
    p = s["parts"]
    bits = [f"evidence {p['evidence']} ({s['direct']} direct, {s['related']} related)"]
    bits += [f"stated expertise {p['stated']}"] if p["stated"] else []
    bits += [f"technical {p['technical']}", f"recent {p['recent']}"]
    return f"<div class='muted score'>Score {s['total']} = {' + '.join(bits)}</div>"


def row(rank, r):
    case, match, cp = r["case"], r["match"], r["profile"]
    labels = ((cp or {}).get("domains") or []) + ([cp["case_type"]] if cp else [])
    return (f"<tr data-tags='{e('|'.join(labels))}'><td>{rank}</td><td class='case'>{case_cell(case, cp)}</td>"
            f"<td class='judge'>{tier(match['tier'])}<div>{tags((cp or {}).get('domains', []))}</div>"
            f"<p>{e(match['summary'])}</p>{score_line(r)}</td>"
            f"<td class='reasons'>{reasons_cell(match, case.get('document_short', 'Doc'))}</td>"
            f"<td class='src'>{sources_cell(case, match, cp)}</td><td class='mail'>{mail_cell(match, cp)}</td></tr>")


def discovery_block(discovery):
    if not discovery:
        return ""
    candidates = discovery["candidates"]
    screened = [c for c in candidates if c.get("screen")]
    relevant = [c for c in screened if c["screen"]["relevant"]]
    kept = [c for c in candidates if c.get("kept")]
    def search_row(s):
        error = f"<div class='warn'>Not run: {e(s['error'])}</div>" if s.get("error") else ""
        return f"<tr><td>{e(s['label'])}</td><td><code>{e(s['terms'])}</code></td><td>{s.get('results', 0)}{error}</td></tr>"
    searches = "".join(search_row(s) for s in discovery["searches"])

    def listing(values):
        return "".join(f"<li><a href='{e(c['url'])}'>{e(c['name'])}</a> <span class='muted'>({e(c['court'])}, filed {e(c['filed'])}, "
                       f"via {e(c['found_by'])})</span><br><span class='muted'>{e((c.get('screen') or {}).get('reason') or c.get('download_error'))}"
                       f"</span></li>" for c in values)
    later = [c for c in relevant if not c.get("kept")]
    rejected = [c for c in candidates if not (c.get("screen") or {}).get("relevant")]
    return (f"<details class='expert'><summary><b>How these cases were found</b>: {len(discovery['searches'])} searches of "
            f"complaints filed in the last {discovery['days']} days → {len(screened)} candidates screened → {len(relevant)} relevant "
            f"→ {len(kept)} read in full</summary>"
            f"<table><thead><tr><th>Search (written from the CV)</th><th>CourtListener query terms</th><th>Complaints</th></tr></thead>"
            f"<tbody>{searches}</tbody></table>"
            f"<div class='lbl'>Read in full ({len(kept)})</div><ul>{listing(kept)}</ul>"
            f"<details><summary>Relevant, not read yet ({len(later)})</summary><ul>{listing(later)}</ul></details>"
            f"<details><summary>Filtered out at screening ({len(rejected)})</summary><ul>{listing(rejected)}</ul></details>"
            f"</details>")


def filters(results):
    labels = []
    for r in results:
        cp = r["profile"] or {}
        for label in (cp.get("domains") or []) + ([cp["case_type"]] if cp.get("case_type") else []):
            if label not in labels:
                labels.append(label)
    if len(labels) < 2:
        return ""
    chips = "".join(f"<button type='button' class='chip' onclick='filterRows(this)' data-tag='{e(t)}'>{e(t)}</button>" for t in labels)
    return f"<div class='filters'><span class='lbl'>Filter</span> <button type='button' class='chip on' onclick='filterRows(this)' data-tag=''>All</button>{chips}</div>"


def render(experts, generated, model, nav=""):
    """nav: optional trusted HTML shown above the title (links back into the local web app)."""
    sections = []
    for expert in experts:
        profile, results = expert["profile"], expert["results"]
        counts = {}
        for r in results:
            counts[r["match"]["tier"]] = counts.get(r["match"]["tier"], 0) + 1
        summary = " ".join(f"{tier(t)} {counts[t]}" for t in GROUPS if t in counts)
        groups, rank = [], 0
        for group in GROUPS:
            members = [r for r in results if r["match"]["tier"] == group]
            if not members:
                continue
            rows = "".join(row(rank + n, r) for n, r in enumerate(members, 1))
            rank += len(members)
            table = f"<div class='wrap'><table>{HEAD}<tbody>{rows}</tbody></table></div>"
            title = f"{tier(group)} {len(members)} case{'s' if len(members) > 1 else ''}"
            groups.append(f"<details class='group'><summary>{title} (collapsed)</summary>{table}</details>" if group == "none"
                          else f"<h3>{title}</h3>{table}")
        items = "".join(f"<li>{e(i['summary'])} <span class='muted'>({i['id']}, {e(i['kind'])}, p. {i['page']})</span></li>" for i in profile["items"])
        sections.append(
            f"<section><h2>{e(profile['name'])}</h2><div class='expert'><div>{e(profile['headline'])}</div><div>{tags(profile['domains'])}</div>"
            f"<details><summary>{len(profile['items'])} verified experience items from the CV</summary><ul>{items}</ul></details></div>"
            f"{discovery_block(expert.get('discovery'))}<div>{summary}</div>{filters(results)}{''.join(groups)}"
            f"<div class='legend'>Ranked by tier, then score within the tier. Score = evidence (2 per direct + 1 per related "
            f"verified reason) + 1 if the document itself states the expertise is needed + share of technical issues + recency "
            f"(1 if filed within 90 days, 0.5 within a year).</div></section>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Expert Case Matching</title><style>{STYLE}</style><script>{SCRIPT}</script></head>"
            f"<body><main>{nav}<h1>Expert–case matching</h1>"
            f"<div class='muted'>Generated {e(generated)} · model {e(model)} · prototype</div>{''.join(sections)}"
            f"<div class='legend'>Every quote shown was checked by code against the CV or the case document (page cited). "
            f"Case summaries, reasons and emails are AI reading of those quotes and must be reviewed before use. "
            f"<span class='stated'>stated</span> = the document itself says this expertise is needed; "
            f"<span class='inferred'>inferred</span> = the system's reading. Insufficient information = no readable case document, "
            f"which is not the same as not relevant. Emails are drafts for the expert to review; nothing is sent.</div></main></body></html>")
