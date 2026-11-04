"""What happened in the cloud, for `python -m src.matching.cloud --dotenv log` (the admin Logs page
in site/app.js shows the same, with each run's steps and model calls).

The data is matching_log_data (migration 018): the daily schedule's firings (pg_cron), GitHub
dispatch answers (pg_net, kept about 6 hours), matching runs, upload requests, docket pipeline
runs and saved CVs.
"""
from datetime import datetime, timedelta, timezone


def when(value):
    """A stored time (ISO text, with or without zone) as an aware datetime, or None."""
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def stamp(value):
    return str(value)[:16].replace("T", " ")


def took(start, end):
    """'4m12s' between two stored times, or '-'."""
    a, b = when(start), when(end)
    if not a or not b:
        return "-"
    seconds = max(0, int((b - a).total_seconds()))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def unchecked(data, now=None):
    """Saved CVs with daily checks on but no daily check in the last 24 hours (saved before that)."""
    now = now or datetime.now(timezone.utc)
    recent = {r["cv"] for r in data["runs"] if r["daily"] and when(r["started"]) >= now - timedelta(hours=24)}
    return [c["name"] for c in data["cvs"]
            if c["active"] and c["name"] not in recent and when(c["saved_at"]) < now - timedelta(hours=24)]


def run_line(r):
    stats, usage = r.get("stats") or {}, r.get("usage") or {}
    tiers = ", ".join(f"{n} {t}" for t, n in sorted((r.get("tiers") or {}).items()))
    parts = [stamp(r["started"]), f"{r['cv']} ({r['owner'] or 'admin'})",
             "daily" if r["daily"] else "upload", r["state"].upper() if r["state"] != "done" else "done",
             took(r["started"], r["finished"]),
             f"{stats.get('calls', 0)} AI calls ({stats.get('cached', 0)} cached)" if stats else None,
             f"{usage.get('courtlistener', 0)} CourtListener, {usage.get('web_searches', 0)} web" if usage else None,
             f"{r['candidates']} found, {r['cases']} read" + (f" [{tiers}]" if tiers else "")]
    line = "  " + " · ".join(p for p in parts if p)
    return line + (f"\n      error: {r['error']}" if r.get("error") else "")


def report(data, now=None):
    lines = [f"Since {data['since']} UTC"]
    lines.append("Daily schedule (pg_cron, 11:30 UTC):" + ("" if data["schedule"] else " no firing recorded"))
    lines += [f"  {stamp(s['at'])}  {s['status']}" + (f"  {s['message']}" if s["status"] != "succeeded" else "")
              for s in data["schedule"]]
    if data["dispatches"]:
        bad = [d for d in data["dispatches"] if not (d["status"] and 200 <= d["status"] < 300)]
        lines.append(f"GitHub dispatches (last hours only): {len(data['dispatches'])}, {len(bad)} not accepted")
        lines += [f"  {stamp(d['at'])}  HTTP {d['status']}  {d.get('error') or ''}" for d in bad]
    lines.append(f"Matching runs: {len(data['runs'])}, {sum(r['state'] != 'done' for r in data['runs'])} not done")
    lines += [run_line(r) for r in data["runs"]]
    missing = unchecked(data, now)
    if missing:
        lines.append("Saved CVs with no daily check in the last 24 hours: " + ", ".join(missing))
    failed = [q for q in data["requests"] if q["state"] == "failed"]
    lines.append(f"Uploads: {len(data['requests'])}, {len(failed)} failed")
    lines += [f"  {stamp(q['created'])}  {q['cv']} ({q['owner'] or 'admin'}): {q['error']}" for q in failed]
    bad = [p for p in data["pipeline"] if p["status"] != "ok"]
    lines.append(f"Docket monitor runs: {len(data['pipeline'])}, {len(bad)} failed")
    for p in bad:
        stages = (p.get("report") or {}).get("stages") or {}
        errors = "; ".join(f"{name}: {s.get('error') or s.get('failed')}" for name, s in stages.items() if s.get("status") == "failed")
        lines.append(f"  {stamp(p['started'])}  {errors or p['status']}")
    return "\n".join(lines)
