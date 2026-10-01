"""Renders the inference engine's results as one self-contained HTML file (no external scripts, fonts or
requests).

The engine decides the results (InvariantInferenceEngine.dashboard_payload); this module only
renders them. Rules that state the same thing after different actions are grouped into one row per
statement (the engine's `family` key) and expand to the per-action rules. A checkbox switches to one
row per rule. Threshold, minimum observations and the amber band width come from the payload, i.e.
from inference_config.json.
"""

from __future__ import annotations

import json


def render_dashboard(payload: dict) -> str:
    # escape "</" so the data cannot end the script block early
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", data)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invariant Inference Dashboard</title>
<style>
:root{--bg:#f6f6f3;--panel:#fff;--text:#1d1d1b;--muted:#6b6a64;--line:#e2e1db;--accent:#2d5bd7;
--ok:#256b42;--okbg:#dff0e5;--warn:#8a5d00;--warnbg:#fbefcf;--bad:#a8231b;--badbg:#f8dedb;
--con:#6b3fb3;--conbg:#ebe2f8;--none:#8d8c86;--nonebg:#ecebe6}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--panel:#1e1e1c;--text:#ecebe6;--muted:#a19f97;--line:#33322e;
--accent:#8ab0ff;--ok:#86d6a2;--okbg:#1b3224;--warn:#eac46b;--warnbg:#382e17;--bad:#f2928a;--badbg:#3b1e1b;
--con:#c3a6f5;--conbg:#2c2240;--none:#8d8c86;--nonebg:#292926}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header,main{max-width:1400px;margin:0 auto;padding:0 28px}
header{padding-top:24px}
h1{font-size:21px;margin:0 0 4px}
h2{font-size:15px;margin:28px 0 10px}
.meta{color:var(--muted);font-size:12.5px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px}
.chip b{font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:18px}
.card{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:10px;
padding:12px 14px;text-align:left;color:inherit;font:inherit;cursor:pointer}
.card:hover{border-color:var(--c)}
.card.active{outline:2px solid var(--accent);outline-offset:1px}
.card .n{font-size:26px;font-weight:650;line-height:1.1}
.card .l{font-weight:600;margin-top:2px}
.card .d{color:var(--muted);font-size:12px}
.g-holds{--c:var(--ok)}.g-investigate{--c:var(--warn)}.g-contradictory{--c:var(--con)}.g-below{--c:var(--bad)}.g-insufficient{--c:var(--none)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px}
.cats{padding:6px 0}
.cat{display:grid;grid-template-columns:190px 1fr 48px;gap:12px;align-items:center;padding:5px 14px;cursor:pointer}
.cat:hover{background:var(--bg)}
.cat.sel{background:var(--bg);font-weight:600}
.bar{display:flex;height:14px;border-radius:4px;overflow:hidden;background:var(--nonebg)}
.bar span{display:block;height:100%}
.b-holds{background:var(--ok)}.b-investigate{background:var(--warn)}.b-contradictory{background:var(--con)}.b-below{background:var(--bad)}.b-insufficient{background:var(--none);opacity:.45}
.num{color:var(--muted);font-size:12px;text-align:right}
.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 10px}
.filters input[type=search],.filters select{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
.filters input[type=search]{min-width:260px;flex:1}
.filters button{font:inherit;color:var(--accent);background:none;border:0;cursor:pointer}
.filters label{display:flex;align-items:center;gap:6px;cursor:pointer}
.legend{display:flex;flex-wrap:wrap;gap:14px;color:var(--muted);font-size:12px;margin-bottom:10px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-2px;margin-right:5px}
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{font-size:12px;font-weight:600;color:var(--muted);text-align:left;padding:10px;border-bottom:1px solid var(--line);white-space:nowrap}
th.ds{text-align:center}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tr.row,tr.mrow{cursor:pointer}
tr.row:hover>td,tr.mrow:hover>td{background:var(--bg)}
td.stmt{font-weight:500;min-width:300px}
td.tag{white-space:nowrap;font-size:12.5px;color:var(--muted)}
td.tag small{display:block;font-size:11px}
td.cell{text-align:center;font-weight:600;white-space:nowrap;min-width:92px}
td.cell small{display:block;font-weight:400;font-size:11px;opacity:.85}
.s-ok{background:var(--okbg);color:var(--ok)}
.s-warn{background:var(--warnbg);color:var(--warn)}
.s-bad{background:var(--badbg);color:var(--bad)}
.s-none{background:var(--nonebg);color:var(--none)}
.s-thin{background:repeating-linear-gradient(45deg,var(--nonebg) 0 5px,transparent 5px 10px);color:var(--muted)}
td.chev{color:var(--muted);width:28px;text-align:center}
tr.detail>td,tr.mdetail>td{background:var(--bg);padding:14px 18px}
.badge{display:inline-block;border:1px solid var(--c);color:var(--c);border-radius:999px;padding:0 8px;font-size:11px}
.detail h4{margin:0 0 6px;font-size:12.5px;color:var(--muted);font-weight:600}
.detail ul{margin:0 0 12px;padding-left:18px}
.detail li{margin:2px 0}
code{font:12px ui-monospace,SFMono-Regular,Consolas,monospace}
pre{margin:0;padding:12px;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow-x:auto;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}
.rulecard{width:100%;border-collapse:collapse;margin:0 0 14px;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.rulecard th{width:130px;vertical-align:top;color:var(--muted);border-bottom:1px solid var(--line)}
.rulecard td{border-bottom:1px solid var(--line);font-size:13px}
.ex{color:var(--muted);font-size:12.5px}
.copy{float:right;font:inherit;font-size:12px;color:var(--accent);background:none;border:0;cursor:pointer}
.empty{padding:28px;text-align:center;color:var(--muted)}
.count{color:var(--muted);font-size:12.5px;margin-left:auto}
</style>
</head>
<body>
<header>
  <h1>Invariant Inference Dashboard</h1>
  <div class="meta" id="meta"></div>
  <div class="chips" id="chips"></div>
  <div class="cards" id="cards"></div>
</header>
<main>
  <h2>By action category</h2>
  <div class="panel cats" id="cats"></div>

  <h2>Candidates</h2>
  <div class="filters">
    <input id="q" type="search" placeholder="Search statements, actions, templates or a rule ID (INF-...)">
    <select id="cat"></select>
    <select id="tpl"></select>
    <label><input id="grp" type="checkbox" checked> Group by statement</label>
    <button id="reset">Reset filters</button>
    <span class="count" id="count"></span>
  </div>
  <div class="legend" id="legend"></div>
  <div class="panel tablewrap">
    <table>
      <thead id="thead"></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</main>
<script>
const D = __DATA__;
const T = D.threshold, NEAR = T - D.near_margin;
const DS = D.datasets.map(d => d.name);
const pct = x => Math.round(x * 100) + "%";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const GROUPS = [
  {id:"holds",         short:"hold",          label:"Invariants that hold", desc:`≥ ${pct(T)} in every dataset`},
  {id:"investigate",   short:"investigate",   label:"Worth investigating",  desc:"hold in some datasets, not in others"},
  {id:"contradictory", short:"contradictory", label:"Contradictory",        desc:"datasets clearly point opposite ways"},
  {id:"below",         short:"below",         label:"Below threshold",      desc:"no dataset reaches the threshold"},
  {id:"insufficient",  short:"not enough data", label:"Not enough data",    desc:`< ${D.min_obs} observations in some dataset`},
];
const groupShort = id => (GROUPS.find(g => g.id === id) || {short: id}).short;
const TPL = {stability:"Stability", change_count:"Change count", correlation:"Correlation", path_key:"Path-key",
             value_to_key:"Value → key", key_containment:"Containment", text_store_agreement:"Text ↔ store", format_agreement:"Format label ↔ content"};
const catLabel = c => c === "*" ? "any action" : c;
const state = {group:"holds", cat:"", tpl:"", q:"", grouped:true};
const openFams = new Set();   // rule families whose per-action rules are shown
const openRows = new Set();   // individual rules whose definition is shown
D.candidates.forEach((c, i) => c.id = i);

function status(v) {
  if (!v) return "none";
  if (v.m < D.min_obs) return "thin";
  if (v.pct >= T) return "ok";
  if (v.pct >= NEAR) return "warn";
  return "bad";
}
const worst = c => Math.min(...DS.map(d => c.datasets[d] ? c.datasets[d].pct : 0));

function matches(c, {ignoreGroup = false, ignoreCat = false} = {}) {
  if (!ignoreGroup && state.group && c.group !== state.group) return false;
  if (!ignoreCat && state.cat && c.category !== state.cat) return false;
  if (state.tpl && c.template !== state.tpl) return false;
  if (state.q) {
    const hay = `${c.statement} ${c.rule.id} ${catLabel(c.category)} ${TPL[c.template] || c.template}`.toLowerCase();
    if (!hay.includes(state.q.toLowerCase())) return false;
  }
  return true;
}

function renderHeader() {
  const total = D.datasets.reduce((s, d) => s + d.snapshots, 0);
  document.getElementById("meta").textContent =
    `Source: ${D.source} · ${D.datasets.length} datasets · ${total.toLocaleString()} snapshots · ` +
    `threshold ${pct(T)} per dataset · generated ${D.generated}`;
  document.getElementById("chips").innerHTML = D.datasets
    .map(d => `<span class="chip"><b>${esc(d.name)}</b> ${d.snapshots.toLocaleString()} snapshots</span>`).join("");
  document.getElementById("legend").innerHTML =
    `<span><i class="s-ok"></i>≥ ${pct(T)} - holds</span>` +
    `<span><i class="s-warn"></i>${pct(NEAR)}-${pct(T)} - close</span>` +
    `<span><i class="s-bad"></i>< ${pct(NEAR)}</span>` +
    `<span><i class="s-thin"></i>fewer than ${D.min_obs} observations</span>` +
    `<span><i class="s-none"></i>not observed</span>` +
    `<span>Percentages count how often the statement, as written, held in that dataset. ` +
    `On a grouped row, each percentage is the lowest among that statement's rules. Click a row to expand it.</span>`;
}

function renderCards() {
  const pool = D.candidates.filter(c => matches(c, {ignoreGroup: true}));
  document.getElementById("cards").innerHTML = GROUPS.map(g => `
    <button class="card g-${g.id} ${state.group === g.id ? "active" : ""}" data-group="${g.id}">
      <div class="n">${pool.filter(c => c.group === g.id).length}</div>
      <div class="l">${g.label}</div><div class="d">${g.desc}</div>
    </button>`).join("");
}

function renderCats() {
  const pool = D.candidates.filter(c => matches(c, {ignoreGroup: true, ignoreCat: true}));
  const cats = [...new Set(D.candidates.map(c => c.category))].sort();
  document.getElementById("cats").innerHTML = cats.map(cat => {
    const inCat = pool.filter(c => c.category === cat);
    const n = inCat.length;
    const segs = GROUPS.map(g => {
      const k = inCat.filter(c => c.group === g.id).length;
      return k ? `<span class="b-${g.id}" style="width:${k / n * 100}%" title="${g.label}: ${k}"></span>` : "";
    }).join("");
    return `<div class="cat ${state.cat === cat ? "sel" : ""}" data-cat="${esc(cat)}">
      <div>${esc(catLabel(cat))}</div><div class="bar">${segs}</div><div class="num">${n}</div></div>`;
  }).join("");
}

function cellHtml(c, d) {
  const v = c.datasets[d];
  const st = status(v);
  if (!v) return `<td class="cell s-none" title="${esc(d)}: not observed">-<small>no data</small></td>`;
  return `<td class="cell s-${st}" title="${esc(d)}: held ${v.held} of ${v.m}">${pct(v.pct)}<small>${v.held}/${v.m}</small></td>`;
}

// One cell for a whole family: the lowest support among its rules in dataset d.
// Rules with too few observations are ignored unless every rule is thin.
function famCell(f, d) {
  const vs = f.members.map(c => c.datasets[d]).filter(Boolean);
  if (!vs.length) return `<td class="cell s-none" title="${esc(d)}: not observed">-<small>no data</small></td>`;
  const solid = vs.filter(v => v.m >= D.min_obs);
  const pool = solid.length ? solid : vs;
  const v = pool.reduce((a, b) => b.pct < a.pct ? b : a);
  const skipped = solid.length && solid.length < vs.length
    ? `; ${vs.length - solid.length} rule(s) with too few observations ignored` : "";
  return `<td class="cell s-${status(v)}" title="${esc(d)}: lowest support ${v.held} of ${v.m}, across ${vs.length} rule(s)${skipped}">` +
    `${pct(v.pct)}<small>${vs.length > 1 ? `lowest of ${vs.length}` : `${v.held}/${v.m}`}</small></td>`;
}

function buildFamilies(rows) {
  const byKey = new Map();
  for (const c of rows) {
    if (!byKey.has(c.family)) byKey.set(c.family, {key: c.family, statement: c.statement, template: c.template, members: []});
    byKey.get(c.family).members.push(c);
  }
  const fams = [...byKey.values()];
  fams.forEach(f => f.members.sort((a, b) => a.category.localeCompare(b.category)));
  fams.sort((a, b) => b.members.length - a.members.length || a.statement.localeCompare(b.statement));
  return fams;
}

function scopeHtml(f) {
  const n = f.members.length;
  const main = n === 1 ? esc(catLabel(f.members[0].category)) : `${n} actions`;
  const gs = GROUPS.filter(g => f.members.some(c => c.group === g.id));
  const mix = gs.length > 1
    ? `<small>${gs.map(g => `${f.members.filter(c => c.group === g.id).length} ${g.short}`).join(" · ")}</small>` : "";
  return main + mix;
}

function detailHtml(c) {
  const r = c.rule, card = r.card;
  const status = c.exported
    ? `exported to <code>${esc(D.rules_file)}</code> - checked by the mutation loop after every matching step`
    : "not exported - not promoted";
  const cardRows = [["Trigger", card.trigger], ["Applies when", card.applies_when],
                    ["Asserts", card.asserts], ["Definitions", card.definitions]]
    .map(([k, v]) => `<tr><th>${k}</th><td>${esc(v)}</td></tr>`).join("");
  const lines = DS.map(d => {
    const v = c.datasets[d];
    if (!v) return `<li><b>${esc(d)}</b>: not observed</li>`;
    const ex = (v.examples || []).map(e => {
      const extra = Object.entries(e).filter(([k]) => !["run_id", "step", "action"].includes(k))
        .map(([k, x]) => `${esc(k)}=${esc(Array.isArray(x) ? x.join(", ") : x)}`).join(" · ");
      return `run ${esc(e.run_id)} step ${esc(e.step)} <code>${esc(e.action)}</code>${extra ? ` (${extra})` : ""}`;
    }).join("; ");
    return `<li><b>${esc(d)}</b>: held ${v.held} of ${v.m} (${pct(v.pct)})${ex ? `<br><span class="ex">counter-examples: ${ex}</span>` : ""}</li>`;
  }).join("");
  return `<h4>Rule <code>${esc(r.id)}</code> · ${status} <button class="copy" data-copy="${c.id}">Copy rule JSON</button></h4>
    <table class="rulecard">${cardRows}</table>
    <h4>Evidence per dataset</h4><ul>${lines}</ul>`;
}

// What an expanded family shows: one row per action (its rule), each expandable to the rule card.
function famDetailHtml(f) {
  if (f.members.length === 1) return detailHtml(f.members[0]);
  const rows = f.members.map(c => {
    const isOpen = openRows.has(c.id);
    return `<tr class="mrow" data-id="${c.id}">
        <td class="tag">${esc(catLabel(c.category))}</td>
        <td class="tag"><code>${esc(c.rule.id)}</code></td>
        <td class="tag"><span class="badge g-${c.group}">${esc(groupShort(c.group))}</span></td>
        ${DS.map(d => cellHtml(c, d)).join("")}
        <td class="chev">${isOpen ? "▾" : "▸"}</td></tr>` +
      (isOpen ? `<tr class="mdetail"><td colspan="${DS.length + 4}"><div class="detail">${detailHtml(c)}</div></td></tr>` : "");
  }).join("");
  return `<h4>${f.members.length} rules, one per action. Click a rule for its definition and counter-examples.</h4>
    <div class="panel tablewrap"><table class="members">
      <thead><tr><th>Action</th><th>Rule</th><th>Status</th>${DS.map(d => `<th class="ds">${esc(d)}</th>`).join("")}<th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

function renderGrouped(rows) {
  const fams = buildFamilies(rows);
  const cols = DS.length + 4;
  document.getElementById("count").textContent = `${fams.length} statements · ${rows.length} rules`;
  document.getElementById("thead").innerHTML =
    `<tr><th>Statement</th><th>Applies to</th><th>Template</th>${DS.map(d => `<th class="ds">${esc(d)}</th>`).join("")}<th></th></tr>`;
  document.getElementById("tbody").innerHTML = fams.length ? fams.map(f => {
    const isOpen = openFams.has(f.key);
    return `<tr class="row fam" data-fam="${esc(f.key)}">
        <td class="stmt">${esc(f.statement)}</td>
        <td class="tag">${scopeHtml(f)}</td>
        <td class="tag">${esc(TPL[f.template] || f.template)}</td>
        ${DS.map(d => famCell(f, d)).join("")}
        <td class="chev">${isOpen ? "▾" : "▸"}</td></tr>` +
      (isOpen ? `<tr class="detail"><td colspan="${cols}"><div class="detail">${famDetailHtml(f)}</div></td></tr>` : "");
  }).join("") : `<tr><td colspan="${cols}" class="empty">No candidates match these filters.</td></tr>`;
}

function renderFlat(rows) {
  rows = rows.slice().sort((a, b) => a.category.localeCompare(b.category) || worst(b) - worst(a) || a.statement.localeCompare(b.statement));
  const cols = DS.length + 4;
  document.getElementById("count").textContent = `${rows.length} shown`;
  document.getElementById("thead").innerHTML =
    `<tr><th>Statement</th><th>Action</th><th>Template</th>${DS.map(d => `<th class="ds">${esc(d)}</th>`).join("")}<th></th></tr>`;
  document.getElementById("tbody").innerHTML = rows.length ? rows.map(c => {
    const isOpen = openRows.has(c.id);
    return `<tr class="row" data-id="${c.id}">
        <td class="stmt">${esc(c.statement)}</td>
        <td class="tag">${esc(catLabel(c.category))}</td>
        <td class="tag">${esc(TPL[c.template] || c.template)}</td>
        ${DS.map(d => cellHtml(c, d)).join("")}
        <td class="chev">${isOpen ? "▾" : "▸"}</td></tr>` +
      (isOpen ? `<tr class="detail"><td colspan="${cols}"><div class="detail">${detailHtml(c)}</div></td></tr>` : "");
  }).join("") : `<tr><td colspan="${cols}" class="empty">No candidates match these filters.</td></tr>`;
}

function renderTable() {
  const rows = D.candidates.filter(c => matches(c));
  state.grouped ? renderGrouped(rows) : renderFlat(rows);
}

function renderAll() { renderCards(); renderCats(); renderTable(); }

// Typing a complete rule ID (e.g. copied from a mutation-run report) jumps straight to that rule.
function revealRule(c) {
  Object.assign(state, {group: c.group, cat: "", tpl: ""});
  document.getElementById("cat").value = "";
  document.getElementById("tpl").value = "";
  openFams.add(c.family);
  openRows.add(c.id);
}

function toggle(set, key) { set.has(key) ? set.delete(key) : set.add(key); }

function initFilters() {
  const cats = [...new Set(D.candidates.map(c => c.category))].sort();
  const tpls = [...new Set(D.candidates.map(c => c.template))].sort();
  document.getElementById("cat").innerHTML = `<option value="">All actions</option>` +
    cats.map(c => `<option value="${esc(c)}">${esc(catLabel(c))}</option>`).join("");
  document.getElementById("tpl").innerHTML = `<option value="">All templates</option>` +
    tpls.map(t => `<option value="${esc(t)}">${esc(TPL[t] || t)}</option>`).join("");

  document.getElementById("q").addEventListener("input", e => {
    state.q = e.target.value.trim();
    const hit = state.q && D.candidates.find(c => c.rule.id.toLowerCase() === state.q.toLowerCase());
    if (hit) revealRule(hit);
    renderAll();
  });
  document.getElementById("cat").addEventListener("change", e => { state.cat = e.target.value; renderAll(); });
  document.getElementById("tpl").addEventListener("change", e => { state.tpl = e.target.value; renderAll(); });
  document.getElementById("grp").addEventListener("change", e => { state.grouped = e.target.checked; renderAll(); });
  document.getElementById("reset").addEventListener("click", () => {
    Object.assign(state, {group: "", cat: "", tpl: "", q: ""});
    document.getElementById("q").value = "";
    document.getElementById("cat").value = "";
    document.getElementById("tpl").value = "";
    renderAll();
  });
  document.getElementById("cards").addEventListener("click", e => {
    const b = e.target.closest("[data-group]");
    if (!b) return;
    state.group = state.group === b.dataset.group ? "" : b.dataset.group;
    renderAll();
  });
  document.getElementById("cats").addEventListener("click", e => {
    const r = e.target.closest("[data-cat]");
    if (!r) return;
    state.cat = state.cat === r.dataset.cat ? "" : r.dataset.cat;
    document.getElementById("cat").value = state.cat;
    renderAll();
  });
  document.getElementById("tbody").addEventListener("click", e => {
    const copyBtn = e.target.closest("[data-copy]");
    if (copyBtn) {
      e.stopPropagation();
      const rule = D.candidates[+copyBtn.dataset.copy].rule;
      navigator.clipboard?.writeText(JSON.stringify(rule, null, 2)).then(() => { copyBtn.textContent = "Copied"; });
      return;
    }
    const member = e.target.closest("tr.mrow");
    if (member) { toggle(openRows, +member.dataset.id); renderTable(); return; }
    const fam = e.target.closest("tr.fam");
    if (fam) { toggle(openFams, fam.dataset.fam); renderTable(); return; }
    const flat = e.target.closest("tr.row");
    if (flat) { toggle(openRows, +flat.dataset.id); renderTable(); }
  });
}

renderHeader();
initFilters();
renderAll();
</script>
</body>
</html>
"""