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
const guideLink = document.getElementById("guide");
const cvsLink = document.getElementById("my-cvs");
// The daily check starts at 11:30 UTC (Supabase pg_cron); a backup run at 17:00 UTC covers a missed one.
const DAILY_UTC = [11, 30];
const BACKUP_UTC = [17, 0];
const cache = { feed: {}, runs: {}, me: null, cvs: null };
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

async function savedCvs() {
  if (!cache.cvs) cache.cvs = await rpc("matching_cvs", { access_key: accessKey() });
  return cache.cvs;
}

// Top bar: "My CVs" appears once there is a saved CV; the current page's link is marked.
function nav(view) {
  cvsLink.hidden = !(cache.me.admin || (cache.cvs && cache.cvs.length));
  for (const [link, name] of [[cvsLink, "cvs"], [guideLink, "guide"]]) {
    if (view === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function textLink(label, view) {
  return h("a", { class: "text-link", href: "#", onclick: (event) => { event.preventDefault(); go(view); } }, label);
}

function uploadButton() {
  return h("button", { class: "btn", type: "button", onclick: () => go({ view: "upload" }) }, "Upload a CV");
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

// What the search agent found for a complaint missing on CourtListener.
function webNote(found) {
  if (!found) return null;
  if (!found.verified) return h("div", { class: "muted" }, "Searched the web: no copy could be verified as this case.");
  return h("div", {}, "Found on the web: ", h("a", { href: found.url, target: "_blank", rel: "noopener" }, found.source),
    found.kind === "complaint" ? " (the complaint; read and matched above)" : " (a web page, not the complaint; read and matched above)");
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
        webNote(c.elsewhere))))) : null);
}

async function listPage() {
  back.hidden = true;
  const p = params();
  const days = WINDOWS.includes(Number(p.get("days"))) ? Number(p.get("days")) : 14;
  const who = p.get("cv");
  const [runs, cvs] = await Promise.all([feed(days), savedCvs()]);
  if (!runs.length && !cvs.length) {
    return show(h("section", { class: "notice welcome" }, h("h2", {}, "Welcome"),
      h("p", {}, "Upload an expert's CV. Each morning the system finds newly filed court cases that match it and lists them here."),
      h("p", { class: "muted small" }, "You only see the CVs you upload."),
      h("div", { class: "buttons" }, uploadButton(), textLink("How it works →", { view: "guide" }))));
  }
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
  const checked = cvs.some((cv) => cv.active);
  show(
    h("div", { class: "headline" }, h("h1", {}, "New cases by day"), h("div", { class: "buttons tight" }, uploadButton())),
    h("p", { class: "muted small intro" }, `Saved CVs are checked every morning at ${atLocal(DAILY_UTC)}. Each case is shown once.`),
    names.length > 1 ? h("div", { class: "controls" },
      h("button", { class: "chip", type: "button", "aria-pressed": String(!who), onclick: () => go({ days }) }, "All experts"),
      names.map((n) => h("button", { class: "chip", type: "button", "aria-pressed": String(who === n), onclick: () => go({ days, cv: n }) }, n))) : null,
    h("div", { class: "controls" }, h("span", { class: "muted small" }, "Show the last"),
      WINDOWS.map((d) => h("button", { class: "chip", type: "button", "aria-pressed": String(d === days), onclick: () => go({ days: d, cv: who }) }, `${d} days`))),
    byDay.size ? [...byDay].map(([label, cards]) => h("div", {}, h("h2", { class: "day" }, label), cards))
      : h("div", { class: "notice" }, h("h3", {}, "No new cases yet"),
        h("p", { class: "muted" }, checked
          ? `After each morning's check, new cases usually appear here by ${atLocal(DAILY_UTC, 30)}. Some days have none.`
          : "None of your CVs is saved for daily checks. Resume one in My CVs, or upload a CV and keep \u201cSave this CV for daily checks\u201d ticked.")),
    uploads.length ? h("div", { class: "section" }, h("h1", { class: "sub" }, "From uploaded CVs"),
      h("p", { class: "muted small" }, "Complaints filed in the year before the upload."), uploads) : null);
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
      cache.feed = {};
      cache.cvs = null;
      go({ request: answer.request_id }, true);
    } catch (error) {
      if (error instanceof AccessError) return show(problem(error));
      status.textContent = error.message;
      submit.disabled = false;
    }
  } },
    h("label", {}, "CV (PDF, at most 10 MB)"), file,
    h("label", { class: "pick plain" }, save, h("span", {}, h("b", {}, "Save this CV for daily checks"),
      h("span", { class: "muted small" }, `New cases for it will then appear on the home page every day, usually by ${atLocal(DAILY_UTC, 30)}. ` +
        "There is no email yet: open this site to see them."))),
    h("div", { class: "buttons" }, submit), status);
  show(h("h1", {}, "Upload a CV"),
    h("p", { class: "muted small intro" }, "The system reads the CV, searches CourtListener for complaints filed in the last year, " +
      "screens them, reads the relevant ones and matches them to the CV. It runs in the cloud and takes about 5 to 10 minutes; " +
      "you can close this page and come back."), form,
    h("p", { class: "small" }, textLink("How daily checks and results work", { view: "guide" })));
}

// My CVs: stop or resume daily checks (unsubscribe), or delete a CV with all its results.
async function cvsPage() {
  back.hidden = false;
  const cvs = cache.cvs = await rpc("matching_cvs", { access_key: accessKey() });
  const status = h("p", { class: "muted small" });
  const act = (fn) => async () => {
    try {
      const message = await fn();
      if (message === null) return;
      cache.feed = {};  // The list changes when results are deleted.
      await cvsPage();
      nav("cvs");
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
  show(h("div", { class: "headline" }, h("h1", {}, "My CVs"), h("div", { class: "buttons tight" }, uploadButton())),
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
      : h("div", { class: "notice" }, h("h3", {}, "You have no saved CVs yet"),
        h("p", { class: "muted" }, "Upload one and keep “Save this CV for daily checks” ticked.")));
}

// Local time (with the time zone) of a daily UTC time, plus `later` minutes.
function atLocal([hour, minute], later = 0) {
  const now = new Date();
  const at = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hour, minute + later));
  return at.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZoneName: "short" });
}

// ---- guide ----

function guideSection(title, ...body) {
  return h("section", { class: "card guide" }, h("h3", {}, title), body);
}

function guidePage() {
  back.hidden = false;
  const daily = atLocal(DAILY_UTC);
  const ready = atLocal(DAILY_UTC, 30);
  const backup = atLocal(BACKUP_UTC);
  const link = (label, view) => h("a", { href: "#", onclick: (event) => { event.preventDefault(); go(view); } }, label);
  show(h("h1", {}, "Guide"),
    h("p", { class: "muted small intro" }, "How Expert Match finds new court cases for an expert, when you get them, and how to use them."),

    guideSection("What it does",
      h("p", {}, "You give it an expert's CV. It reads the CV, searches CourtListener (federal court filings) for new complaints, " +
        "screens out the ones that do not fit, reads the relevant ones in full, and explains which parts of the CV match " +
        "which issues in each case. For good matches it drafts an email to the lawyers on the case.")),

    guideSection("Getting started",
      h("ol", {},
        h("li", {}, "Open the personal link you were sent. It is your access: whoever has it sees your CVs, so do not forward it."),
        h("li", {}, "Add it to your phone's home screen. iPhone: in Safari tap Share, then “Add to Home Screen”. " +
          "Android: in Chrome tap ⋮, then “Add to Home screen”. The icon then opens straight to your cases."),
        h("li", {}, "Upload a CV (", link("Upload a CV", { view: "upload" }), ").")),
      h("p", { class: "muted small" }, "The name in the top bar shows whose link this is. “My CVs” appears there once you have saved a CV.")),

    guideSection("Upload a CV",
      h("ol", {},
        h("li", {}, "Choose the CV as a PDF (at most 10 MB)."),
        h("li", {}, "Keep “Save this CV for daily checks” ticked if you want new cases for this expert every day."),
        h("li", {}, "Tap “Find cases”. The first search covers complaints filed in the last year and takes about 3 to 10 minutes. " +
          "The page shows each step; you can close it and come back.")),
      h("p", {}, "The results appear on the home page under “From uploaded CVs”.")),

    guideSection("When and how you get new cases",
      h("ul", {},
        h("li", {}, h("b", {}, "When: "), `every day at ${daily} the system checks each saved CV against complaints filed in the previous two days. ` +
          `Results are usually ready by ${ready}. If that run is missed, a backup run at ${backup} catches up.`),
        h("li", {}, h("b", {}, "Where: "), "open this website (or its home-screen icon). New cases are on the home page, under the day they were found. " +
          "Pull down or reopen the page to refresh."),
        h("li", {}, h("b", {}, "Notifications: "), "none yet. There is no email or phone alert; check the site each morning."),
        h("li", {}, h("b", {}, "Each case once: "), "a case already shown for a CV is not shown again."),
        h("li", {}, h("b", {}, "Quiet days: "), "few complaints are filed on weekends and holidays, so some days show nothing new."))),

    guideSection("Reading the results",
      h("ul", {},
        h("li", {}, h("span", { class: "tier high" }, "High"), " the CV directly matches at least one technical issue in the case."),
        h("li", {}, h("span", { class: "tier medium" }, "Medium"), " related experience that may help."),
        h("li", {}, h("span", { class: "tier low" }, "Low"), " a weak link; “Insufficient information” means the complaint could not be read or judged."),
        h("li", {}, h("b", {}, "Score: "), "orders cases within a level, from the evidence found, how technical the case is and how new it is."),
        h("li", {}, h("b", {}, "Match reasons: "), "why the CV fits the case, point by point (see the next section)."),
        h("li", {}, h("b", {}, "Complaint not on CourtListener: "), "about half of new complaints have no PDF there yet. Those are judged from the " +
          "one-line docket text. For relevant ones a search agent looks for the complaint on the web (agency, court and law firm sites). " +
          "A copy is used only if code confirms it names both parties and carries the docket number or filing year; it is then read in full " +
          "and labelled with its source. A news or press page alone is labelled as not the complaint and is never rated above Medium. " +
          "If nothing is confirmed, the case is listed with the cost of buying the complaint from PACER (at most $3). Nothing is bought automatically."))),

    guideSection("Match reasons: what E1, I1 and X1 mean",
      h("p", {}, "Each match reason links one thing from the CV to one thing in the case, with a quote from each document and the page it is on."),
      h("ul", {},
        h("li", {}, h("b", {}, "E1, E2, …"), " an item from the expert's CV: a job, project, patent, publication, past testimony, degree, " +
          "certification, skill or award. E1 is the first one found, E2 the second, and so on."),
        h("li", {}, h("b", {}, "I1, I2, …"), " an issue in dispute in the case, as stated in the complaint. I1 is the first issue."),
        h("li", {}, h("b", {}, "X1, X2, …"), " an area of expertise the case needs, for example machine learning or medical devices."),
        h("li", {}, h("b", {}, "direct"), " the CV item is about the same thing as the issue; ", h("b", {}, "related"), " it is close, but not the same.")),
      h("p", { class: "muted small" }, "Example (made up):"),
      h("div", { class: "reason" },
        h("div", {}, h("span", { class: "strength" }, "direct"), " · Both concern image-recognition models for spotting defects."),
        h("div", { class: "pair" },
          side("CV E2 · project · p. 1", "Led development of a deep-learning system that detects defects in factory images.",
            "Built a defect-detection model."),
          side("Complaint I1 · p. 4", "Defendant's inspection software uses a neural network to identify defective parts…",
            "Whether the accused software copies the plaintiff's defect-detection method."))),
      h("p", { class: "muted small" }, "Read it as: CV item E2 matches case issue I1, directly. Code checks that every quote really appears " +
        "in the CV or the complaint on that page; the one-line summaries are written by AI and can be wrong.")),

    guideSection("Contacting the lawyers",
      h("ol", {},
        h("li", {}, "Open a High or Medium case and scroll to “Email draft”."),
        h("li", {}, "Tick the lawyers to write to. Their addresses come from the complaint's signature block; you can add others."),
        h("li", {}, "Edit the subject and message."),
        h("li", {}, "Tap “Open in Gmail” (your Gmail adds your signature), “Open in Mail app”, or “Copy”. " +
          "Nothing is sent until you send it yourself."))),

    guideSection("Managing your CVs",
      h("p", {}, link("My CVs", { view: "cvs" }), " (in the top bar) lists the CVs you saved."),
      h("ul", {},
        h("li", {}, h("b", {}, "Stop daily checks: "), "no more daily searches for that CV; its past results stay. You can resume later."),
        h("li", {}, h("b", {}, "Delete: "), "removes the CV and every result made from it. This cannot be undone.")),
      h("p", { class: "muted small" }, "You only see and manage the CVs you uploaded.")),

    guideSection("Limits",
      h("ul", { class: "small" },
        h("li", {}, "PDF CVs of at most 10 MB; at most 10 uploads a day."),
        h("li", {}, "Daily checks look for newly filed complaints only; they do not follow later filings in older cases."),
        h("li", {}, "Only federal cases on CourtListener are searched."),
        h("li", {}, "AI summaries and judgments can be wrong; check the complaint before contacting anyone."))));
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
      c.document_url ? h("a", { href: c.document_url, target: "_blank", rel: "noopener" },
        c.from_web === "web page" ? `Source page (${c.web_source})` : `${doc} PDF${c.total_pages ? ` (${c.total_pages} pages)` : ""}`) : null),
    c.from_web ? h("p", { class: "notice small" }, c.from_web === "complaint"
      ? `CourtListener has no copy of this complaint. The search agent found it on ${c.web_source} and checked it is this case (${c.web_check}).`
      : `CourtListener has no copy of this complaint. This reading is from a web page on ${c.web_source} (checked: ${c.web_check}), ` +
        "not the complaint itself, so the level is at most Medium.") : null,
    h("div", { class: "section" }, h("h2", {}, "Why it fits"), h("p", {}, match.summary || "")),
    profile ? h("div", { class: "section" }, h("h2", {}, "The case"),
      h("p", {}, profile.summary),
      h("div", { class: "tags" }, [profile.case_type, ...profile.domains].filter(Boolean).map((t) => h("span", { class: "tag" }, t))),
      h("p", { class: "small" }, h("b", {}, "Plaintiffs: "), profile.plaintiffs.join("; ")),
      h("p", { class: "small" }, h("b", {}, "Defendants: "), profile.defendants.join("; "))) : null,
    h("div", { class: "section" }, h("h2", {}, `Match reasons (CV ↔ ${doc.toLowerCase()})`),
      h("p", { class: "muted small" }, "E = an item in the CV · I = an issue in the case · X = expertise the case needs. ",
        textLink("More in the guide", { view: "guide" })),
      reasons(match, doc)),
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
    await savedCvs();
    nav(p.get("view"));
    if (p.get("run") && p.get("case") !== null) await casePage(p.get("run"), Number(p.get("case")));
    else if (p.get("run")) await runPage(p.get("run"));
    else if (p.get("request")) await requestPage(p.get("request"));
    else if (p.get("view") === "upload") uploadPage();
    else if (p.get("view") === "cvs") await cvsPage();
    else if (p.get("view") === "guide") guidePage();
    else await listPage();
  } catch (error) {
    show(problem(error));
  }
}

back.addEventListener("click", () => (history.length > 1 ? history.back() : go({})));
guideLink.addEventListener("click", (event) => { event.preventDefault(); go({ view: "guide" }); });
cvsLink.addEventListener("click", (event) => { event.preventDefault(); go({ view: "cvs" }); });
window.addEventListener("hashchange", route);
route();
