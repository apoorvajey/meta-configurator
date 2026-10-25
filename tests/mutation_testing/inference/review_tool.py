"""Review the promoted rules family by family and keep the decisions in config/rule_reviews/.

    python -m tests.cli review html           review.html, opened in a browser; starts from the newest review file;
                                              its download button saves rule_review_<date>_<time>.json
    python -m tests.cli review import         copy the newest rule_review_*.json from Downloads into rule_reviews/
                                              (--downloads DIR, --json FILE)
    python -m tests.cli review export         review_sheet.csv in the newest inference run folder (--run-dir)
    python -m tests.cli review import-sheet   read the filled-in sheet and save it as a new file in rule_reviews/

A rule family is every reviewable rule with the same template, sentence and confidence tier. The sheet has one row
per family; fill in "decision" (empty = unreviewed) and optionally "bug" (e.g. BUG-02) and "note":

    accepted      a correct invariant: exported to inferred_rules.json and checked in every run. Rules that missed
                  the threshold in some dataset can be accepted too (manual promotion); they are marked "manual"
                  and the loop says so when they fire
    rejected      not a real invariant (coincidence of the traces, wrong template, ...)
    trivial       true but obvious; not checked
    learned_bug   the app does this often but it is unexpected: a bug the inference learned; not checked, kept as a finding

Only "accepted" rules are checked in the loop; the other decisions differ only as bookkeeping. A decision applies
to every rule of the family; untick "Same decision for all N actions" on the page to decide per action. A decision
stores the tier it was made at; when a later run puts the rule in another tier, the page shows "evidence changed".
Rule ids hash template, category, params and direction, so decisions survive re-runs.

Confidence tiers (the page filters on them): holds (held at the threshold in every dataset), investigate (reached
it in some datasets), below (in none), insufficient (too few observations in a dataset), contradictory (datasets
point in opposite directions).

The sheet uses ';' as separator and UTF-8 with BOM, which German-locale Excel opens directly.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.common.paths import INBOX_DIR
from tests.mutation_testing.common.report_theme import DARK_VARS
from tests.mutation_testing.inference.learned_bug import negation_unsupported_reason
from tests.mutation_testing.inference.review_store import (REVIEW_DIR, default_download_dir, install_review, latest_review_file,
                                                 load_review, review_files, save_review)

RUNS_DIR = "tests/output/reports/inference/runs"
REVIEW_HTML = "tests/output/reports/inference/review.html"   # always the latest review page; run folders do not keep a copy
SHEET_NAME = "review_sheet.csv"
LOGS_DIR = "tests/output/runs"                # tests/output/runs/<run_id>/mutation_log.html, written by the loop
DECISIONS = ("accepted", "rejected", "trivial", "learned_bug")
TIERS = {   # candidates.json 'group' -> label, in the order of the filter
    "holds": "Holds everywhere",
    "investigate": "Close",
    "below": "Weak",
    "insufficient": "Thin evidence",
    "contradictory": "Contradictory",
}
TIER_HELP = {
    "holds": "held at the threshold in every dataset",
    "investigate": "reached the threshold in some datasets, not in all",
    "below": "reached the threshold in no dataset",
    "insufficient": "too few observations in at least one dataset",
    "contradictory": "the datasets point in opposite directions",
}
COLUMNS = ["decision", "bug", "note", "tier", "template", "statement", "rules", "actions", "min_support",
           "weakest_dataset", "counter_example", "family"]


def _latest(root: str) -> Path:
    runs = sorted(p for p in Path(root).iterdir() if p.is_dir()) if Path(root).exists() else []
    if not runs:
        raise SystemExit(f"No inference runs found in {root}. Run: python -m tests.cli infer")
    return runs[-1]


def _payload(run_dir: Path) -> dict:
    return json.loads((run_dir / "candidates.json").read_text(encoding="utf-8"))


def _families(run_dir: Path) -> dict[str, list[dict]]:
    """Reviewable rules grouped by statement and confidence tier. Older run folders have no 'reviewable' flag; they list the exported rules."""
    fams: dict[str, list[dict]] = defaultdict(list)
    for c in _payload(run_dir)["candidates"]:
        if c.get("reviewable", c["exported"]):
            fams[f"{c['family']}|{c['group']}"].append(c)
    return fams


def _counter_example(rules: list[dict]) -> str:
    for c in rules:
        for ds in c["datasets"].values():
            if ds.get("examples"):
                return json.dumps(ds["examples"][0], ensure_ascii=False)[:160]
    return ""


def _log_link(run_id, step) -> str:
    """file:// link to a step of a run's mutation log (relative to the folder you run the command in), or ''."""
    if not run_id:
        return ""
    from tests.mutation_testing.runs.run_layout import find_run_dir
    log = (find_run_dir(str(run_id), Path(LOGS_DIR)) or Path(LOGS_DIR) / str(run_id)) / "mutation_log.html"
    return log.resolve().as_uri() + (f"#step-{step}" if step else "") if log.is_file() else ""


def _examples(rules: list[dict], key: str, limit: int = 3) -> list[dict]:
    """Up to `limit` steps from the family's rules: key "examples" = where it did not hold, "support" = where it held."""
    out, seen = [], set()
    for c in rules:
        for ds, v in c["datasets"].items():
            for ex in v.get(key) or []:
                if (ex.get("run_id"), ex.get("step")) in seen:
                    continue
                seen.add((ex.get("run_id"), ex.get("step")))
                detail = {k: x for k, x in ex.items() if k not in ("run_id", "step", "action")}
                out.append({"ds": ds, "run": ex.get("run_id"), "step": ex.get("step"), "action": ex.get("action"),
                            "detail": json.dumps(detail, ensure_ascii=False)[:160],
                            "link": _log_link(ex.get("run_id"), ex.get("step"))})
                if len(out) >= limit:
                    return out
    return out


def export_sheet(run_dir: Path, review_dir: str) -> Path:
    review = load_review(review_dir)
    rows = []
    for fam, rules in _families(run_dir).items():
        ids = [c["rule"]["id"] for c in rules]
        statuses = {review.get(i, {}).get("status") for i in ids}
        weakest = min(((ds, v["pct"]) for c in rules for ds, v in c["datasets"].items()), key=lambda t: t[1])
        rows.append({
            "decision": statuses.pop() or "" if len(statuses) == 1 else "",
            "bug": "", "note": "", "tier": TIERS.get(rules[0]["group"], rules[0]["group"]),
            "template": rules[0]["template"], "statement": rules[0]["statement"], "rules": len(rules),
            "actions": ", ".join(sorted(c["category"] for c in rules)),
            "min_support": f"{min(c['rule']['min_support'] for c in rules):.0%}",
            "weakest_dataset": f"{weakest[0]} {weakest[1]:.0%}",
            "counter_example": _counter_example(rules), "family": fam,
        })
    rows.sort(key=lambda r: (list(TIERS.values()).index(r["tier"]) if r["tier"] in TIERS.values() else len(TIERS),
                             r["template"], r["statement"]))
    out = run_dir / SHEET_NAME
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    print(f"[Review] {len(rows)} families ({sum(r['rules'] for r in rows)} rules) -> {out}")
    print(f"[Review] fill in the 'decision' column ({' / '.join(DECISIONS)}), then: python -m tests.cli review import-sheet")
    return out


def import_sheet(run_dir: Path, review_dir: str) -> None:
    sheet = run_dir / SHEET_NAME
    if not sheet.exists():
        raise SystemExit(f"{sheet} not found - run 'review export' first")
    fams = _families(run_dir)
    review = load_review(review_dir)
    applied = skipped = 0
    with sheet.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            decision = (row.get("decision") or "").strip().lower()
            if not decision:
                skipped += 1
                continue
            if decision not in DECISIONS:
                raise SystemExit(f"Unknown decision '{decision}' for: {row['statement'][:70]} (use {', '.join(DECISIONS)})")
            for c in fams.get(row["family"], []):
                entry = {"status": decision}
                if row.get("bug", "").strip():
                    entry["bug"] = row["bug"].strip()
                if row.get("note", "").strip():
                    entry["note"] = row["note"].strip()
                review[c["rule"]["id"]] = {**review.get(c["rule"]["id"], {}), **entry}
                applied += 1
    review_file = save_review(review, review_dir)
    counts = defaultdict(int)
    for v in review.values():
        counts[v.get("status", "?")] += 1
    print(f"[Review] {applied} rule decision(s) written to {review_file}; {skipped} family row(s) left undecided")
    print("[Review] totals: " + ", ".join(f"{k}={n}" for k, n in sorted(counts.items())))
    from tests.mutation_testing.inference.rule_history import write_history
    print(f"[Review] rule history updated: {write_history(review_dir)}")


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Rule Review</title><style>
:root{__VARS__}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
.bar{max-width:1000px;margin:0 auto;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:18px;margin:0;flex:1 1 auto}input,select,button,textarea{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
button{cursor:pointer}button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.prog{max-width:1000px;margin:0 auto;height:6px;background:var(--chip);border-radius:6px;overflow:hidden}.prog div{height:100%;background:var(--accent);width:0}
main{max-width:1000px;margin:0 auto;padding:16px 16px 80px}.hint{color:var(--mute);font-size:13px;margin:0 0 14px}
.card{background:var(--panel);border:1px solid var(--line);border-left:5px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 12px}
.card.accepted{border-left-color:var(--ok)}.card.rejected{border-left-color:var(--no)}.card.trivial{border-left-color:var(--triv)}.card.learned_bug{border-left-color:var(--bug)}
.st{font-weight:600;margin:0 0 6px}.tags{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 8px}.tag{background:var(--chip);border-radius:20px;padding:1px 10px;font-size:12.5px}.tag.prev{color:var(--warn)}.ex{margin:6px 0 6px 12px}.ex a{color:var(--accent)}.mute{color:var(--mute)}
.tag.t-holds{color:var(--good)}.tag.t-investigate{color:var(--warn)}.tag.t-below{color:var(--warn)}.tag.t-insufficient{color:var(--mute)}.tag.t-contradictory{color:var(--bad)}
.tag.hand{color:var(--accent)}.tag.thin{color:var(--mute);border:1px dashed var(--line);background:none}.tag.low{color:var(--warn)}
.meter{display:inline-block;width:70px;height:7px;background:var(--chip);border-radius:6px;vertical-align:middle;overflow:hidden}.meter i{display:block;height:100%;background:var(--ok)}
.note{color:var(--mute);font-size:13px;margin:0 0 8px}
.dec{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.dec button{padding:4px 12px}.dec button.on{color:#fff;border-color:transparent}
.dec button[data-d=accepted].on{background:var(--ok)}.dec button[data-d=rejected].on{background:var(--no)}.dec button[data-d=trivial].on{background:var(--triv)}.dec button[data-d=learned_bug].on{background:var(--bug)}
.bugbox{flex:1 1 100%;border:1px solid var(--line);border-radius:8px;padding:10px;display:grid;gap:10px;margin-top:4px}.bugbox .bt{margin:0;font-weight:600}.fld{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--mute)}.bugbox .fld input,.bugbox .fld select,.bugbox .fld textarea{flex:none;width:100%;font-size:14px;color:var(--ink)}.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.tabs{display:flex;gap:4px;max-width:1000px;margin:8px auto 0}.tabs button{border-radius:8px 8px 0 0;border-bottom:none;color:var(--mute)}.tabs button.on{color:var(--ink);background:var(--chip);border-color:var(--accent)}
.sec{border:1px solid var(--line);border-radius:10px;margin:8px 0;background:var(--panel)}.sech{cursor:pointer;padding:10px 14px;display:flex;gap:10px;align-items:center;font-weight:600}
.sec .arrow::before{content:"▸";color:var(--mute)}.sec.open .arrow::before{content:"▾"}.sec.finished .arrow::before{content:"✓";color:var(--ok)}.sec.finished .sech{cursor:default;opacity:.6}
.cnt{margin-left:auto;font-weight:400;font-size:13px;color:var(--mute);display:flex;gap:10px}.cnt .need{color:var(--bug)}.cnt .done{color:var(--ok)}.sname{word-break:break-all}
.rows{border-top:1px solid var(--line)}.rowc{border-bottom:1px solid var(--line)}.rowc:last-child{border-bottom:none}
.rowh{cursor:pointer;display:grid;grid-template-columns:12px 1fr auto;gap:10px;align-items:center;padding:8px 14px}.rowh .st{margin:0}.rowh small{display:block;color:var(--mute);font-size:12px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--ok)}.dot.close{background:var(--bug)}.dot.weak{background:var(--no)}.dot.thin{background:var(--mute)}
.rowh .dec{margin:0;gap:4px}.rowh .dec button{padding:2px 9px;font-size:13px}
.more{padding:4px 14px 12px 36px}.big{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-top:10px}.big>.st{font-size:19px}
.big .dec{margin:14px 0;gap:8px}.big .dec button{padding:8px 16px;font-size:15px}.kbd{font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 5px;margin-left:6px;color:var(--mute)}
.nav{display:flex;gap:8px;margin-top:14px;align-items:center;color:var(--mute);font-size:13px}.nav .sp{flex:1}
.row{display:flex;flex-wrap:wrap;gap:8px}.row input{flex:1 1 140px}.row input.note{flex:3 1 260px}
details{margin:6px 0;color:var(--mute);font-size:13.5px}summary{cursor:pointer}code{font-size:12.5px;word-break:break-all}
details.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:0 0 14px;color:var(--ink);font-size:14px}
details.panel summary{font-weight:600}.panel p{color:var(--mute);font-size:13px;margin:8px 0}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:600}
.s-recovered{color:var(--good)}.s-partial,.s-seen{color:var(--warn)}.s-missing{color:var(--bad)}.s-no_evidence,.s-not_expressible{color:var(--mute)}
.empty{color:var(--mute);text-align:center;padding:40px}
.allrow{display:flex;align-items:center;gap:8px;margin:8px 0;cursor:pointer;font-size:14px}input[type=checkbox]{padding:0;width:16px;height:16px;accent-color:var(--accent)}
.act{border-top:1px solid var(--line);padding:8px 0}.act .nm{font-weight:600;font-size:13.5px;margin:0 0 4px;word-break:break-all}.act .dec{margin:4px 0}
h2{font-size:16px;margin:22px 0 8px;display:flex;align-items:center;gap:10px}h2 .n{font-weight:400;color:var(--mute);font-size:14px}
h2 label{margin-left:auto;font-size:13px;font-weight:400;color:var(--mute);display:flex;gap:6px;align-items:center;cursor:pointer}
.sum{max-width:1000px;margin:8px auto 0;font-size:13px;color:var(--mute)}.sum b{color:var(--ink)}.tag.stay{color:var(--ok)}
details.panel .card{background:var(--bg)}details.panel .row{margin:8px 0}.tag.id{font-family:ui-monospace,Consolas,monospace}
</style></head><body>
<header><div class=prog><div id=pb></div></div><div class=sum id=tot></div><div class=bar style="margin-top:8px"><h1>Rule review</h1>
<button id=rreset title="Forget every decision made on this page since it was opened">Discard my changes</button>
<button class=primary id=dl>Download review file</button></div>
<div class=bar style="margin-top:8px"><select id=ftier title="Confidence tier"></select>
<select id=fdec title="Decision"></select>
<select id=ftpl title="Template"></select>
<select id=fact title="Action"></select>
<select id=fsort title="Order"><option value=name>Order: template</option><option value=action>Order: action (A-Z)</option><option value=low>Order: weakest support first</option><option value=high>Order: strongest support first</option><option value=rules>Order: most rules first</option></select>
<input id=q type=search placeholder="Search statements, ids and actions"></div>
<div class=tabs><button id=tb class=on>Browse by action</button><button id=tr>Review mode</button></div>
<div class=sum id=sum></div></header>
<main><p class=hint>Rule families are grouped by action; open a section, then click a row for the evidence and the note. A family is the same statement and tier across actions: untick <b>Same decision for all</b> in its row to decide per action. The dot is the confidence tier: green holds everywhere, orange close, red weak or contradictory, grey thin evidence (hover for details). <b>Review mode</b> goes through the undecided families one at a time (keys A / R / T / L).
<b>accepted</b> = checked in the loop · <b>rejected</b> = not a real invariant · <b>trivial</b> = true but obvious · <b>learned bug</b> = frequent but unexpected.
Undecided actions are not exported. Choices stay in this browser. When done, click <b>Download review file</b>; then press Enter in the terminal where <code>python -m tests.cli review</code> is running:
it moves the file to <code>tests/mutation_testing/config/rule_reviews/</code> and rewrites the checked rules (and the learned-bug checks). The newest file in that folder is the one in use.</p>
<div id=manual></div>
<div id=bpane><h2 id=fhead></h2><div id=list></div>
<h2 id=rhead><span id=rtitle></span><label><input type=checkbox id=rshow> Show saved rules</label></h2>
<p class=note id=rnote>Every rule saved in the newest review file, grouped by action. Same filters, order and download button as above.</p>
<div id=rlist></div></div><div id=review hidden></div><datalist id=groups></datalist></main>
<script>
const DATA=__DATA__;const BASE=__BASE__;const KEY="rule-review:"+DATA.run;
let st={};try{st=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){}
const STAMP=()=>{const d=new Date(),p=n=>String(n).padStart(2,"0");return ""+d.getFullYear()+p(d.getMonth()+1)+p(d.getDate())+"_"+p(d.getHours())+p(d.getMinutes())+p(d.getSeconds())};
let RS={};try{RS=JSON.parse(localStorage.getItem(KEY+":rules")||"{}")}catch(e){}   // per-rule edits: {ruleId:{d,note,bug}}, only where they differ from the review file this page started from
const save=()=>{try{localStorage.setItem(KEY,JSON.stringify(st));localStorage.setItem(KEY+":rules",JSON.stringify(RS))}catch(e){}};
const $=s=>document.querySelector(s);const DEC=["accepted","rejected","trivial","learned_bug"];const KEPT=["accepted","trivial","learned_bug"];
const TIER=Object.fromEntries(DATA.tiers.map(t=>[t.key,t]));
const STATUS={recovered:"Found",partial:"Partly found",seen:"Seen, not promotable",missing:"Missing",no_evidence:"Cannot be tested",not_expressible:"No template yet"};
function el(tag,cls,txt){const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
const changed=f=>f.prev&&f.prevTier&&f.prevTier!==f.tier;
const pct=x=>Math.round(100*x)+"%";
const baseOf=id=>BASE[id]||{};
const effOf=id=>RS[id]||{d:baseOf(id).status||"",note:baseOf(id).note||"",bug:baseOf(id).bug||"",chk:!!baseOf(id).check,bn:baseOf(id).bugName||"",bg:baseOf(id).bugGroup||"",bw:baseOf(id).bugWhere||"",bs:baseOf(id).bugSummary||"",bc:baseOf(id).bugNotes||""};
const chg=id=>!!RS[id]&&(RS[id].d!==(baseOf(id).status||"")||(RS[id].note||"")!==(baseOf(id).note||"")||(RS[id].bug||"")!==(baseOf(id).bug||"")||!!RS[id].chk!==!!baseOf(id).check||(RS[id].bn||"")!==(baseOf(id).bugName||"")||(RS[id].bg||"")!==(baseOf(id).bugGroup||"")||(RS[id].bw||"")!==(baseOf(id).bugWhere||"")||(RS[id].bs||"")!==(baseOf(id).bugSummary||"")||(RS[id].bc||"")!==(baseOf(id).bugNotes||""));
const setR=(id,p)=>{RS[id]=Object.assign({},effOf(id),p);if(!chg(id))delete RS[id]};
const famD=f=>{const u=new Set(f.ids.map(id=>effOf(id).d));return u.size===1?[...u][0]:"mixed"};
const needs=f=>f.ids.some(id=>!effOf(id).d)||changed(f);
const common=(f,k)=>{const u=new Set(f.ids.map(id=>effOf(id)[k]||""));return u.size===1?[...u][0]:""};
DATA.families.forEach(f=>{const s=st[f.id]||(st[f.id]={});
 if(!s.m){s.m=1;if(s.d!==undefined&&s.d!==(f.decision||""))f.ids.forEach(id=>{if(!RS[id])RS[id]={d:s.d,note:s.note||"",bug:s.bug||""}})}   // decisions made on the older page
 if(s.all===undefined)s.all=new Set(f.ids.map(id=>baseOf(id).status||"")).size===1});
Object.keys(RS).forEach(id=>{if(!chg(id))delete RS[id]});
const F=()=>({tier:$("#ftier").value,dec:$("#fdec").value,tpl:$("#ftpl").value,act:$("#fact").value,q:$("#q").value.toLowerCase()});
const pin=new Set(),pinR=new Set();   // cards decided just now: they stay in the list until a filter changes
const OPTS={ftier:[["","All tiers"]].concat(DATA.tiers.filter(t=>DATA.families.some(f=>f.tier===t.key)).map(t=>[t.key,t.label])),
 fdec:[["needs","Needs decision"],["","All decisions"],["_prev","Decided earlier"],["accepted","Accepted"],["rejected","Rejected"],["trivial","Trivial"],["learned_bug","Learned bug"],["_chg","Changed this session"]],
 ftpl:[["","All templates"]].concat([...new Set(DATA.families.map(f=>f.template).concat(DATA.rules.map(r=>r.template)).filter(Boolean))].sort().map(t=>[t,t])),
 fact:[["","All actions"]].concat([...new Set(DATA.families.flatMap(f=>f.actions).concat(DATA.rules.map(r=>r.action)).filter(Boolean))].sort().map(t=>[t,t]))};
const KEYOF={ftier:"tier",fdec:"dec",ftpl:"tpl",fact:"act"};
Object.keys(OPTS).forEach(id=>OPTS[id].forEach(([v,l])=>{const o=el("option",null,l);o.value=v;const t=DATA.tiers.find(t=>t.key===v);if(id==="ftier"&&t)o.title=t.help;$("#"+id).appendChild(o)}));
$("#ftier").value=DATA.families.some(f=>f.tier==="holds")?"holds":"";
try{$("#rshow").checked=localStorage.getItem(KEY+":showrules")==="1"}catch(e){}
const SHOW_MANUAL_PANEL=false; // set to true to show the hand-written invariants panel (recall / precision)
function renderManual(){const box=$("#manual");box.textContent="";const m=DATA.manual;if(!m||!SHOW_MANUAL_PANEL)return;const s=m.summary;
 const fdv=f=>{const d=famD(f);return DEC.includes(d)?d:""};
 const decided=DATA.families.filter(f=>fdv(f)),kept=decided.filter(f=>KEPT.includes(fdv(f)));
 const d=el("details","panel");
 d.appendChild(el("summary",null,"Hand-written invariants: the engine found "+s.recovered+" of "+s.testable+" testable (recall "+pct(s.recall)+")"+
  " · Your decisions: "+(decided.length?kept.length+" of "+decided.length+" kept (precision "+pct(kept.length/decided.length)+")":"none yet")));
 d.appendChild(el("p",null,"Recall: of the hand-written invariants that a template can express and the traces can test, how many the engine found on its own at the threshold. "+
  "Precision: of the rule families you decided on, how many you kept (accepted, trivial or learned bug); rejected ones count as wrong. "+
  s.no_evidence+" invariant(s) cannot be tested because the traces lack that action; "+s.not_expressible+" have no template yet."));
 const t=el("table");const h=el("tr");["Invariant","What it says","Engine","Why"].forEach(x=>h.appendChild(el("th",null,x)));t.appendChild(h);
 m.entries.forEach(e=>{const r=el("tr");r.appendChild(el("td",null,e.id));r.appendChild(el("td",null,e.name));
  r.appendChild(el("td","s-"+e.status,STATUS[e.status]||e.status));
  r.appendChild(el("td","mute",e.reason||e.note||(e.status==="seen"?e.categories.map(c=>c.category+": "+c.verdicts.join(", ")).join("; "):"")));t.appendChild(r)});
 d.appendChild(t);box.appendChild(d)}
function famMatch(f,o,pinOk){
 if(o.tier&&f.tier!==o.tier)return false;if(o.tpl&&f.template!==o.tpl)return false;if(o.act&&!f.actions.includes(o.act))return false;
 if(o.q&&!(f.statement+" "+f.actions.join(" ")+" "+f.hand.join(" ")).toLowerCase().includes(o.q))return false;
 if(pinOk&&pin.has(f.id))return true;
 if(o.dec==="needs")return needs(f);if(o.dec==="_prev")return f.prev;if(o.dec==="_chg")return f.ids.some(chg);
 return o.dec?f.ids.some(id=>effOf(id).d===o.dec):true}
function ruleMatch(r,o,pinOk){
 if(o.tier&&r.tier!==o.tier)return false;if(o.tpl&&r.template!==o.tpl)return false;if(o.act&&r.action!==o.act)return false;
 if(o.q&&!(r.id+" "+r.statement+" "+r.action+" "+r.template).toLowerCase().includes(o.q))return false;
 if(pinOk&&pinR.has(r.id))return true;
 if(o.dec==="needs")return !effOf(r.id).d;if(o.dec==="_prev")return !!baseOf(r.id).status;if(o.dec==="_chg")return chg(r.id);
 return o.dec?effOf(r.id).d===o.dec:true}
const sorters={name:(a,b)=>a.template.localeCompare(b.template)||a.statement.localeCompare(b.statement),
 action:(a,b)=>(a.actions[0]||"").localeCompare(b.actions[0]||"")||sorters.name(a,b),
 low:(a,b)=>a.minSupport-b.minSupport||sorters.name(a,b),high:(a,b)=>b.minSupport-a.minSupport||sorters.name(a,b),
 rules:(a,b)=>b.rules-a.rules||sorters.name(a,b)};
const rname=(a,b)=>(a.template||"").localeCompare(b.template||"")||(a.statement||"").localeCompare(b.statement||"");
const rsorters={name:rname,action:(a,b)=>(a.action||"").localeCompare(b.action||"")||rname(a,b),
 low:(a,b)=>(a.minSupport??0)-(b.minSupport??0)||rname(a,b),high:(a,b)=>(b.minSupport??0)-(a.minSupport??0)||rname(a,b),rules:rname};
const redraw=()=>{save();render()};
function refresh(){renderRules();updateCounts()}
function updateCounts(){const o=F(),all=DATA.families,need=all.filter(needs).length,left=all.filter(f=>famMatch(f,Object.assign({},o,{dec:"needs"}),false)).length;
 Object.keys(OPTS).forEach(id=>[...$("#"+id).options].forEach((op,i)=>{op.textContent=OPTS[id][i][1]}));
 $("#fhead").textContent="Families";$("#rtitle").textContent="Saved rules";
 const inF=all.filter(f=>famMatch(f,Object.assign({},o,{dec:""}),false)),nIn=inF.filter(needs).length,dn={};
 inF.filter(f=>!needs(f)).forEach(f=>{const k=famD(f).replace("_"," ");dn[k]=(dn[k]||0)+1});
 const nd=Object.keys(dn).map(k=>dn[k]+" "+k).join(" · ");
 $("#tot").textContent=need+" of "+all.length+" families still need a decision";
 $("#sum").textContent="With the current filters: "+nIn+" need a decision · "+(inF.length-nIn)+" decided"+(nd?" ("+nd+")":"");
 $("#tr").textContent="Review mode · "+left+" left";
 $("#pb").style.width=(100*(all.length-need)/Math.max(1,all.length))+"%"}
function fld(label,input,req){const l=el("label","fld");l.appendChild(el("span",null,label+(req?" *":"")));l.appendChild(input);return l}
function chkRow(ids,reason,fid,stmt){const w=el("div","row"),e0=effOf(ids[0]);
 const put2=(key,i)=>{i.oninput=()=>{ids.forEach(id=>setR(id,{[key]:i.value}));save();refresh()};return i};
 const txt=(key,val,rows)=>{const i=rows?el("textarea"):el("input");if(rows)i.rows=rows;i.value=val||"";return put2(key,i)};
 const idBox=txt("bug",e0.bug);idBox.placeholder="e.g. BUG-11";
 if(reason){w.appendChild(el("p","note","No invariant can be created for this rule: "+reason+". The bug is only logged."));const p=el("div","bugbox");p.appendChild(fld("Bug ID (label only)",idBox));w.appendChild(p);return w}
 const lb=el("label","allrow");const cb=el("input");cb.type="checkbox";cb.checked=ids.every(id=>!!effOf(id).chk);
 cb.onchange=()=>{ids.forEach(id=>setR(id,{chk:cb.checked}));save();pin.add(fid);redraw()};
 lb.appendChild(cb);lb.appendChild(document.createTextNode("Also create an invariant that fails whenever this happens (checked in every run)"));w.appendChild(lb);
 const p=el("div","bugbox");
 if(!cb.checked){p.appendChild(fld("Bug ID (label only)",idBox));w.appendChild(p);return w}
 p.appendChild(el("p","bt","Bug to log in the known bugs list"));
 const sel=el("select");[["","choose..."],["data","Data Editor"],["schema","Schema Editor"],["both","Both / any"]].forEach(([v,t])=>{const o=el("option",null,t);o.value=v;sel.appendChild(o)});
 sel.value=e0.bw||"";sel.onchange=()=>{ids.forEach(id=>setR(id,{bw:sel.value}));save();refresh()};
 const g=txt("bg",e0.bg);g.setAttribute("list","groups");
 const sm=txt("bs",e0.bs||("Learned bug: "+stmt),2);
 const top=el("div","two");top.appendChild(fld("Bug ID",idBox,true));top.appendChild(fld("Where",sel,true));p.appendChild(top);
 p.appendChild(fld("Name",txt("bn",e0.bn)));p.appendChild(fld("Summary",sm));
 const mid=el("div","two");mid.appendChild(fld("Group (same root cause)",g));p.appendChild(mid);
 p.appendChild(fld("Bug notes",txt("bc",e0.bc,2)));
 p.appendChild(el("p","note","Logged on the next: python -m tests.cli infer --export-only. The rule Note above stays with the rule. The bugs page shows the bug as learned until you confirm it."));
 w.appendChild(p);return w}
function decButtons(cur,onPick){const dec=el("div","dec");DEC.forEach(k=>{const b=el("button",cur===k?"on":"",k.replace("_"," "));b.dataset.d=k;b.onclick=()=>onPick(k);dec.appendChild(b)});return dec}
let mode="browse",openSec,cur=null;const openRows=new Set();
const actBase=a=>a.replace(/#(first|later)$/,"");
const famSec=f=>{const b=[...new Set(f.actions.map(actBase))];return b.length===1?(b[0]==="*"?"All actions":b[0]):"Several actions"};
const DOT={holds:"",investigate:"close",below:"weak",insufficient:"thin",contradictory:"weak"};
const pickFam=(f,k)=>{const dd=famD(f);f.ids.forEach(id=>setR(id,{d:dd===k?"":k}));pin.add(f.id);if(k==="learned_bug"&&dd!==k)openRows.add(f.id);redraw()};
function famParts(f,c,decEl){const s=st[f.id],dd=famD(f),multi=f.items.length>1,perAction=multi&&!s.all,tl=TIER[f.tier];
 const tags=el("div","tags");const tt=el("span","tag t-"+f.tier,tl.label);tt.title=tl.help;tags.appendChild(tt);
 if(decEl){tags.appendChild(el("span","tag",f.template));if(f.rules>1)tags.appendChild(el("span","tag",f.rules+" actions"))}
 f.hand.forEach(h=>{const x=el("span","tag hand","also hand-written: "+h);x.title="A hand-written invariant says the same";tags.appendChild(x)});
 if(changed(f))tags.appendChild(el("span","tag prev","evidence changed: was "+(TIER[f.prevTier]||{label:f.prevTier}).label+", now "+tl.label));
 else if(f.prev)tags.appendChild(el("span","tag prev","decided earlier"));
 if(dd==="mixed"){const n={};f.ids.forEach(id=>{const k=(effOf(id).d||"undecided").replace("_"," ");n[k]=(n[k]||0)+1});
  tags.appendChild(el("span","tag prev","mixed: "+Object.keys(n).map(k=>n[k]+" "+k).join(", ")))}
 if(pin.has(f.id)&&!famMatch(f,F(),false))tags.appendChild(el("span","tag stay","decided just now · stays until you change a filter"));
 if(decEl){const sup=el("span","tag");const m=el("span","meter");const i=el("i");i.style.width=f.minSupport+"%";m.appendChild(i);sup.appendChild(m);
 sup.appendChild(document.createTextNode(" weakest support "+f.minSupport+"%"));tags.appendChild(sup)}c.appendChild(tags);
 const ds=el("div","tags");f.datasets.forEach(d=>{let txt,cls="tag";
  if(d.n===0){txt=d.name+": no data";cls+=" thin"}else if(d.thin){txt=d.name+": "+pct(d.pct)+" of only "+d.n;cls+=" thin"}
  else{txt=d.name+": "+pct(d.pct)+" · n="+d.n;if(d.pct<DATA.threshold)cls+=" low"}
  ds.appendChild(el("span",cls,txt))});c.appendChild(ds);
 if(f.tier!=="holds")c.appendChild(el("p","note","Accepting this rule promotes it by hand: it is checked in the loop although it did not hold in every dataset at "+pct(DATA.threshold)+"."));
 if(decEl&&!perAction)c.appendChild(decEl);
 const showEx=(title,items)=>{if(!items||!items.length)return;const x=el("details");x.appendChild(el("summary",null,title+" ("+items.length+")"));
  items.forEach(e=>{const li=el("div","ex");li.appendChild(el("span",null,e.ds+" · run "+e.run+" · step "+e.step+" · "+e.action+" · "));
   if(e.link){const a=el("a",null,"open step");a.href=e.link;a.target="_blank";li.appendChild(a)}else li.appendChild(el("span","mute","log not found"));
   li.appendChild(el("br"));li.appendChild(el("code",null,e.detail));x.appendChild(li)});c.appendChild(x)};
 if(!perAction&&(decEl||f.actions.length>1)){const d=el("details");d.appendChild(el("summary",null,"Actions ("+f.actions.length+")"));d.appendChild(el("div",null,f.actions.join(", ")));c.appendChild(d)}
 showEx("Steps where it did not hold",f.fails);showEx("Steps where it held",f.holds);
 if(multi){const lb=el("label","allrow");const cb=el("input");cb.type="checkbox";cb.checked=!!s.all;
  cb.onchange=()=>{s.all=cb.checked;save();render()};lb.appendChild(cb);lb.appendChild(document.createTextNode("Same decision for all "+f.items.length+" actions"));c.appendChild(lb)}
 if(!perAction){
  const row=el("div","row");
  const note=el("input","note");note.placeholder=multi?"Note (all actions)":"Note";note.value=common(f,"note");note.oninput=()=>{f.ids.forEach(id=>setR(id,{note:note.value}));save();refresh()};
  row.appendChild(note);c.appendChild(row);
  if(dd==="learned_bug")c.appendChild(chkRow(f.ids,f.negReason,f.id,f.statement))
 }else f.items.forEach(it=>{const e=effOf(it.id);const a=el("div","act");a.appendChild(el("p","nm",it.action));
  a.appendChild(decButtons(e.d,k=>{setR(it.id,{d:e.d===k?"":k});pin.add(f.id);redraw()}));
  const note=el("input","note");note.placeholder="Note";note.value=e.note||"";note.oninput=()=>{setR(it.id,{note:note.value});save();refresh()};a.appendChild(note);
  if(e.d==="learned_bug")a.appendChild(chkRow([it.id],f.negReason,f.id,f.statement));c.appendChild(a)})}
function famRow(f){const dd=famD(f),s=st[f.id],perAction=f.items.length>1&&!s.all;
 const r=el("div","rowc"+(openRows.has(f.id)?" open":"")),h=el("div","rowh");
 const dot=el("span","dot "+(DOT[f.tier]||"thin"));dot.title=TIER[f.tier].label+": "+TIER[f.tier].help;
 const t=el("div","rowt");t.appendChild(el("p","st",f.statement));
 const bits=[f.template];if(f.rules>1)bits.push(f.rules+" actions");bits.push("weakest support "+f.minSupport+"%");
 if(changed(f))bits.push("evidence changed");else if(f.prev)bits.push("decided earlier");
 if(dd==="mixed")bits.push("mixed");if(pin.has(f.id)&&!famMatch(f,F(),false))bits.push("decided just now");
 t.appendChild(el("small",null,bits.join(" · ")));
 const d=perAction?el("span","mute","decide per action (open the row)"):decButtons(dd,k=>pickFam(f,k));
 d.addEventListener("click",e=>e.stopPropagation());
 h.appendChild(dot);h.appendChild(t);h.appendChild(d);r.appendChild(h);
 const fill=()=>{const b=el("div","more");famParts(f,b,null);r.appendChild(b)};
 if(openRows.has(f.id))fill();
 h.onclick=()=>{if(openRows.has(f.id)){openRows.delete(f.id);r.classList.remove("open");const b=r.querySelector(".more");if(b)b.remove()}else{openRows.add(f.id);r.classList.add("open");fill()}};
 return r}
function renderBrowse(){const list=$("#list");list.textContent="";const o=F();
 const shownF=DATA.families.filter(f=>famMatch(f,o,true)).sort(sorters[$("#fsort").value]);
 const baseF=DATA.families.filter(f=>famMatch(f,Object.assign({},o,{dec:""}),false));
 const secs={};const sec=k=>secs[k]||(secs[k]={base:[],shown:[]});
 baseF.forEach(f=>sec(famSec(f)).base.push(f));shownF.forEach(f=>sec(famSec(f)).shown.push(f));
 const names=Object.keys(secs).filter(n=>secs[n].shown.length).sort((a,b)=>a==="Several actions"?1:b==="Several actions"?-1:a.localeCompare(b));
 if(openSec===undefined||(openSec&&!names.includes(openSec)))openSec=names.find(n=>secs[n].shown.length)||"";
 names.forEach(n=>{const S=secs[n],need=S.base.filter(needs).length,total=S.base.length,fin=!S.shown.length;
  const box=el("div","sec"+(n===openSec&&!fin?" open":"")+(fin?" finished":"")),h=el("div","sech");
  h.appendChild(el("span","arrow"));h.appendChild(el("span","sname",n));
  const cnt=el("span","cnt");if(need)cnt.appendChild(el("span","need",need+" need a decision"));
  if(total-need)cnt.appendChild(el("span","done",(need?total-need+" decided":"✓ all "+total+" decided")));cnt.appendChild(el("span",null,total+" famil"+(total>1?"ies":"y")));
  h.appendChild(cnt);if(!fin)h.onclick=()=>{openSec=openSec===n?"":n;render()};box.appendChild(h);
  if(n===openSec&&!fin){const rows=el("div","rows");S.shown.forEach(f=>rows.appendChild(famRow(f)));box.appendChild(rows)}
  list.appendChild(box)});
 if(!names.length)list.appendChild(el("p","empty","No family matches the filters. Change the confidence tier or the decision filter."))}
const queueList=()=>{const o=Object.assign({},F(),{dec:"needs"});return DATA.families.filter(f=>famMatch(f,o,false)||f.id===cur).sort(sorters[$("#fsort").value])};
function goReview(d){const L=queueList(),i=L.findIndex(f=>f.id===cur),n=L[i+d];if(n){cur=n.id;render()}}
function decideReview(k){const f=DATA.families.find(x=>x.id===cur);if(!f)return;const s=st[f.id];if(f.items.length>1&&!s.all)return;
 const L=queueList(),i=L.findIndex(x=>x.id===cur),nxt=L[i+1],dd=famD(f);
 f.ids.forEach(id=>setR(id,{d:dd===k?"":k}));pin.add(f.id);
 if(k!=="learned_bug"&&dd!==k&&nxt)cur=nxt.id;if(k==="learned_bug"&&dd!==k)openRows.add(f.id);redraw()}
function renderReview(){const box=$("#review");box.textContent="";let L=queueList();
 if(!cur||!L.some(f=>f.id===cur))cur=(L.find(needs)||L[0]||{}).id||null;
 if(!cur){box.appendChild(el("p","empty","Nothing left to decide with the current filters."));return}
 L=queueList();const f=DATA.families.find(x=>x.id===cur),i=L.findIndex(x=>x.id===cur),left=L.filter(needs).length;
 const big=el("div","big");big.appendChild(el("p","hint",famSec(f)+" · "+(i+1)+" of "+L.length+" in this queue · "+left+" still need a decision"));
 big.appendChild(el("p","st",f.statement));
 const dd=famD(f),KEYS={accepted:"A",rejected:"R",trivial:"T",learned_bug:"L"};
 const dec=decButtons(dd,k=>decideReview(k));dec.classList.add("bigdec");[...dec.children].forEach(b=>b.appendChild(el("span","kbd",KEYS[b.dataset.d])));
 famParts(f,big,dec);
 const nav=el("div","nav");const pv=el("button",null,"← Previous");pv.onclick=()=>goReview(-1);const nx=el("button",null,"Next →");nx.onclick=()=>goReview(1);
 nav.appendChild(pv);nav.appendChild(el("span","sp"));nav.appendChild(el("span",null,"Keys: A accepted · R rejected · T trivial · L learned bug · ← → move"));nav.appendChild(nx);big.appendChild(nav);box.appendChild(big)}
document.addEventListener("keydown",e=>{if(mode!=="review"||e.ctrlKey||e.metaKey||e.altKey)return;const t=e.target&&e.target.tagName;if(t==="INPUT"||t==="TEXTAREA"||t==="SELECT")return;
 const k={a:"accepted",r:"rejected",t:"trivial",l:"learned_bug"}[e.key.toLowerCase()];if(k){e.preventDefault();decideReview(k)}
 else if(e.key==="ArrowRight")goReview(1);else if(e.key==="ArrowLeft")goReview(-1)});
function setMode(m){mode=m;$("#tb").classList.toggle("on",m==="browse");$("#tr").classList.toggle("on",m==="review");render()}
$("#tb").onclick=()=>setMode("browse");$("#tr").onclick=()=>setMode("review");
function render(){renderManual();$("#bpane").hidden=mode!=="browse";$("#review").hidden=mode!=="review";
 if(mode==="browse"){renderBrowse();renderRules()}else renderReview();updateCounts()}
["#q","#ftier","#fdec","#ftpl","#fact","#fsort"].forEach(s=>$(s).addEventListener("input",()=>{pin.clear();pinR.clear();render()}));
$("#rshow").onchange=()=>{try{localStorage.setItem(KEY+":showrules",$("#rshow").checked?"1":"0")}catch(e){}render()};
function put(out,id,e,tier,ms,keep){if(!e.d){delete out[id];return}
 const o=Object.assign({},out[id]||{},{status:e.d});
 if(keep){if(!o.tier&&tier)o.tier=tier;if(o.minSupport==null&&ms!=null)o.minSupport=ms}else{o.tier=tier;o.minSupport=ms}
 if(e.bug&&e.d==="learned_bug")o.bug=e.bug;else delete o.bug;if(e.chk&&e.d==="learned_bug")o.check=true;else delete o.check;
 const lg=e.chk&&e.d==="learned_bug";if(lg&&e.bn)o.bugName=e.bn;else delete o.bugName;if(lg&&e.bg)o.bugGroup=e.bg;else delete o.bugGroup;if(lg&&e.bw)o.bugWhere=e.bw;else delete o.bugWhere;if(lg&&e.bs)o.bugSummary=e.bs;else delete o.bugSummary;if(lg&&e.bc)o.bugNotes=e.bc;else delete o.bugNotes;
 if(e.note)o.note=e.note;else delete o.note;out[id]=o}
(DATA.groups||[]).forEach(g=>{const o=document.createElement("option");o.value=g;$("#groups").appendChild(o)});
const inFam=new Set(DATA.families.flatMap(f=>f.ids));
$("#dl").onclick=()=>{const miss=[...new Set([...DATA.families.flatMap(f=>f.ids),...Object.keys(RS)])].filter(id=>{const e=effOf(id);return e.d==="learned_bug"&&e.chk&&(!/^[A-Za-z0-9_.-]+$/.test((e.bug||"").trim())||!e.bw)});
 let msg=$("#dlerr");if(!msg){msg=el("span");msg.id="dlerr";msg.style.cssText="color:var(--no);font-weight:700";$("#dl").insertAdjacentElement("afterend",msg)}
 if(miss.length){msg.textContent="Not downloaded: "+miss.length+" learned bug(s) with an invariant need a Bug ID (like BUG-08, no spaces) and Where.";return}msg.textContent="";
 const out=JSON.parse(JSON.stringify(BASE));
 DATA.families.forEach(f=>f.ids.forEach(id=>put(out,id,effOf(id),f.tier,f.minSupport)));
 DATA.rules.forEach(r=>{if(!inFam.has(r.id)&&RS[r.id])put(out,r.id,effOf(r.id),r.tier,r.minSupport,true)});
 const text=JSON.stringify(out,null,2),name="rule_review_"+STAMP()+".json";
 const dl=()=>{const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([text],{type:"application/json"}));a.download=name;a.click()};
 dl()};
let openSecR;const openRowsR=new Set();
function ruleRow(r,o){const e=effOf(r.id),row=el("div","rowc"+(openRowsR.has(r.id)?" open":"")),h=el("div","rowh");
 const dot=el("span","dot "+(DOT[r.tier]??"thin"));if(TIER[r.tier])dot.title=TIER[r.tier].label+": "+TIER[r.tier].help;
 const t=el("div","rowt");t.appendChild(el("p","st",r.statement||"(not in the latest inference run)"));
 const bits=[r.id];if(r.minSupport!=null)bits.push("weakest support "+r.minSupport+"%");if(chg(r.id))bits.push("changed: was "+r.status.replace("_"," "));
 if(pinR.has(r.id)&&!ruleMatch(r,o,false))bits.push("decided just now");t.appendChild(el("small",null,bits.join(" · ")));
 const d=decButtons(e.d,k=>{setR(r.id,{d:e.d===k?"":k});pinR.add(r.id);save();render()});d.addEventListener("click",ev=>ev.stopPropagation());
 h.appendChild(dot);h.appendChild(t);h.appendChild(d);row.appendChild(h);
 const fill=()=>{const b=el("div","more"),tags=el("div","tags");tags.appendChild(el("span","tag id",r.id));if(r.action)tags.appendChild(el("span","tag",r.action));
  if(TIER[r.tier])tags.appendChild(el("span","tag t-"+r.tier,TIER[r.tier].label));b.appendChild(tags);
  const note=el("input","note");note.placeholder="Note";note.value=effOf(r.id).note||"";note.oninput=()=>{setR(r.id,{note:note.value});save();updateCounts()};b.appendChild(note);row.appendChild(b)};
 if(openRowsR.has(r.id))fill();
 h.onclick=()=>{if(openRowsR.has(r.id)){openRowsR.delete(r.id);row.classList.remove("open");const b=row.querySelector(".more");if(b)b.remove()}else{openRowsR.add(r.id);row.classList.add("open");fill()}};return row}
function renderRules(){const list=$("#rlist"),show=$("#rshow").checked;list.textContent="";$("#rnote").hidden=!show;if(!show)return;
 const o=F(),secs={};
 DATA.rules.filter(r=>ruleMatch(r,o,true)).sort(rsorters[$("#fsort").value]||rname).forEach(r=>{const k=r.action?(actBase(r.action)==="*"?"All actions":actBase(r.action)):"Not in the latest run";(secs[k]=secs[k]||[]).push(r)});
 const names=Object.keys(secs).sort((a,b)=>a.localeCompare(b));
 if(names.length&&(openSecR===undefined||(openSecR&&!names.includes(openSecR))))openSecR=names[0];
 names.forEach(n=>{const rs=secs[n],box=el("div","sec"+(n===openSecR?" open":"")),h=el("div","sech");
  h.appendChild(el("span","arrow"));h.appendChild(el("span","sname",n));const cnt=el("span","cnt");
  const nd=rs.filter(r=>effOf(r.id).d).length;cnt.appendChild(el("span",null,rs.length+" rule"+(rs.length>1?"s":"")));cnt.appendChild(el("span","done",nd+" with a decision"));h.appendChild(cnt);
  h.onclick=()=>{openSecR=openSecR===n?"":n;render()};box.appendChild(h);
  if(n===openSecR){const rows=el("div","rows");rs.forEach(r=>rows.appendChild(ruleRow(r,o)));box.appendChild(rows)}list.appendChild(box)});
 if(!names.length)list.appendChild(el("p","empty","No saved rule matches the filters."))}
$("#rreset").onclick=()=>{if(!Object.keys(RS).length)return;RS={};save();pin.clear();pinR.clear();render()};
render();
</script></body></html>"""


def _dataset_support(rules: list[dict], datasets: list[str], min_obs: int) -> list[dict]:
    """Per dataset: the weakest support across the family's rules, the smallest number of observations, and whether it is thin."""
    out = []
    for name in datasets:
        seen = [c["datasets"][name] for c in rules if name in c["datasets"]]
        n = min((v["m"] for v in seen), default=0) if len(seen) == len(rules) else 0
        out.append({"name": name, "n": n, "thin": n < min_obs,
                    "pct": round(min((v["pct"] for v in seen), default=0.0), 4)})
    return out


def _known_groups() -> list[str]:
    """Group names already used in the known bugs list (suggestions for the learned-bug form)."""
    try:
        from tests.mutation_testing.reports.known_bugs_tool import read_lists
        return sorted({b["group"] for items in read_lists().values() for b in items if b.get("group")})
    except Exception:
        return []


def html_page(run_dir: Path, review_dir: str, out_path: str = REVIEW_HTML) -> Path:
    review = load_review(review_dir)
    payload = _payload(run_dir)
    names = [d["name"] for d in payload.get("datasets", [])]
    min_obs = payload.get("min_obs", 1)
    families = []
    for fam, rules in sorted(_families(run_dir).items(), key=lambda kv: (kv[1][0]["template"], kv[1][0]["statement"])):
        ids = [c["rule"]["id"] for c in rules]
        statuses = {review.get(i, {}).get("status") for i in ids}
        decided_earlier = bool(statuses - {None})
        first = review.get(ids[0], {})
        observed = [v["pct"] for c in rules for v in c["datasets"].values() if v["m"]]
        families.append({
            "id": fam, "ids": ids, "template": rules[0]["template"], "statement": rules[0]["statement"],
            "tier": rules[0]["group"], "rules": len(rules), "actions": sorted(c["category"] for c in rules),
            "minSupport": round(100 * min(observed, default=0.0)),
            "datasets": _dataset_support(rules, names, min_obs),
            "hand": sorted({m for c in rules for m in c.get("manual", [])}),
            "fails": _examples(rules, "examples"), "holds": _examples(rules, "support"),
            "decision": (statuses.pop() or "") if len(statuses) == 1 else "", "prev": decided_earlier,
            "prevTier": first.get("tier", ""), "bug": first.get("bug", ""), "note": first.get("note", ""),
            "negReason": next((r for r in (negation_unsupported_reason(c["rule"]) for c in rules) if r), ""),
            "items": sorted(({"id": c["rule"]["id"], "action": c["category"]} for c in rules), key=lambda x: x["action"]),
        })
    by_id = {c["rule"]["id"]: c for c in payload["candidates"]}
    rules = []   # every rule saved in the newest review file, for the "Accepted rules" panel (also ids the latest run no longer has)
    for rid, entry in sorted(review.items()):
        c = by_id.get(rid)
        seen = [v["pct"] for v in c["datasets"].values() if v["m"]] if c else []
        rules.append({
            "id": rid, "status": entry.get("status", ""), "note": entry.get("note", ""),
            "statement": c["statement"] if c else "", "action": c["category"] if c else "",
            "template": c["template"] if c else "",
            "tier": c["group"] if c else entry.get("tier", ""),
            "minSupport": round(100 * min(seen, default=0.0)) if c else entry.get("minSupport"),
        })
    data = json.dumps({
        "run": str(run_dir), "families": families, "rules": rules, "threshold": payload.get("threshold", 0.0),
        "tiers": [{"key": k, "label": v, "help": TIER_HELP[k]} for k, v in TIERS.items()],
        "manual": payload.get("manual_coverage"), "groups": _known_groups(),
    }, ensure_ascii=False).replace("</", "<\\/")
    base = json.dumps(review, ensure_ascii=False).replace("</", "<\\/")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE.replace("__VARS__", DARK_VARS).replace("__DATA__", data).replace("__BASE__", base), encoding="utf-8")
    by_tier = {t: sum(1 for f in families if f["tier"] == t) for t in TIERS}
    print(f"[Review] {len(families)} families ({sum(f['rules'] for f in families)} rules): "
          + ", ".join(f"{TIERS[t]} {n}" for t, n in by_tier.items() if n) + f" -> {out}")
    print(f"[Review] open in browser: {out.resolve().as_uri()}")
    print("[Review] when done: Download review file, then (python -m tests.cli review) press Enter in the terminal")
    return out



def import_json(file: str, review_dir: str) -> None:
    new = json.loads(Path(file).read_text(encoding="utf-8"))
    bad = [k for k, v in new.items() if not isinstance(v, dict) or v.get("status") not in DECISIONS]
    if bad:
        raise SystemExit(f"{file}: {len(bad)} entr(y/ies) without a valid status, e.g. {bad[:3]}")
    dest, copied = install_review(file, review_dir)
    counts: dict[str, int] = defaultdict(int)
    for v in new.values():
        counts[v["status"]] += 1
    summary = ", ".join(f"{k}={n}" for k, n in sorted(counts.items()))
    if copied:
        print(f"[Review] {len(new)} rule decision(s) from {file} installed as {dest}: {summary}")
    else:
        print(f"[Review] {dest} is already in {review_dir}; nothing to copy ({len(new)} rule decision(s): {summary})")
    from tests.mutation_testing.inference.rule_history import write_history
    print(f"[Review] rule history updated: {write_history(review_dir)}")


ENGINE_MODULE = "tests.mutation_testing.inference.invariant_inference_engine"


def _new_download(since: float, review_dir: str) -> Path | None:
    """Newest rule_review_*.json in the inbox (tests/output/inbox), else in Downloads, that is new (modified at or after
    `since`) and whose name is not in the review folder yet (so it was not imported before)."""
    done = {f.name for f in review_files(review_dir)}
    for folder in (INBOX_DIR, Path(default_download_dir())):
        files = [f for f in review_files(str(folder)) if f.stat().st_mtime >= since and f.name not in done]
        if files:
            return max(files, key=lambda f: f.stat().st_mtime)
    return None


def session(run_dir: Path, review_dir: str) -> None:
    """Open the review page; on Enter: move the downloaded decisions into the review folder and rewrite the checked rules
    (infer --export-only: accepted rules, learned-bug checks, bugs logged in the known bugs list). The next loop run uses them."""
    page = html_page(run_dir, review_dir)
    webbrowser.open(page.resolve().as_uri())
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Not a terminal: download the review on the page, then run: python -m tests.cli review import  and  python -m tests.cli infer --export-only")
        return
    since = time.time() - 2
    while True:
        if input("\nReview the rules, click Download review file on the page, then press Enter here (q = finish): ").strip().lower() in ("q", "quit"):
            return
        file = _new_download(since, review_dir)
        if file is None:
            print(f"No new rule_review_*.json in {INBOX_DIR} or {default_download_dir()}. Click Download review file on the page first.")
            continue
        print(f"[Review] using {file}")
        import_json(str(file), review_dir)
        if file.resolve().parent != Path(review_dir).resolve():
            try:
                file.unlink()   # the copy in the review folder is the one in use: the download is moved, not copied
            except OSError as exc:
                print(f"[Review] could not remove {file}: {exc}")
        print("[Review] rewriting the checked rules ...")
        if subprocess.run([sys.executable, "-m", ENGINE_MODULE, "--export-only"]).returncode != 0:
            print("[Review] the export failed (see above); fix it and press Enter again, or run: python -m tests.cli infer --export-only")
            continue
        html_page(run_dir, review_dir)
        print("[Review] done. The next loop run checks these rules. Press F5 on the page to see the saved state.")
        since = time.time() - 2


def newest_download(downloads: str, review_dir: str = REVIEW_DIR) -> str | None:
    """The newest rule_review_*.json in the downloads folder, if it is newer than the active file in the review folder.
    None when the review folder already has the newest decisions (a file saved there directly needs no import)."""
    active = latest_review_file(review_dir)
    found = latest_review_file(downloads)
    if found is not None and (active is None or found.stat().st_mtime > active.stat().st_mtime):
        print(f"[Review] newest download: {found}")
        return str(found)
    if active is not None:
        print(f"[Review] nothing newer in {downloads}; the active review file is already in the review folder: {active}")
        return None
    raise SystemExit(f"[Review] no rule_review_*.json in {review_dir} or {downloads}. Download it from the review page "
                     f"(save it in {review_dir}), or pass --json FILE (or --downloads DIR if your browser saves elsewhere).")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Review promoted rules by family", formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="without an action: open the review page; click Download review file there, press Enter here, and the review is\n"
               "moved to the review folder and applied (checked rules, learned-bug checks, bugs logged).\n\n"
               "actions:\n"
               "  html          build the review page from the newest inference run and print its link\n"
               "  import        install the rule_review_*.json that the page downloaded (from Downloads, or --json FILE); not needed if you saved it in the review folder\n"
               "  export        write a CSV sheet of the rule families to fill in (decision, bug, note)\n"
               "  import-sheet  read the filled-in CSV sheet back as decisions\n"
               "  history       when each rule was first accepted and how its review state changed (history [RULE_ID])\n\n"
               "After import / import-sheet: python -m tests.cli infer --export-only")
    ap.add_argument("action", choices=("html", "export", "import", "import-sheet", "history"), nargs="?", default=None,
                    help="what to do (see the list below); without one: open the page and apply your review when you press Enter")
    ap.add_argument("rule_id", nargs="?", default=None, help="history: show the full log of this rule (default: list all accepted rules)")
    ap.add_argument("--json", default=None, help="import: a specific review file (default: the newest rule_review_*.json in Downloads, if it is newer than the one in the review folder)")
    ap.add_argument("--downloads", default=default_download_dir(), help="import: where the browser saves downloads (default: %(default)s)")
    ap.add_argument("--run-dir", default=None, help="Engine run folder (default: newest in tests/output/reports/inference/runs)")
    ap.add_argument("--review-dir", default=REVIEW_DIR, help="Folder with the review files (default: %(default)s)")
    args = ap.parse_args()
    if args.action == "import":
        file = args.json or newest_download(args.downloads, args.review_dir)
        if file:
            import_json(file, args.review_dir)
        return
    if args.action == "history":
        from tests.mutation_testing.inference.rule_history import show
        show(args.rule_id, args.review_dir)
        return
    run_dir = Path(args.run_dir) if args.run_dir else _latest(RUNS_DIR)
    if args.action is None:
        session(run_dir, args.review_dir)
        return
    {"html": html_page, "export": export_sheet, "import-sheet": import_sheet}[args.action](run_dir, args.review_dir)


if __name__ == "__main__":
    main()