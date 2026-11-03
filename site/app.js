// Expert Match: read-only results website (GitHub Pages).
//
// The link carries an access key in its fragment (#key=...), which never leaves the browser except
// in calls to the two database functions (sql/migrations/007_matching_feed.postgres.sql); each
// checks the key before returning anything. config.js (written at deploy time) holds the Supabase
// URL and the publishable key, which alone can read nothing.
"use strict";

const CONFIG = window.EXPERT_MATCH || {};
const TIERS = { high: "High", medium: "Medium", low: "Low", none: "Not relevant", insufficient: "Insufficient information" };
const KEPT = "expert-match-key";
const WINDOWS = [7, 14, 30];
const app = document.getElementById("app");
const back = document.getElementById("back");
const cache = { feed: {}, runs: {} };

class AccessError extends Error {}

// ---- small helpers ----

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (name === "class") node.className = value;
    else node.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function params() {
  return new URLSearchParams(location.hash.slice(1));
}

function accessKey() {
  const key = params().get("key");
  if (key) {
    try { localStorage.setItem(KEPT, key); } catch (e) { /* private mode: the link still works */ }
    return key;
  }
  try { return localStorage.getItem(KEPT); } catch (e) { return null; }
}

// Navigation lives in the fragment next to the key, so a home-screen icon or bookmark keeps both.
function go(view) {
  const next = new URLSearchParams({ key: accessKey() || "" });
  for (const [name, value] of Object.entries(view)) if (value !== null && value !== undefined) next.set(name, value);
  location.hash = next.toString();
}

async function rpc(name, body) {
  const response = await fetch(`${CONFIG.url.replace(/\/$/, "")}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers: { apikey: CONFIG.key, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.ok) return response.json();
  const detail = await response.json().catch(() => ({}));
  if (/invalid access key/.test(detail.message || "")) throw new AccessError();
  throw new Error(`The server answered ${response.status}.`);
}

function day(iso) {
  return new Date(iso).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

function time(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function tierTag(tier) {
  return h("span", { class: `tier ${tier}` }, TIERS[tier] || tier);
}

function money(value) {
  return `$${Number(value).toFixed(2)}`;
}

function meta(...parts) {
  return parts.filter(Boolean).join(" · ");
}

function show(...nodes) {
  app.replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
  window.scrollTo(0, 0);
}

function problem(error) {
  if (error instanceof AccessError || !accessKey()) {
    return h("div", { class: "notice" }, h("h3", {}, "This link is not valid"),
      h("p", { class: "muted" }, "Open the full link you were sent, including everything after #key=. " +
        "If it still does not work, ask for a new link."));
  }
  return h("div", { class: "notice" }, h("h3", {}, "Could not load the results"),
    h("p", { class: "muted" }, error.message), h("button", { class: "btn", type: "button", onclick: route }, "Try again"));
}

// ---- list page: new cases by day ----

async function feed(days) {
  if (!cache.feed[days]) cache.feed[days] = await rpc("matching_feed", { access_key: accessKey(), days });
  return cache.feed[days];
}

function counts(expert) {
  const found = expert.candidates.length;
  const noPdf = expert.candidates.filter((c) => c.has_pdf === false).length;
  const out = expert.candidates.filter((c) => c.screen && !c.screen.relevant && !c.screen.unclear).length;
  const unclear = expert.candidates.filter((c) => c.screen && c.screen.unclear).length;
  return meta(`${found} new complaint${found === 1 ? "" : "s"}${noPdf ? ` (${noPdf} without a PDF)` : ""}`,
    `${out} screened out`, unclear && `${unclear} can't tell`, `${expert.cases.length} read and matched`,
    expert.skipped && `${expert.skipped} shown before`);
}

function expertCard(run, expert) {
  const wanted = expert.candidates.filter((c) => c.has_pdf === false && c.screen && c.screen.relevant);
  return h("section", { class: "card" },
    h("div", { class: "expert" }, h("h3", {}, expert.name || run.cv), h("span", { class: "muted small" }, time(run.started))),
    h("div", { class: "counts" }, counts(expert)),
    expert.cases.length ? null : wanted.length ? null : h("p", { class: "muted small" }, "No new relevant case in this window."),
    expert.cases.map((c, i) => h("button", { class: "case", type: "button", onclick: () => go({ run: run.run_id, case: i }) },
      h("div", { class: "top" }, tierTag(c.tier), h("span", { class: "score" }, `score ${Number(c.score).toFixed(1)}`)),
      h("div", { class: "name" }, c.name),
      h("div", { class: "muted small" }, meta(c.court, c.filed && `filed ${c.filed}`, c.case_type)),
      c.summary ? h("div", { class: "why small" }, c.summary) : null)),
    wanted.length ? h("div", { class: "nopdf" },
      h("div", { class: "small" }, h("b", {}, `Possibly relevant, complaint not on CourtListener (${wanted.length})`)),
      h("div", { class: "muted small" }, "Judged from the docket text only. Nothing is bought."),
      h("ul", { class: "small" }, wanted.map((c) => h("li", {},
        h("a", { href: c.url, target: "_blank", rel: "noopener" }, c.name),
        h("div", { class: "muted" }, meta(c.court, c.filed && `filed ${c.filed}`, `PACER ≤ ${money(c.cost || 3)}`)),
        h("div", {}, c.screen.reason))))) : null);
}

async function listPage() {
  back.hidden = true;
  const p = params();
  const days = WINDOWS.includes(Number(p.get("days"))) ? Number(p.get("days")) : 14;
  const who = p.get("cv");
  const runs = await feed(days);
  const names = [...new Set(runs.flatMap((r) => r.experts.map((e) => e.name || r.cv)))].sort();
  const byDay = new Map();
  for (const run of runs) {
    for (const expert of run.experts) {
      if (who && (expert.name || run.cv) !== who) continue;
      const key = day(run.started);
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key).push(expertCard(run, expert));
    }
  }
  show(
    h("h1", {}, "New cases by day"),
    h("p", { class: "muted small intro" }, "Each morning the system searches complaints filed in the last two days for every saved CV, " +
      "screens them, reads the relevant ones and matches them to the CV. Each case is shown once."),
    h("div", { class: "controls" },
      h("button", { class: "chip", type: "button", "aria-pressed": String(!who), onclick: () => go({ days }) }, "All experts"),
      names.map((n) => h("button", { class: "chip", type: "button", "aria-pressed": String(who === n), onclick: () => go({ days, cv: n }) }, n))),
    byDay.size ? [...byDay].map(([label, cards]) => h("div", {}, h("h2", { class: "day" }, label), cards))
      : h("p", { class: "muted" }, `No daily check in the last ${days} days.`),
    h("div", { class: "controls" }, h("span", { class: "muted small" }, "Show the last"),
      WINDOWS.map((d) => h("button", { class: "chip", type: "button", "aria-pressed": String(d === days), onclick: () => go({ days: d, cv: who }) }, `${d} days`))));
}

// ---- case page ----

async function run(id) {
  if (!cache.runs[id]) cache.runs[id] = await rpc("matching_run", { access_key: accessKey(), which_run: id });
  return cache.runs[id];
}

function side(label, quote, note) {
  return h("div", { class: "side" }, h("div", { class: "lbl" }, label), h("q", {}, quote), note ? h("div", { class: "muted" }, note) : null);
}

function reasons(match, doc) {
  if (!match.matches.length) return h("p", { class: "muted" }, "No specific match.");
  return match.matches.map((m) => h("div", { class: "reason" },
    h("div", {}, h("span", { class: "strength" }, m.strength), " · ", m.reason),
    h("div", { class: "pair" },
      side(`CV ${m.expert.id} · ${m.expert.kind} · p. ${m.expert.page}`, m.expert.quote, m.expert.summary),
      side(`${doc} ${m.point.id} · p. ${m.point.page}`, m.point.quote, m.point.issue || m.point.area))));
}

function emailBox(match, profile) {
  const mail = match.email;
  if (!mail) return h("p", { class: "muted" }, "No draft: drafts are written for High and Medium matches only.");
  const counsel = (profile.counsel || []).filter((c) => !mail.to_party || c.party === mail.to_party).slice(0, 4);
  const subject = h("input", { value: mail.subject });
  const body = h("textarea", {}, mail.body);
  const status = h("span", { class: "muted small" });
  const open = (base) => () => {
    const q = `subject=${encodeURIComponent(subject.value)}&body=${encodeURIComponent(body.value)}`;
    window.open(base === "gmail" ? `https://mail.google.com/mail/?view=cm&fs=1&su=${encodeURIComponent(subject.value)}&body=${encodeURIComponent(body.value)}` : `mailto:?${q}`, "_blank");
  };
  const copy = async () => {
    try { await navigator.clipboard.writeText(`Subject: ${subject.value}\n\n${body.value}`); status.textContent = "Copied."; }
    catch (e) { status.textContent = "Copy failed; select the text instead."; }
  };
  return h("div", {},
    h("p", { class: "small" }, h("b", {}, "To: "), counsel.length ? counsel.map((c) => `${c.name}${c.firm ? `, ${c.firm}` : ""}`).join("; ") : "counsel of record",
      mail.to_party ? h("span", { class: "muted" }, ` (for ${mail.to_party})`) : null),
    h("p", { class: "muted small" }, "Lawyers' email addresses are not filled in yet. Edit the draft here, then open it in your email app; nothing is sent automatically."),
    h("label", {}, "Subject"), subject, h("label", {}, "Message"), body,
    h("div", { class: "buttons" },
      h("button", { class: "btn", type: "button", onclick: open("gmail") }, "Open in Gmail"),
      h("button", { class: "btn light", type: "button", onclick: open("mail") }, "Open in Mail app"),
      h("button", { class: "btn light", type: "button", onclick: copy }, "Copy"), status));
}

async function casePage(id, index) {
  back.hidden = false;
  const data = await run(id);
  const expert = data && data.result && data.result[0];
  const item = expert && expert.results[index];
  if (!item) return show(h("div", { class: "notice" }, h("h3", {}, "Case not found"), h("p", { class: "muted" }, "It may be older than the results kept here.")));
  const { case: c, profile, match, score } = item;
  const doc = c.document_short || "Complaint";
  show(
    h("p", { class: "muted small" }, `For ${expert.profile.name} · checked ${day(data.started)}`),
    h("h1", {}, c.name),
    h("p", { class: "muted small" }, meta(c.court, c.docket_number, c.filed && `filed ${c.filed}`)),
    h("div", { class: "top" }, tierTag(match.tier), " ", h("span", { class: "score" }, `score ${Number(score.total).toFixed(1)} · ${score.direct} direct, ${score.related} related`)),
    h("div", { class: "links" },
      c.url ? h("a", { href: c.url, target: "_blank", rel: "noopener" }, "Docket on CourtListener") : null,
      c.document_url ? h("a", { href: c.document_url, target: "_blank", rel: "noopener" }, `${doc} PDF${c.total_pages ? ` (${c.total_pages} pages)` : ""}`) : null),
    h("div", { class: "section" }, h("h2", {}, "Why it fits"), h("p", {}, match.summary || "")),
    profile ? h("div", { class: "section" }, h("h2", {}, "The case"),
      h("p", {}, profile.summary),
      h("div", { class: "tags" }, [profile.case_type, ...profile.domains].filter(Boolean).map((t) => h("span", { class: "tag" }, t))),
      h("p", { class: "small" }, h("b", {}, "Plaintiffs: "), profile.plaintiffs.join("; ")),
      h("p", { class: "small" }, h("b", {}, "Defendants: "), profile.defendants.join("; "))) : null,
    h("div", { class: "section" }, h("h2", {}, `Match reasons (CV ↔ ${doc.toLowerCase()})`), reasons(match, doc)),
    match.gaps && match.gaps.length ? h("div", { class: "section" }, h("h2", {}, "Gaps"), h("ul", {}, match.gaps.map((g) => h("li", {}, g)))) : null,
    profile ? h("div", { class: "section" }, h("h2", {}, `Disputed issues (${profile.issues.length})`),
      h("ul", {}, profile.issues.map((i) => h("li", {}, i.issue, h("span", { class: "muted" }, ` (${i.id}, p. ${i.page})`))))) : null,
    profile && profile.expertise.length ? h("div", { class: "section" }, h("h2", {}, "Expertise the case needs"),
      h("ul", {}, profile.expertise.map((x) => h("li", {}, h("b", {}, x.area), `: ${x.why}`, h("span", { class: "muted" }, ` (${x.id}, p. ${x.page})`))))) : null,
    profile && profile.counsel.length ? h("div", { class: "section" }, h("h2", {}, "Counsel"),
      h("ul", {}, profile.counsel.map((x) => h("li", {}, h("b", {}, x.name), x.firm ? `, ${x.firm}` : "", h("span", { class: "muted" }, ` for ${x.party} (p. ${x.page})`))))) : null,
    h("div", { class: "section" }, h("h2", {}, "Email draft"), emailBox(match, profile || {})),
    h("p", { class: "muted small section" }, "Summaries and reasons are written by AI. Every quote shown was checked against the CV or the complaint."));
}

// ---- routing ----

async function route() {
  if (!CONFIG.url || !CONFIG.key) return show(h("div", { class: "notice" }, h("h3", {}, "Not configured"), h("p", { class: "muted" }, "config.js is missing.")));
  if (!accessKey()) return show(problem(new AccessError()));
  const p = params();
  try {
    if (p.get("run") && p.get("case") !== null) await casePage(p.get("run"), Number(p.get("case")));
    else await listPage();
  } catch (error) {
    show(problem(error));
  }
}

back.addEventListener("click", () => (history.length > 1 ? history.back() : go({})));
window.addEventListener("hashchange", route);
route();
