"use strict";

const MAX_BYTES = 10 * 1024 * 1024;
const STEP_DELAY = 320;                       // ms of "checking..." per step (cosmetic)
const DPP_SEMANTIC_ID = "https://admin-shell.io/idta/cds/dppMetadata/1";
const KNOWN = [                                // display order + requirement
  ["digitalProductPassportId", true], ["uniqueProductIdentifier", true],
  ["granularity", true], ["dppSchemaVersion", true], ["dppStatus", true],
  ["lastUpdate", true], ["economicOperatorId", true],
  ["facilityId", false], ["contentSpecificationIds", false],
];

/* Steps in order of importance. `match` decides which backend results belong to a step. */
const STEPS = [
  { key: "file", title: "Valid JSON file", desc: "Readable, valid JSON, root is an AAS environment object" },
  { key: "aas", title: "AAS structure", desc: "Root collections, shells, submodel references, DPP submodel found" },
  { key: "mandatory", title: "Mandatory fields", desc: "All 7 required DPP attributes present and non-empty" },
  { key: "attrs", title: "Attributes & types", desc: "Element types, value types, structure, optional attributes", table: true },
  { key: "datetime", title: "Date, time & timezone", desc: "lastUpdate is a real date-time with a valid timezone" },
  { key: "semantic", title: "Semantic IDs & allowed values", desc: "semanticIds and granularity match IDTA 02099-1" },
  { key: "identifiers", title: "Identifier formats (EN 18219)", desc: "Product, operator and facility identifier rules" },
];

function stepOf(r) {
  const id = r.check_id;
  if (id.startsWith("NOTRUN")) return null;
  if (id.startsWith("JSON-") || id === "ROOT-OBJECT" || id === "INTERNAL") return "file";
  if (id.startsWith("ROOT-") || id.startsWith("AAS-") || id.startsWith("DPP-SUBMODEL")) return "aas";
  if (id.startsWith("MAND-")) return "mandatory";
  if (id.startsWith("TYPE-") || id.startsWith("ELEM-") || id.startsWith("OPT-") || id === "DPP-IDSHORT") return "attrs";
  if (id.startsWith("DT-")) return "datetime";
  if (id.startsWith("SEM-") || id.startsWith("VAL-") || id === "DPP-SEMANTIC") return "semantic";
  if (id.startsWith("ID-")) return "identifiers";
  return "aas";
}

/* ---------- tiny DOM helper ---------- */
const h = (tag, props = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;            // only used with static icon strings
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
};
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ICONS = {
  pass: '<svg viewBox="0 0 24 24"><path d="M6 12.5l4 4 8-9"/></svg>',
  fail: '<svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7L7 17"/></svg>',
  warn: '<svg viewBox="0 0 24 24"><path d="M12 6v7M12 17.2v.3"/></svg>',
  skip: '<svg viewBox="0 0 24 24"><path d="M7 12h10"/></svg>',
  running: '<svg viewBox="0 0 24 24" class="spin"><circle cx="12" cy="12" r="8"/></svg>',
};
const icon = (status) => h("span", { class: "icon " + status, html: ICONS[status] });
const CHEVRON = '<svg class="chev" viewBox="0 0 16 16"><path d="M3 6l5 5 5-5"/></svg>';

/* ---------- status helpers ---------- */
function statusOf(results) {
  if (!results.length) return "skip";
  if (results.some((r) => r.status === "FAIL")) return "fail";
  if (results.some((r) => r.status === "WARN")) return "warn";
  if (results.every((r) => r.status === "SKIP")) return "skip";
  return "pass";
}
const lower = (s) => s.toLowerCase();

/* ---------- key/value extraction (display only; validation is done by the backend) ---------- */
function refValues(ref) {
  return ref && Array.isArray(ref.keys) ? ref.keys.map((k) => k && k.value).filter((v) => typeof v === "string") : [];
}
function extractDpp(data) {
  try {
    const sms = (data.submodels || []).filter((s) => s && typeof s === "object");
    const sm = sms.find((s) => s.idShort === "DppMetadata" || refValues(s.semanticId).includes(DPP_SEMANTIC_ID));
    if (!sm || !Array.isArray(sm.submodelElements)) return null;
    const map = {};
    for (const e of sm.submodelElements) {
      if (e && typeof e === "object" && typeof e.idShort === "string" && !(e.idShort in map)) map[e.idShort] = e;
    }
    return map;
  } catch { return null; }
}
function displayValue(el) {
  const v = el.value;
  if (v === undefined || v === null) return null;
  if (Array.isArray(v)) {
    const vals = v.map((x) => (x && typeof x === "object" && "value" in x ? x.value : x));
    return vals.length ? vals.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join("\n") : null;
  }
  return typeof v === "string" ? (v.trim() === "" ? null : v) : JSON.stringify(v);
}

/* ---------- rendering ---------- */
function checkRow(r) {
  const meta = [];
  if (r.location) meta.push(h("span", {}, "Where: ", h("code", {}, r.location)));
  if (r.expected != null) meta.push(h("span", {}, "Expected: ", h("code", {}, r.expected)));
  if (r.actual != null) meta.push(h("span", {}, "Actual: ", h("code", {}, r.actual)));
  return h("div", { class: "row" }, icon(lower(r.status)),
    h("div", {}, h("b", {}, r.name), h("div", {}, r.message), h("div", { class: "meta" }, meta)));
}
function checksList(rs) { return h("div", { class: "rows" }, rs.map(checkRow)); }

function attributeTable(rs, dpp) {
  const rows = KNOWN.map(([id, mandatory]) => {
    const mine = rs.concat(window.__allResults || []).filter((r) =>
      /^(TYPE|MAND|OPT)-/.test(r.check_id) && r.check_id.split("-").pop() === id);
    const el = dpp && dpp[id];
    let st = statusOf(mine.filter((r) => r.status !== "SKIP"));
    if (!el && !mandatory && st === "pass") st = "skip";
    const val = el ? displayValue(el) : null;
    const kind = el ? [el.modelType, el.valueType].filter(Boolean).join(" · ") : mandatory ? "required" : "optional";
    return h("tr", {}, h("td", {}, icon(st)), h("td", { class: "k" }, id),
      h("td", { class: "v" }, val === null ? h("span", { class: "none" }, el ? "no value" : "not present")
        : h("code", {}, val.length > 160 ? val.slice(0, 157) + "…" : val)),
      h("td", { class: "t" }, kind));
  });
  const extras = dpp ? Object.keys(dpp).filter((k) => !KNOWN.some(([id]) => id === k)) : [];
  return h("div", {}, h("table", { class: "kv" }, h("tbody", {}, rows)),
    extras.length ? h("div", { class: "meta" }, `${extras.length} additional element(s) not part of IDTA 02099-1 were ignored: ${extras.join(", ")}`) : null);
}

function stepDetail(step, rs, dpp) {
  const sources = [...new Set(rs.map((r) => r.source).filter(Boolean))];
  const body = [];
  if (sources.length) body.push(h("div", { class: "against" }, "Checked against: ", sources.join(" · ")));
  if (step.table) {
    body.push(attributeTable(rs, dpp));
    const btn = h("button", { class: "more", onclick: (e) => e.currentTarget.classList.toggle("on") }, "Show all checks");
    body.push(btn, checksList(rs));
  } else {
    body.push(rs.length ? checksList(rs) : h("div", { class: "against" }, "Not run (an earlier step could not be completed)."));
  }
  return h("div", { class: "detail" }, body);
}

function buildStep(step) {
  const li = h("li", { class: "step", "data-status": "running" });
  const head = h("button", { class: "step-head", "aria-expanded": "false" },
    icon("running"), h("span", { class: "text" }, h("span", { class: "title" }, step.title), h("span", { class: "desc" }, step.desc)),
    h("span", { class: "count" }), h("span", { html: CHEVRON }));
  head.addEventListener("click", () => {
    if (li.dataset.status === "running") return;
    const open = li.classList.toggle("open");
    head.setAttribute("aria-expanded", String(open));
  });
  const body = h("div", { class: "step-body" }, h("div", { class: "inner" }));
  li.append(head, body);
  return li;
}

function finishStep(li, step, rs, dpp) {
  const st = statusOf(rs);
  li.dataset.status = st;
  const old = li.querySelector(".step-head .icon");
  old.replaceWith(icon(st));
  const counted = rs.filter((r) => r.status !== "SKIP");
  li.querySelector(".count").textContent = counted.length
    ? `${counted.filter((r) => r.status === "PASS").length}/${counted.length}` : "not run";
  li.querySelector(".inner").replaceChildren(stepDetail(step, rs, dpp));
}

function layerChips(results) {
  const layers = [["1 JSON", "JSON"], ["2 AAS structure", "AAS structure"],
                  ["3 IDTA 02099-1", "IDTA 02099-1"], ["4 EN 18223 / EN 18219", "EN 18223 / 18219"]];
  return h("div", { class: "chips" }, layers.map(([key, label]) => {
    const rs = results.filter((r) => r.layer === key);
    const st = statusOf(rs.filter((r) => !r.check_id.startsWith("NOTRUN")));
    const text = st === "pass" || st === "warn" ? "compliant" : st === "fail" ? "failed" : "not evaluated";
    return h("span", { class: "chip " + (st === "warn" ? "pass" : st) }, h("i"), `${label}: ${text}`);
  }));
}

function renderSummary(report) {
  const ok = report.verdict === "COMPLIANT";
  const c = (s) => report.results.filter((r) => r.status === s).length;
  const parts = [`${c("PASS")} passed`, `${c("FAIL")} failed`];
  if (c("WARN")) parts.push(`${c("WARN")} warning${c("WARN") > 1 ? "s" : ""}`);
  if (c("SKIP")) parts.push(`${c("SKIP")} not evaluated`);
  const note = !report.validation_complete ? "Validation incomplete: " + report.incomplete_reason
    : ok ? "Compliant with respect to the implemented checks. This is not a certification; skipped checks were not evaluated."
         : null;
  return h("div", {},
    h("div", { class: "verdict" }, icon(ok ? "pass" : "fail"),
      h("div", {}, h("h2", {}, ok ? "Compliant" : "Not compliant"), h("p", {}, `${report.path} · ${parts.join(" · ")}`))),
    layerChips(report.results),
    note ? h("p", { class: "note" + (ok ? "" : " bad") }, note) : null);
}

/* ---------- flow ---------- */
const drop = $("drop"), fileInput = $("file"), results = $("results"),
      summary = $("summary"), stepsEl = $("steps"), resetBtn = $("reset");
let runId = 0;

async function handleFile(file) {
  const run = ++runId;
  drop.classList.add("hide"); results.hidden = false; resetBtn.hidden = true;
  stepsEl.replaceChildren();
  summary.replaceChildren(h("div", { class: "verdict" }, icon("running"),
    h("div", {}, h("h2", {}, "Checking…"), h("p", {}, file.name))));

  if (file.size > MAX_BYTES) return showError("File is larger than 10 MB.");

  let dpp = null;
  try { dpp = extractDpp(JSON.parse(await file.text())); } catch { /* backend reports the problem */ }

  let report;
  try {
    const res = await fetch("/api/check", {
      method: "POST",
      headers: { "X-Filename": encodeURIComponent(file.name), "Content-Type": "application/octet-stream" },
      body: file,
    });
    report = await res.json();
    if (!res.ok) throw new Error(report.error || res.statusText);
  } catch (e) {
    return showError("Could not reach the checker (" + e.message + "). Is server.py running?");
  }
  window.__allResults = report.results;

  const groups = Object.fromEntries(STEPS.map((s) => [s.key, []]));
  for (const r of report.results) { const k = stepOf(r); if (k) groups[k].push(r); }

  const lis = [];
  for (const step of STEPS) {
    if (run !== runId) return;
    const li = buildStep(step); lis.push(li); stepsEl.append(li);
    await sleep(STEP_DELAY);
    finishStep(li, step, groups[step.key], dpp);
  }
  summary.replaceChildren(renderSummary(report));
  const firstBad = lis.find((li) => ["fail", "warn"].includes(li.dataset.status));
  if (firstBad) firstBad.querySelector(".step-head").click();
  resetBtn.hidden = false;
}

function showError(msg) {
  summary.replaceChildren(h("div", { class: "verdict" }, icon("fail"), h("div", {}, h("h2", {}, "Something went wrong"), h("p", {}, msg))));
  resetBtn.hidden = false;
}
function reset() {
  runId++; results.hidden = true; drop.classList.remove("hide"); fileInput.value = "";
  stepsEl.replaceChildren(); summary.replaceChildren(); delete window.__allResults;
}

drop.addEventListener("click", () => fileInput.click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
fileInput.addEventListener("change", () => fileInput.files[0] && handleFile(fileInput.files[0]));
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) handleFile(f); });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());
resetBtn.addEventListener("click", reset);
