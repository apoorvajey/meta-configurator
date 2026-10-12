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
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.common.report_theme import DARK_VARS
from tests.mutation_testing.inference.review_store import (REVIEW_DIR, default_download_dir, install_review, latest_review_file,
                                                 load_review, save_review)

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


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Rule Review</title><style>
:root{__VARS__}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
.bar{max-width:1000px;margin:0 auto;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:18px;margin:0;flex:1 1 auto}input,select,button,textarea{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
button{cursor:pointer}button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.prog{max-width:1000px;margin:8px auto 0;height:6px;background:var(--chip);border-radius:6px;overflow:hidden}.prog div{height:100%;background:var(--accent);width:0}
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
.row{display:flex;flex-wrap:wrap;gap:8px}.row input{flex:1 1 140px}.row input.note{flex:3 1 260px}
details{margin:6px 0;color:var(--mute);font-size:13.5px}summary{cursor:pointer}code{font-size:12.5px;word-break:break-all}
details.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:0 0 14px;color:var(--ink);font-size:14px}
details.panel summary{font-weight:600}.panel p{color:var(--mute);font-size:13px;margin:8px 0}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:600}
.s-recovered{color:var(--good)}.s-partial,.s-seen{color:var(--warn)}.s-missing{color:var(--bad)}.s-no_evidence,.s-not_expressible{color:var(--mute)}
.empty{color:var(--mute);text-align:center;padding:40px}
.allrow{display:flex;align-items:center;gap:8px;margin:8px 0;cursor:pointer;font-size:14px}input[type=checkbox]{padding:0;width:16px;height:16px;accent-color:var(--accent)}
.act{border-top:1px solid var(--line);padding:8px 0}.act .nm{font-weight:600;font-size:13.5px;margin:0 0 4px;word-break:break-all}.act .dec{margin:4px 0}
details.panel .card{background:var(--bg)}details.panel .row{margin:8px 0}.tag.id{font-family:ui-monospace,Consolas,monospace}
</style></head><body>
<header><div class=bar><h1>Rule review <span id=count class=tag></span></h1>
<button class=primary id=dl>Download review file</button></div>
<div class=bar style="margin-top:8px"><select id=ftier title="Confidence tier"></select>
<select id=fdec title="Decision"><option value=needs>Needs decision</option><option value="">All decisions</option><option value=_prev>Decided earlier</option><option>accepted</option><option>rejected</option><option>trivial</option><option>learned_bug</option></select>
<select id=ftpl><option value="">All templates</option></select>
<select id=fsort title="Order"><option value=name>Order: template</option><option value=low>Order: weakest support first</option><option value=high>Order: strongest support first</option><option value=rules>Order: most rules first</option></select>
<input id=q type=search placeholder="Search statements and actions"></div>
<div class=prog><div id=pb></div></div></header>
<main><p class=hint>One card per rule family: same statement and tier, different actions. Untick <b>Same decision for all</b> to decide per action.
<b>accepted</b> = checked in the loop · <b>rejected</b> = not a real invariant · <b>trivial</b> = true but obvious · <b>learned bug</b> = frequent but unexpected.
Undecided actions are not exported. Choices stay in this browser. When done, download the file to
<code>tests/mutation_testing/config/rule_reviews/</code> (or just run <code>python -m tests.cli review import</code>: it picks up the newest download). The newest file in that folder is the one in use.</p>
<div id=manual></div><div id=list></div>
<details class=panel id=rpanel><summary id=rsum></summary>
<p>Every rule saved in the newest review file, one by one. Same download button.</p>
<div class=row><select id=rfilt><option value=acc>Accepted rules</option><option value=chg>Changed here</option><option value=all>All saved rules</option></select>
<input id=rq type=search placeholder="Search id, statement, action"><button id=rreset>Discard my changes</button></div>
<div id=rlist></div></details></main>
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
const effOf=id=>RS[id]||{d:baseOf(id).status||"",note:baseOf(id).note||"",bug:baseOf(id).bug||""};
const chg=id=>!!RS[id]&&(RS[id].d!==(baseOf(id).status||"")||(RS[id].note||"")!==(baseOf(id).note||"")||(RS[id].bug||"")!==(baseOf(id).bug||""));
const setR=(id,p)=>{RS[id]=Object.assign({},effOf(id),p);if(!chg(id))delete RS[id]};
const famD=f=>{const u=new Set(f.ids.map(id=>effOf(id).d));return u.size===1?[...u][0]:"mixed"};
const needs=f=>f.ids.some(id=>!effOf(id).d)||changed(f);
const common=(f,k)=>{const u=new Set(f.ids.map(id=>effOf(id)[k]||""));return u.size===1?[...u][0]:""};
DATA.families.forEach(f=>{const s=st[f.id]||(st[f.id]={});
 if(!s.m){s.m=1;if(s.d!==undefined&&s.d!==(f.decision||""))f.ids.forEach(id=>{if(!RS[id])RS[id]={d:s.d,note:s.note||"",bug:s.bug||""}})}   // decisions made on the older page
 if(s.all===undefined)s.all=new Set(f.ids.map(id=>baseOf(id).status||"")).size===1});
Object.keys(RS).forEach(id=>{if(!chg(id))delete RS[id]});
(function initFilters(){const ft=$("#ftier");
 const all=el("option",null,"All tiers ("+DATA.families.length+")");all.value="";ft.appendChild(all);
 DATA.tiers.forEach(t=>{const n=DATA.families.filter(f=>f.tier===t.key).length;if(!n)return;const o=el("option",null,t.label+" ("+n+")");o.value=t.key;o.title=t.help;ft.appendChild(o)});
 ft.value=DATA.families.some(f=>f.tier==="holds")?"holds":"";
 [...new Set(DATA.families.map(f=>f.template))].sort().forEach(t=>{const o=el("option",null,t);o.value=t;$("#ftpl").appendChild(o)})})();
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
function matches(f){const q=$("#q").value.toLowerCase(),ft=$("#ftier").value,fd=$("#fdec").value,tp=$("#ftpl").value;
 if(ft&&f.tier!==ft)return false;if(tp&&f.template!==tp)return false;
 if(fd==="needs"){if(!needs(f))return false}else if(fd==="_prev"){if(!f.prev)return false}else if(fd&&!f.ids.some(id=>effOf(id).d===fd))return false;
 if(q&&!(f.statement+" "+f.actions.join(" ")+" "+f.hand.join(" ")).toLowerCase().includes(q))return false;return true}
const sorters={name:(a,b)=>a.template.localeCompare(b.template)||a.statement.localeCompare(b.statement),
 low:(a,b)=>a.minSupport-b.minSupport||sorters.name(a,b),high:(a,b)=>b.minSupport-a.minSupport||sorters.name(a,b),
 rules:(a,b)=>b.rules-a.rules||sorters.name(a,b)};
const redraw=()=>{save();render();renderRules()};
function decButtons(cur,onPick){const dec=el("div","dec");DEC.forEach(k=>{const b=el("button",cur===k?"on":"",k.replace("_"," "));b.dataset.d=k;b.onclick=()=>onPick(k);dec.appendChild(b)});return dec}
function render(){renderManual();const list=$("#list");list.textContent="";let shown=0;
 DATA.families.filter(matches).sort(sorters[$("#fsort").value]).forEach(f=>{const s=st[f.id];shown++;const dd=famD(f);const multi=f.items.length>1,perAction=multi&&!s.all;
  const c=el("div","card"+(DEC.includes(dd)?" "+dd:""));c.appendChild(el("p","st",f.statement));
  const tags=el("div","tags");const tl=TIER[f.tier];
  const tt=el("span","tag t-"+f.tier,tl.label);tt.title=tl.help;tags.appendChild(tt);
  tags.appendChild(el("span","tag",f.template));tags.appendChild(el("span","tag",f.rules+" action"+(f.rules>1?"s":"")));
  f.hand.forEach(h=>{const x=el("span","tag hand","also hand-written: "+h);x.title="A hand-written invariant says the same";tags.appendChild(x)});
  if(changed(f))tags.appendChild(el("span","tag prev","evidence changed: was "+(TIER[f.prevTier]||{label:f.prevTier}).label+", now "+tl.label));
  else if(f.prev)tags.appendChild(el("span","tag prev","decided earlier"));
  if(dd==="mixed"){const n={};f.ids.forEach(id=>{const k=(effOf(id).d||"undecided").replace("_"," ");n[k]=(n[k]||0)+1});
   tags.appendChild(el("span","tag prev","mixed: "+Object.keys(n).map(k=>n[k]+" "+k).join(", ")))}
  const sup=el("span","tag");const m=el("span","meter");const i=el("i");i.style.width=f.minSupport+"%";m.appendChild(i);sup.appendChild(m);
  sup.appendChild(document.createTextNode(" weakest support "+f.minSupport+"%"));tags.appendChild(sup);c.appendChild(tags);
  const ds=el("div","tags");f.datasets.forEach(d=>{let txt,cls="tag";
   if(d.n===0){txt=d.name+": no data";cls+=" thin"}else if(d.thin){txt=d.name+": "+pct(d.pct)+" of only "+d.n;cls+=" thin"}
   else{txt=d.name+": "+pct(d.pct)+" · n="+d.n;if(d.pct<DATA.threshold)cls+=" low"}
   ds.appendChild(el("span",cls,txt))});c.appendChild(ds);
  if(f.tier!=="holds")c.appendChild(el("p","note","Accepting this rule promotes it by hand: it is checked in the loop although it did not hold in every dataset at "+pct(DATA.threshold)+"."));
  const showEx=(title,items)=>{if(!items||!items.length)return;const x=el("details");x.appendChild(el("summary",null,title+" ("+items.length+")"));
   items.forEach(e=>{const li=el("div","ex");li.appendChild(el("span",null,e.ds+" · run "+e.run+" · step "+e.step+" · "+e.action+" · "));
    if(e.link){const a=el("a",null,"open step");a.href=e.link;a.target="_blank";li.appendChild(a)}else li.appendChild(el("span","mute","log not found"));
    li.appendChild(el("br"));li.appendChild(el("code",null,e.detail));x.appendChild(li)});c.appendChild(x)};
  if(!perAction){const d=el("details");d.appendChild(el("summary",null,"Actions ("+f.actions.length+")"));d.appendChild(el("div",null,f.actions.join(", ")));c.appendChild(d)}
  showEx("Steps where it did not hold",f.fails);showEx("Steps where it held",f.holds);
  if(multi){const lb=el("label","allrow");const cb=el("input");cb.type="checkbox";cb.checked=!!s.all;
   cb.onchange=()=>{s.all=cb.checked;save();render()};lb.appendChild(cb);lb.appendChild(document.createTextNode("Same decision for all "+f.items.length+" actions"));c.appendChild(lb)}
  if(!perAction){
   c.appendChild(decButtons(dd,k=>{f.ids.forEach(id=>setR(id,{d:dd===k?"":k}));redraw()}));
   const row=el("div","row");const bug=el("input");bug.placeholder="Bug id, e.g. BUG-02";bug.value=common(f,"bug");bug.oninput=()=>{f.ids.forEach(id=>setR(id,{bug:bug.value}));save();renderRules()};
   const note=el("input","note");note.placeholder=multi?"Note (all actions)":"Note";note.value=common(f,"note");note.oninput=()=>{f.ids.forEach(id=>setR(id,{note:note.value}));save();renderRules()};
   row.appendChild(bug);row.appendChild(note);c.appendChild(row)
  }else f.items.forEach(it=>{const e=effOf(it.id);const a=el("div","act");a.appendChild(el("p","nm",it.action));
   a.appendChild(decButtons(e.d,k=>{setR(it.id,{d:e.d===k?"":k});redraw()}));
   const note=el("input","note");note.placeholder="Note";note.value=e.note||"";note.oninput=()=>{setR(it.id,{note:note.value});save();renderRules()};a.appendChild(note);c.appendChild(a)});
  list.appendChild(c)});
 if(!shown)list.appendChild(el("p","empty","No family matches the filters. Change the confidence tier or the decision filter."));
 const ids=DATA.families.flatMap(f=>f.ids),done=ids.filter(id=>effOf(id).d).length;$("#count").textContent=done+" / "+ids.length+" actions decided";$("#pb").style.width=(100*done/Math.max(1,ids.length))+"%"}
["#q","#ftier","#fdec","#ftpl","#fsort"].forEach(s=>$(s).addEventListener("input",render));
function put(out,id,e,tier,ms,keep){if(!e.d){delete out[id];return}
 const o=Object.assign({},out[id]||{},{status:e.d});
 if(keep){if(!o.tier&&tier)o.tier=tier;if(o.minSupport==null&&ms!=null)o.minSupport=ms}else{o.tier=tier;o.minSupport=ms}
 if(e.bug)o.bug=e.bug;else delete o.bug;if(e.note)o.note=e.note;else delete o.note;out[id]=o}
const inFam=new Set(DATA.families.flatMap(f=>f.ids));
$("#dl").onclick=()=>{const out=JSON.parse(JSON.stringify(BASE));
 DATA.families.forEach(f=>f.ids.forEach(id=>put(out,id,effOf(id),f.tier,f.minSupport)));
 DATA.rules.forEach(r=>{if(!inFam.has(r.id)&&RS[r.id])put(out,r.id,effOf(r.id),r.tier,r.minSupport,true)});
 const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:"application/json"}));a.download="rule_review_"+STAMP()+".json";a.click()};
function rsum(){const nch=DATA.rules.filter(r=>chg(r.id)).length,nacc=DATA.rules.filter(r=>r.status==="accepted").length;
 $("#rsum").textContent="Accepted rules ("+nacc+")"+(nch?" · "+nch+" changed":"")}
function renderRules(){const f=$("#rfilt").value,q=$("#rq").value.toLowerCase(),list=$("#rlist");list.textContent="";rsum();let shown=0;
 DATA.rules.filter(r=>(f==="all"||(f==="chg"?chg(r.id):r.status==="accepted"))&&(!q||(r.id+" "+r.statement+" "+r.action).toLowerCase().includes(q))).forEach(r=>{
  shown++;const e=effOf(r.id);const c=el("div","card"+(e.d?" "+e.d:""));
  c.appendChild(el("p","st",r.statement||"(not in the latest inference run)"));
  const tags=el("div","tags");tags.appendChild(el("span","tag id",r.id));if(r.action)tags.appendChild(el("span","tag",r.action));
  const tl=TIER[r.tier];if(tl)tags.appendChild(el("span","tag t-"+r.tier,tl.label));
  if(r.minSupport!=null)tags.appendChild(el("span","tag","weakest support "+r.minSupport+"%"));
  if(chg(r.id))tags.appendChild(el("span","tag prev","changed: was "+r.status.replace("_"," ")));c.appendChild(tags);
  c.appendChild(decButtons(e.d,k=>{setR(r.id,{d:e.d===k?"":k});save();renderRules();render()}));
  const note=el("input","note");note.placeholder="Note";note.value=e.note||"";
  note.oninput=()=>{setR(r.id,{note:note.value});save();rsum()};c.appendChild(note);list.appendChild(c)});
 if(!shown)list.appendChild(el("p","empty",f==="chg"?"Nothing changed yet.":"No saved rule matches."))}
["#rfilt","#rq"].forEach(s=>$(s).addEventListener("input",renderRules));
$("#rreset").onclick=()=>{if(!Object.keys(RS).length)return;RS={};save();renderRules();render()};
render();renderRules();
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
            "tier": c["group"] if c else entry.get("tier", ""),
            "minSupport": round(100 * min(seen, default=0.0)) if c else entry.get("minSupport"),
        })
    data = json.dumps({
        "run": str(run_dir), "families": families, "rules": rules, "threshold": payload.get("threshold", 0.0),
        "tiers": [{"key": k, "label": v, "help": TIER_HELP[k]} for k, v in TIERS.items()],
        "manual": payload.get("manual_coverage"),
    }, ensure_ascii=False).replace("</", "<\\/")
    base = json.dumps(review, ensure_ascii=False).replace("</", "<\\/")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE.replace("__VARS__", DARK_VARS).replace("__DATA__", data).replace("__BASE__", base), encoding="utf-8")
    by_tier = {t: sum(1 for f in families if f["tier"] == t) for t in TIERS}
    print(f"[Review] {len(families)} families ({sum(f['rules'] for f in families)} rules): "
          + ", ".join(f"{TIERS[t]} {n}" for t, n in by_tier.items() if n) + f" -> {out}")
    print(f"[Review] open in browser: {out.resolve().as_uri()}")
    print("[Review] when done, use the download button and save the file into "
          f"{review_dir} (or just run: python -m tests.cli review import), then: python -m tests.cli infer --export-only")
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


def newest_download(downloads: str) -> str:
    """The newest rule_review_*.json in the downloads folder."""
    found = latest_review_file(downloads)
    if found is None:
        raise SystemExit(f"[Review] no rule_review_*.json in {downloads}. Download it from the review page, "
                         f"or pass --json FILE (or --downloads DIR if your browser saves elsewhere).")
    print(f"[Review] newest download: {found}")
    return str(found)


def main() -> None:
    ap = argparse.ArgumentParser(description="Review promoted rules by family")
    ap.add_argument("action", choices=("html", "export", "import", "import-sheet"))
    ap.add_argument("--json", default=None, help="import: a specific review file (default: the newest rule_review_*.json in the Downloads folder)")
    ap.add_argument("--downloads", default=default_download_dir(), help="import: where the browser saves downloads (default: %(default)s)")
    ap.add_argument("--run-dir", default=None, help="Engine run folder (default: newest in tests/output/reports/inference/runs)")
    ap.add_argument("--review-dir", default=REVIEW_DIR, help="Folder with the review files (default: %(default)s)")
    args = ap.parse_args()
    if args.action == "import":
        import_json(args.json or newest_download(args.downloads), args.review_dir)
        return
    run_dir = Path(args.run_dir) if args.run_dir else _latest(RUNS_DIR)
    {"html": html_page, "export": export_sheet, "import-sheet": import_sheet}[args.action](run_dir, args.review_dir)


if __name__ == "__main__":
    main()