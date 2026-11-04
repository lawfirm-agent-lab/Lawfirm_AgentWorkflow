"""Feedback on matched cases (migrations 016, 017): statistics and exports.

Rows are what matching_feedback_all returns (CloudRuns.feedback reads the same from the table):
who, run_id, case_index, case_name, cv, kind ("rating" or "email"), verdict ("up"/"down", or for
email "gmail"/"mail"/"copy"), reason, should_be, would_contact, note, current, at, and snapshot
(what the system decided then: tier, score, source, model, prompt_version...).

A person gives one rating per case, saved only when they submit it (migrations 019, 020). Email
rows are counted ("drafts opened email") but are not ratings. The website's Feedback page
(site/app.js) shows the same figures.
"""
import csv
import io
from collections import Counter, defaultdict

REASONS = {"not_relevant": "Not relevant to this expert", "wrong_level": "Relevant, but the level is wrong",
           "misread": "The case was misread", "contact_info": "Lawyer or email details are wrong",
           "already_known": "Already known", "other": "Other"}
LEVELS = ("high", "medium", "low", "none")
COLUMNS = ("at", "who", "cv", "case_name", "case_url", "run_id", "case_index", "kind", "verdict", "reason", "should_be",
           "would_contact", "note", "current", "tier", "model_tier", "score", "direct", "related", "reasons",
           "case_type", "court", "filed", "docket_number", "source", "web_source", "pages", "pages_read",
           "screen_relevant", "screen_reason", "expert", "daily", "run_started", "model", "prompt_version")


def ratings(rows):
    return [r for r in rows if r["kind"] == "rating" and r["current"]]


def rate(group):
    return round(100 * sum(r["verdict"] == "up" for r in group) / len(group)) if group else None


def by(rows, key):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r) or "unknown"].append(r)
    return {name: {"n": len(g), "useful": rate(g)} for name, g in sorted(groups.items())}


def source(r):
    snap = r.get("snapshot") or {}
    return {"courtlistener": "CourtListener PDF", "web complaint": "Complaint found on the web",
            "web web page": "Web page only"}.get(snap.get("source"), snap.get("source"))


def summarize(rows):
    rated = ratings(rows)
    down = [r for r in rated if r["verdict"] == "down"]
    emailed = {(r["run_id"], r["case_index"], r["who"]) for r in rows if r["kind"] == "email"}
    levels = Counter((r["snapshot"].get("tier"), r["should_be"]) for r in rated if r.get("should_be"))
    return {
        "ratings": len(rated), "useful": rate(rated), "would_contact": sum(bool(r["would_contact"]) for r in rated),
        "emailed": len(emailed),
        "by_tier": by(rated, lambda r: r["snapshot"].get("tier")), "by_source": by(rated, source),
        "by_version": by(rated, lambda r: f"{r['snapshot'].get('model')} / prompts {r['snapshot'].get('prompt_version')}"),
        "by_person": by(rated, lambda r: r["who"]), "by_cv": by(rated, lambda r: r["cv"]),
        "reasons": dict(Counter(REASONS.get(r["reason"], "No reason given") for r in down).most_common()),
        # (system level, level the person says it should be) -> count
        "levels": {f"{tier} -> {should}": n for (tier, should), n in sorted(levels.items(), key=lambda kv: str(kv[0]))},
        "attention": [r for r in down if r["snapshot"].get("tier") == "high"],
    }


def flat(row):
    snap = row.get("snapshot") or {}
    screen = snap.get("screen") or {}
    out = {**{k: row.get(k) for k in COLUMNS}, **{k: snap.get(k) for k in COLUMNS if k in snap}}
    out.update(screen_relevant=screen.get("relevant"), screen_reason=screen.get("reason"))
    return out


def csv_text(rows):
    """One line per feedback row, ratings and email actions, in the column order of COLUMNS (opens in Excel)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(flat(row))
    return buffer.getvalue()


def report(summary):
    """Plain-text summary for the terminal."""
    if not summary["ratings"]:
        return "No ratings yet."
    pct = lambda v: "-" if v is None else f"{v}%"
    lines = [f"{summary['ratings']} ratings, {pct(summary['useful'])} useful, {summary['would_contact']} would contact, "
             f"{summary['emailed']} drafts opened email"]
    for title, key in (("By level", "by_tier"), ("By source", "by_source"), ("By version", "by_version"),
                       ("By person", "by_person"), ("By CV", "by_cv")):
        lines.append(f"{title}: " + " · ".join(f"{k} {pct(v['useful'])} of {v['n']}" for k, v in summary[key].items()))
    if summary["reasons"]:
        lines.append("Not useful because: " + " · ".join(f"{k} {n}" for k, n in summary["reasons"].items()))
    if summary["levels"]:
        lines.append("Level given -> level it should be: " + " · ".join(f"{k} ({n})" for k, n in summary["levels"].items()))
    for r in summary["attention"]:
        lines.append(f"Needs attention (High, not useful): {r['case_name']} for {r['cv']} by {r['who']}"
                     f" - {REASONS.get(r['reason'], 'no reason')}{': ' + r['note'] if r.get('note') else ''}")
    return "\n".join(lines)
