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
const whoLabel = document.getElementById("who");
const cache = { feed: {}, runs: {}, me: null };
const MAX_UPLOAD = 10 * 1024 * 1024;
let poll = null;  // Timer of the progress page; stopped when the page changes.

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
function go(view, replace = false) {
  const next = new URLSearchParams({ key: accessKey() || "" });
  for (const [name, value] of Object.entries(view)) if (value !== null && value !== undefined) next.set(name, value);
  if (!replace) return void (location.hash = next.toString());
  history.replaceState(null, "", `#${next}`);  // No history entry, so Back skips this page.
  route();
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
  throw new Error(detail.message || `The server answered ${response.status}.`);
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

// Who this link belongs to: {who, admin}. Each person sees only their own CVs and results.
async function me() {
  if (!cache.me) cache.me = await rpc("matching_whoami", { access_key: accessKey() });
  whoLabel.textContent = `${cache.me.who}${cache.me.admin ? " · admin" : ""}`;
  return cache.me;
}

function ownerNote(owner) {
  return cache.me && cache.me.admin ? `owner: ${owner || "admin"}` : null;
}

// ---- list page: new cases by day ----

async function feed(days) {
  if (!cache.feed[days]) cache.feed[days] = await rpc("matching_feed", { access_key: accessKey(), days });
  return cache.feed[days];
}

function counts(expert, fresh = true) {
  const found = expert.candidates.length;
  const noPdf = expert.candidates.filter((c) => c.has_pdf === false).length;
  const out = expert.candidates.filter((c) => c.screen && !c.screen.relevant && !c.screen.unclear).length;
  const unclear = expert.candidates.filter((c) => c.screen && c.screen.unclear).length;
  return meta(`${found} ${fresh ? "new " : ""}complaint${found === 1 ? "" : "s"} found${noPdf ? ` (${noPdf} without a PDF)` : ""}`,
    `${out} screened out`, unclear && `${unclear} can't tell`, `${expert.cases.length} read and matched`,
    expert.skipped && `${expert.skipped} shown before`);
}

function expertCard(run, expert) {
  const wanted = expert.candidates.filter((c) => c.has_pdf === false && c.screen && c.screen.relevant);
  return h("section", { class: "card" },
    h("div", { class: "expert" }, h("h3", {}, expert.name || run.cv), h("span", { class: "muted small" }, meta(ownerNote(run.owner), time(run.started)))),
    h("div", { class: "counts" }, counts(expert, run.daily !== false)),
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
        h("div", {}, c.screen.reason),
        c.elsewhere && c.elsewhere.url ? h("div", {}, "Found elsewhere: ", h("a", { href: c.elsewhere.url, target: "_blank", rel: "noopener" }, c.elsewhere.source || "link")) : null)))) : null);
}

async function listPage() {
  back.hidden = true;
  const p = params();
  const days = WINDOWS.includes(Number(p.get("days"))) ? Number(p.get("days")) : 14;
  const who = p.get("cv");
  const runs = await feed(days);
  const names = [...new Set(runs.flatMap((r) => r.experts.map((e) => e.name || r.cv)))].sort();
  const byDay = new Map();
  const uploads = [];
  for (const run of runs) {
    for (const expert of run.experts) {
      if (who && (expert.name || run.cv) !== who) continue;
      if (run.daily === false) {
        uploads.push(h("div", {}, h("h2", { class: "day" }, `Uploaded ${day(run.started)}`), expertCard(run, expert)));
        continue;
      }
      const key = day(run.started);
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key).push(expertCard(run, expert));
    }
  }
  show(
    h("div", { class: "headline" }, h("h1", {}, "New cases by day"),
      h("div", { class: "buttons tight" },
        h("button", { class: "btn", type: "button", onclick: () => go({ view: "upload" }) }, "Upload a CV"),
        h("button", { class: "btn light", type: "button", onclick: () => go({ view: "cvs" }) }, "Saved CVs"))),
    h("p", { class: "muted small intro" }, "Each morning the system searches complaints filed in the last two days for every saved CV, " +
      "screens them, reads the relevant ones and matches them to the CV. Each case is shown once."),
    h("div", { class: "controls" },
      h("button", { class: "chip", type: "button", "aria-pressed": String(!who), onclick: () => go({ days }) }, "All experts"),
      names.map((n) => h("button", { class: "chip", type: "button", "aria-pressed": String(who === n), onclick: () => go({ days, cv: n }) }, n))),
    byDay.size ? [...byDay].map(([label, cards]) => h("div", {}, h("h2", { class: "day" }, label), cards))
      : runs.length ? h("p", { class: "muted" }, `No daily check in the last ${days} days.`)
        : h("div", { class: "notice" }, h("h3", {}, "Nothing here yet"),
          h("p", { class: "muted" }, "Upload a CV to find matching cases. Tick \u201cSave this CV for daily checks\u201d " +
            "and new cases for it will appear here every morning. You only see the CVs you upload.")),
    uploads.length ? h("div", { class: "section" }, h("h1", { class: "sub" }, "From uploaded CVs"),
      h("p", { class: "muted small" }, "Complaints filed in the year before the upload."), uploads) : null,
    h("div", { class: "controls" }, h("span", { class: "muted small" }, "Show the last"),
      WINDOWS.map((d) => h("button", { class: "chip", type: "button", "aria-pressed": String(d === days), onclick: () => go({ days: d, cv: who }) }, `${d} days`))));
}

// ---- upload a CV, follow its run, show its results ----

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsDataURL(file);
  });
}

function uploadPage() {
  back.hidden = false;
  const file = h("input", { type: "file", accept: "application/pdf,.pdf" });
  const save = h("input", { type: "checkbox", checked: true });
  const status = h("p", { class: "muted small" });
  const submit = h("button", { class: "btn", type: "submit" }, "Find cases");
  const form = h("form", { class: "card", onsubmit: async (event) => {
    event.preventDefault();
    const chosen = file.files[0];
    if (!chosen) return (status.textContent = "Choose a CV (PDF) first.");
    if (chosen.size > MAX_UPLOAD) return (status.textContent = "The CV is larger than 10 MB.");
    submit.disabled = true;
    status.textContent = "Uploading…";
    try {
      const answer = await rpc("matching_submit", { access_key: accessKey(), cv_name: chosen.name,
        pdf_base64: await fileAsBase64(chosen), save: save.checked });
      go({ request: answer.request_id }, true);
    } catch (error) {
      if (error instanceof AccessError) return show(problem(error));
      status.textContent = error.message;
      submit.disabled = false;
    }
  } },
    h("label", {}, "CV (PDF, at most 10 MB)"), file,
    h("label", { class: "pick plain" }, save, h("span", {}, h("b", {}, "Save this CV for daily checks"),
      h("span", { class: "muted small" }, "New cases for it will then appear here every morning."))),
    h("div", { class: "buttons" }, submit), status);
  show(h("h1", {}, "Upload a CV"),
    h("p", { class: "muted small intro" }, "The system reads the CV, searches CourtListener for complaints filed in the last year, " +
      "screens them, reads the relevant ones and matches them to the CV. It runs in the cloud and takes about 5 to 10 minutes; " +
      "you can close this page and come back."), form);
}

// Saved CVs: stop or resume daily checks (unsubscribe), or delete a CV with all its results.
async function cvsPage() {
  back.hidden = false;
  const cvs = await rpc("matching_cvs", { access_key: accessKey() });
  const status = h("p", { class: "muted small" });
  const act = (fn) => async () => {
    try {
      const message = await fn();
      if (message === null) return;
      cache.feed = {};  // The list changes when results are deleted.
      await cvsPage();
      app.querySelector(".status").textContent = message;
    } catch (error) {
      if (error instanceof AccessError) return show(problem(error));
      status.textContent = error.message;
    }
  };
  const toggle = (cv) => act(async () => {
    await rpc("matching_cv_active", { access_key: accessKey(), cv: cv.name, active: !cv.active });
    return `${cv.name}: daily checks ${cv.active ? "stopped" : "resumed"}.`;
  });
  const remove = (cv) => act(async () => {
    if (!window.confirm(`Delete ${cv.name} and all ${cv.runs} of its results? This cannot be undone.`)) return null;
    const done = await rpc("matching_cv_delete", { access_key: accessKey(), cv: cv.name });
    return `Deleted ${cv.name} and ${done.runs} result${done.runs === 1 ? "" : "s"}.`;
  });
  status.className = "muted small status";
  show(h("h1", {}, "Saved CVs"),
    h("p", { class: "muted small intro" }, (cache.me && cache.me.admin ? "All saved CVs (admin). " : "The CVs you saved. ") +
      "Saved CVs are checked for new cases every morning. Stopping a CV ends its daily checks " +
      "and keeps its past results; deleting it removes the CV and every result made from it."),
    status,
    cvs.length ? cvs.map((cv) => h("section", { class: "card" },
      h("div", { class: "expert" }, h("h3", {}, cv.name),
        h("span", { class: `tier ${cv.active ? "high" : "low"}` }, cv.active ? "Daily checks on" : "Stopped")),
      h("div", { class: "counts" }, meta(ownerNote(cv.owner), `saved ${day(cv.saved_at)}`, `${cv.runs} result${cv.runs === 1 ? "" : "s"}`,
        cv.last_run && `last ${day(cv.last_run)}`)),
      h("div", { class: "buttons" },
        h("button", { class: "btn light", type: "button", onclick: toggle(cv) }, cv.active ? "Stop daily checks" : "Resume daily checks"),
        h("button", { class: "btn danger", type: "button", onclick: remove(cv) }, "Delete"))))
      : h("p", { class: "muted" }, "No saved CV. Upload one and tick “Save this CV for daily checks”."),
    h("div", { class: "buttons" }, h("button", { class: "btn", type: "button", onclick: () => go({ view: "upload" }) }, "Upload a CV")));
}

function minutes(from, to) {
  return Math.max(0, Math.round((new Date(to) - new Date(from)) / 60000));
}

async function requestPage(id) {
  back.hidden = false;
  const state = await rpc("matching_request", { access_key: accessKey(), request: id });
  if (!state) return show(h("div", { class: "notice" }, h("h3", {}, "Upload not found")));
  if (state.state === "done" && state.run_id) return go({ run: state.run_id }, true);
  const waited = minutes(state.created_at, state.now);
  const lines = {
    queued: state.error ? state.error : waited >= 10 ? "Still waiting for the cloud to start. Try uploading again later."
      : "Waiting for the cloud to start (usually 1 to 2 minutes).",
    running: state.progress || "Running…",
    failed: state.error || "The run failed.",
  };
  show(h("h1", {}, state.cv_name),
    h("div", { class: "card" },
      h("div", { class: "top" }, h("span", { class: `tier ${state.state === "failed" ? "low" : "medium"}` },
        { queued: "Waiting", running: "Running", failed: "Failed" }[state.state] || state.state),
        h("span", { class: "score" }, `started ${waited} min ago`)),
      h("p", {}, lines[state.state] || ""),
      state.state === "failed" || (state.state === "queued" && state.error) ? null
        : h("p", { class: "muted small" }, "This page refreshes by itself.")));
  if (state.state === "queued" || state.state === "running") poll = setTimeout(route, 10000);
}

function summary(result) {
  return result.map((e) => ({
    name: e.profile.name, since: e.discovery && e.discovery.since, skipped: e.discovery && e.discovery.skipped,
    candidates: (e.discovery && e.discovery.candidates) || [],
    cases: e.results.map((x) => ({ name: x.case.name, url: x.case.url, court: x.case.court, filed: x.case.filed,
      case_type: x.profile && x.profile.case_type, tier: x.match.tier, score: x.score.total, summary: x.match.summary })),
  }));
}

async function runPage(id) {
  back.hidden = false;
  const data = await run(id);
  if (!data) return show(h("div", { class: "notice" }, h("h3", {}, "Results not found")));
  const runInfo = { run_id: data.run_id, cv: data.cv, started: data.started, daily: false };
  show(h("h1", {}, "Results"), h("p", { class: "muted small intro" }, `${data.cv} · ${day(data.started)} · complaints filed in the year before`),
    summary(data.result).map((expert) => expertCard(runInfo, expert)));
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

// Recipients: every lawyer named in the complaint, with the address code found for them, checked
// when they represent the party the draft is for; other addresses in the complaint unchecked;
// and any address the user adds. Sending happens in the user's own mail (Gmail or the phone's app),
// which adds their signature.
function recipientRow(address, label, note, checked) {
  const box = h("input", { type: "checkbox", value: address || "", checked: checked && Boolean(address), disabled: !address });
  return h("label", { class: "pick" }, box, h("span", {}, h("b", {}, label), address ? h("span", { class: "addr" }, address) : null,
    note ? h("span", { class: "muted small" }, note) : null));
}

function emailBox(match, profile) {
  const mail = match.email;
  if (!mail) return h("p", { class: "muted" }, "No draft: drafts are written for High and Medium matches only.");
  const counsel = profile.counsel || [];
  const others = profile.emails || [];
  const list = h("div", { class: "picks" },
    counsel.map((c) => recipientRow(c.email, c.name,
      meta(c.firm, c.party && `for ${c.party}`, c.email ? `p. ${c.email_page}` : "no address in the complaint"),
      !mail.to_party || c.party === mail.to_party)),
    others.length ? h("div", { class: "muted small sub" }, "Other addresses found in the complaint") : null,
    others.map((x) => recipientRow(x.email, x.email.split("@")[0], `p. ${x.page}`, false)));
  const added = h("input", { type: "email", placeholder: "Add an address (optional)", autocomplete: "email" });
  const subject = h("input", { value: mail.subject });
  const body = h("textarea", {}, mail.body);
  const status = h("span", { class: "muted small" });
  const recipients = () => {
    const chosen = [...list.querySelectorAll("input:checked")].map((b) => b.value);
    for (const a of added.value.split(/[\s,;]+/)) if (/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(a)) chosen.push(a);
    return [...new Set(chosen)];
  };
  const enc = encodeURIComponent;
  const gmail = () => window.open(`https://mail.google.com/mail/?view=cm&fs=1&to=${enc(recipients().join(","))}` +
    `&su=${enc(subject.value)}&body=${enc(body.value)}`, "_blank", "noopener");
  const mailApp = () => { location.href = `mailto:${recipients().map(enc).join(",")}?subject=${enc(subject.value)}&body=${enc(body.value)}`; };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(`To: ${recipients().join(", ")}\nSubject: ${subject.value}\n\n${body.value}`);
      status.textContent = "Copied.";
    } catch (e) { status.textContent = "Copy failed; select the text instead."; }
  };
  return h("div", {},
    h("label", {}, `To${mail.to_party ? ` (draft written for ${mail.to_party})` : ""}`), list, added,
    h("p", { class: "muted small" }, "Addresses come from the complaint's signature block. Check them before sending; nothing is sent automatically."),
    h("label", {}, "Subject"), subject, h("label", {}, "Message"), body,
    h("div", { class: "buttons" },
      h("button", { class: "btn", type: "button", onclick: gmail }, "Open in Gmail"),
      h("button", { class: "btn light", type: "button", onclick: mailApp }, "Open in Mail app"),
      h("button", { class: "btn light", type: "button", onclick: copy }, "Copy"), status),
    h("p", { class: "muted small" }, "Gmail opens signed in as you, with your signature added."));
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
      h("ul", {}, profile.counsel.map((x) => h("li", {}, h("b", {}, x.name), x.firm ? `, ${x.firm}` : "",
        x.email ? h("span", {}, " · ", h("a", { href: `mailto:${x.email}` }, x.email)) : null,
        h("span", { class: "muted" }, ` for ${x.party} (p. ${x.page})`))))) : null,
    h("div", { class: "section" }, h("h2", {}, "Email draft"), emailBox(match, profile || {})),
    h("p", { class: "muted small section" }, "Summaries and reasons are written by AI. Every quote shown was checked against the CV or the complaint."));
}

// ---- routing ----

async function route() {
  if (!CONFIG.url || !CONFIG.key) return show(h("div", { class: "notice" }, h("h3", {}, "Not configured"), h("p", { class: "muted" }, "config.js is missing.")));
  if (!accessKey()) return show(problem(new AccessError()));
  const p = params();
  clearTimeout(poll);
  try {
    await me();
    if (p.get("run") && p.get("case") !== null) await casePage(p.get("run"), Number(p.get("case")));
    else if (p.get("run")) await runPage(p.get("run"));
    else if (p.get("request")) await requestPage(p.get("request"));
    else if (p.get("view") === "upload") uploadPage();
    else if (p.get("view") === "cvs") await cvsPage();
    else await listPage();
  } catch (error) {
    show(problem(error));
  }
}

back.addEventListener("click", () => (history.length > 1 ? history.back() : go({})));
window.addEventListener("hashchange", route);
route();
