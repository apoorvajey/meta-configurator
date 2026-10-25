"""Shared pieces of the stats reports: the common options, the run folders, and the report page.

Every stats report saves an HTML page in tests/output/reports/stats/
(<name>_<time>.html; earlier pages are kept). The page uses the look of the corpus report (same stylesheet and colours):
title, summary cards, sortable tables, notes.
"""
from __future__ import annotations

import argparse
import csv
import html
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from tests.mutation_testing.common.paths import STATS_DIR
from tests.mutation_testing.runs.run_layout import RUNS_DIR, iter_run_dirs
from tests.mutation_testing.traces.corpus_report import BUTTON_CSS, CSS   # the corpus report's stylesheet

EXTRA_CSS = ("tr.total td{font-weight:700;background:var(--chip)}td.warn{font-weight:600}"
             "p.note{margin:4px 0}p.note.good{color:var(--good)}p.note.bad{color:var(--bad)}p.note.warn{color:var(--warn)}"
             ".tabs{display:flex;gap:6px;margin:22px 0 4px;border-bottom:1px solid var(--line)}"
             ".tabs button{font:inherit;padding:8px 16px;border:0;border-bottom:2px solid transparent;background:transparent;"
             "color:var(--mute);cursor:pointer}.tabs button.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}"
             ".tabpane[hidden]{display:none}"
             "details.fold>summary{cursor:pointer;font-size:1.15em;font-weight:600;margin:22px 0 8px}"
             "tr.filters td{padding:2px 4px}"
             "button.ddb{width:100%;min-width:70px;text-align:left;font:inherit;font-size:12px;padding:2px 6px;border:1px solid var(--line);"
             "border-radius:6px;background:var(--chip);color:var(--ink);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
             "button.ddb.on{border-color:var(--accent);font-weight:600}"
             "div.ddp{position:fixed;z-index:50;width:240px;max-height:320px;display:flex;flex-direction:column;background:var(--panel);"
             "color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px;box-shadow:0 6px 20px #0006;font-size:13px}"
             "div.ddp[hidden]{display:none}div.ddp input[type=search]{width:100%;font:inherit;margin-bottom:4px;background:var(--bg);color:var(--ink);"
             "border:1px solid var(--line);border-radius:4px}div.ddp .acts{margin-bottom:4px}div.ddp .acts a{cursor:pointer;color:var(--accent);margin-right:10px}"
             "div.ddp .list{overflow:auto}div.ddp label{display:block;white-space:nowrap;padding:1px 0;cursor:pointer}")
# The corpus report's sorting, except that a row with class "total" stays at the bottom
SORT_JS = """
document.querySelectorAll('table.sortable').forEach(function(t){var hs=t.querySelectorAll('th');
hs.forEach(function(h,i){h.style.cursor='pointer';h.title='Click to sort';h.addEventListener('click',function(){
var asc=h.dataset.dir!=='asc';hs.forEach(function(x){x.dataset.dir='';x.classList.remove('asc','desc')});
h.dataset.dir=asc?'asc':'desc';h.classList.add(asc?'asc':'desc');
var all=[].slice.call(t.querySelectorAll('tr')).slice(1).filter(function(r){return !r.classList.contains('filters')}),rows=all.filter(function(r){return !r.classList.contains('total')}),
tot=all.filter(function(r){return r.classList.contains('total')});
var v=function(r){var c=r.children[i];return c?(c.dataset.v!==undefined?c.dataset.v:c.textContent.trim()):''};
rows.sort(function(a,b){var x=v(a),y=v(b),nx=parseFloat(x),ny=parseFloat(y);
var c=(!isNaN(nx)&&!isNaN(ny)&&/^-?[0-9.]+%?$/.test(x)&&/^-?[0-9.]+%?$/.test(y))?nx-ny:x.localeCompare(y);return asc?c:-c});
rows.concat(tot).forEach(function(r){t.appendChild(r)})})})});
document.querySelectorAll('tr.filters').forEach(function(f){var t=f.closest('table'),sel=[],panels=[],
rows=[].slice.call(t.querySelectorAll('tr')).filter(function(r){return !r.classList.contains('filters')&&!r.classList.contains('total')&&!r.querySelector('th')});
var apply=function(){rows.forEach(function(r){r.style.display=sel.every(function(s,i){return !s||s.has(r.children[i].textContent.trim())})?'':'none'})};
[].slice.call(f.children).forEach(function(td,i){var seen={},vals=[];
rows.forEach(function(r){var v=r.children[i].textContent.trim();if(!seen[v]){seen[v]=1;vals.push(v)}});
vals.sort(function(a,b){var x=parseFloat(a),y=parseFloat(b);return(!isNaN(x)&&!isNaN(y))?x-y:a.localeCompare(b)});
var btn=document.createElement('button');btn.type='button';btn.className='ddb';btn.textContent='all';
var box=document.createElement('div');box.className='ddp';box.hidden=true;panels.push(box);
var q=document.createElement('input');q.type='search';q.placeholder='search values';
var acts=document.createElement('div');acts.className='acts';
var all=document.createElement('a');all.textContent='all';var none=document.createElement('a');none.textContent='none';
acts.appendChild(all);acts.appendChild(none);
var list=document.createElement('div');list.className='list';
var checks=vals.map(function(v){var l=document.createElement('label'),c=document.createElement('input');c.type='checkbox';c.checked=true;
l.appendChild(c);l.appendChild(document.createTextNode(' '+(v===''?'(empty)':v)));list.appendChild(l);return{c:c,l:l,v:v}});
var update=function(){var on=checks.filter(function(x){return x.c.checked});
if(on.length===checks.length){sel[i]=null;btn.textContent='all';btn.classList.remove('on')}
else{sel[i]=new Set(on.map(function(x){return x.v}));btn.textContent=on.length===0?'none':on.length===1?(on[0].v||'(empty)'):on.length+' selected';btn.classList.add('on')}apply()};
checks.forEach(function(x){x.c.addEventListener('change',update)});
var show=function(){var s=q.value.trim().toLowerCase();checks.forEach(function(x){x.l.style.display=(!x.na&&x.v.toLowerCase().indexOf(s)>=0)?'':'none'})};
q.addEventListener('input',show);
var setAll=function(on){checks.forEach(function(x){if(x.l.style.display!=='none')x.c.checked=on});update()};
all.addEventListener('click',function(){setAll(true)});none.addEventListener('click',function(){setAll(false)});
btn.addEventListener('click',function(e){e.stopPropagation();var open=box.hidden;panels.forEach(function(p){p.hidden=true});
if(open){var av={};rows.forEach(function(r){if(sel.every(function(s,j){return j===i||!s||s.has(r.children[j].textContent.trim())}))av[r.children[i].textContent.trim()]=1});
checks.forEach(function(x){x.na=!av[x.v]});q.value='';show();var r=btn.getBoundingClientRect();box.style.top=Math.min(r.bottom+2,window.innerHeight-340)+'px';box.style.left=Math.min(r.left,window.innerWidth-250)+'px';box.hidden=false;q.focus()}});
box.addEventListener('click',function(e){e.stopPropagation()});
box.appendChild(q);box.appendChild(acts);box.appendChild(list);td.appendChild(btn);document.body.appendChild(box)});
document.addEventListener('click',function(){panels.forEach(function(p){p.hidden=true})});
window.addEventListener('scroll',function(){panels.forEach(function(p){p.hidden=true})},true)});
document.querySelectorAll('.tabs').forEach(function(nav){var bs=[].slice.call(nav.querySelectorAll('button'));
bs.forEach(function(b){b.addEventListener('click',function(){bs.forEach(function(x){x.classList.toggle('on',x===b)});
document.querySelectorAll('.tabpane').forEach(function(p){p.hidden=p.dataset.tab!==b.dataset.tab})})})});
"""


@dataclass
class Table:
    title: str
    header: list[str]
    rows: list[list]                 # cells are numbers, strings or None ("-" on the page)
    text_columns: int = 1            # the first columns are text, the rest are numbers
    intro: str = ""
    flag: Callable[[list], bool] | None = None   # rows to point out (first cell in orange)
    total: list | None = None        # last row, bold, stays at the bottom when sorting
    collapsed: bool = False          # shown as a closed section ("title (rows)") that opens on click
    tab: str = ""                    # tables with a tab name are shown as tabs of one page (first tab open)
    show_count: bool = True          # collapsed tables: "title (rows)"
    filterable: bool = True          # a dropdown under every column heading to pick the values to show


@dataclass
class Report:
    name: str                        # file name: <name>_<time>.html
    title: str
    subtitle: str
    intro: str = ""
    cards: list[tuple[object, str]] = field(default_factory=list)   # (value, label)
    tables: list[Table] = field(default_factory=list)
    csv_table: int = 0               # the table --csv prints
    notes: list[str] = field(default_factory=list)                  # "OK ..." green, "DOES NOT MATCH ..." red, "Not checked ..." orange


def add_report_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", default=None, help=f"Report page (default: {STATS_DIR.as_posix()}/<report>_<time>.html)")
    parser.add_argument("--no-open", action="store_true", help="Do not open the report in the browser")


def make_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--csv", action="store_true",
                        help="Print the first table as CSV on stdout instead of the tables and the page (the notes go to stderr)")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR), help="Where the runs are (default: tests/output/runs)")
    add_report_args(parser)
    return parser


def add_review_arg(parser: argparse.ArgumentParser) -> None:
    from tests.mutation_testing.inference.review_store import REVIEW_DIR
    parser.add_argument("--review-dir", default=REVIEW_DIR,
                        help="Folder with the rule review files; rules accepted after the newest run started are listed apart")


def run_dirs(args) -> list[Path]:
    """Run folders, oldest first; other folders (e.g. an archive folder) are not runs."""
    return iter_run_dirs(Path(args.runs_dir))


def text_of(x) -> str:
    if x is None:
        return "-"
    return str(round(x, 2)) if isinstance(x, float) else str(x)


def print_table(table: Table) -> None:
    body = [[text_of(x) for x in row] for row in table.rows + ([table.total] if table.total else [])]
    widths = [max(len(c) for c in col) for col in zip(table.header, *body)] if body else [len(h) for h in table.header]
    for line in [table.header] + body:
        print("  ".join(c.ljust(w) if i < table.text_columns else c.rjust(w) for i, (c, w) in enumerate(zip(line, widths))))


def _table_html(t: Table) -> str:
    e = html.escape
    out = []
    if t.collapsed:
        count = " (" + str(len(t.rows)) + ")" if t.show_count else ""
        out.append(f"<details class=fold><summary>{e(t.title)}{count}</summary>")
    elif t.title:
        out.append(f"<h2>{e(t.title)}</h2>")
    if t.intro:
        out.append(f"<p class=sub>{e(t.intro)}</p>")
    heads = "".join("<th class=n>" + e(h.replace("_", " ")) if i >= t.text_columns else "<th>" + e(h.replace("_", " "))
                    for i, h in enumerate(t.header))
    out.append("<div class=scroll><table class=sortable><tr>" + heads + "</tr>")
    if t.filterable:
        out.append("<tr class=filters>" + "<td>" * len(t.header) + "</tr>")
    for row, is_total in [(r, False) for r in t.rows] + ([(t.total, True)] if t.total else []):
        cells = []
        for i, x in enumerate(row):
            classes = ["n"] if i >= t.text_columns else []
            empty = x is None or x == "-"
            if empty or (isinstance(x, (int, float)) and x == 0):
                classes.append("mute")
            elif i == 0 and t.flag and not is_total and t.flag(row):
                classes.append("warn")
            attrs = (" class='" + " ".join(classes) + "'" if classes else "") + (" data-v=-1" if empty else "")
            cells.append("<td" + attrs + ">" + e(text_of(x)))
        out.append("<tr class=total>" if is_total else "<tr>")
        out.append("".join(cells) + "</tr>")
    out.append("</table></div>")
    if t.collapsed:
        out.append("</details>")
    return "\n".join(out)


def render_page(report: Report) -> str:
    e = html.escape
    out = ["<!doctype html><html lang=en><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{e(report.title)}</title><style>{CSS}{BUTTON_CSS}{EXTRA_CSS}</style></head><body><main>",
           f"<h1>{e(report.title)}</h1>",
           f"<p class=sub>{e(report.subtitle)} · generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>"]
    if report.intro:
        out.append(f"<p class=sub>{e(report.intro)}</p>")
    if report.cards:
        out.append("<div class=cards>" + "".join(f"<div class=card><b>{e(text_of(v))}</b><span>{e(label)}</span></div>"
                                                   for v, label in report.cards) + "</div>")
    tabs = list(dict.fromkeys(t.tab for t in report.tables if t.tab))
    nav_done = False
    for t in report.tables:
        if not t.tab:
            out.append(_table_html(t))
            continue
        if not nav_done:
            buttons = "".join(f"<button type=button data-tab='{i}'" + (" class=on" if i == 0 else "") + f">{e(n)}</button>"
                              for i, n in enumerate(tabs))
            out.append("<div class=tabs>" + buttons + "</div>")
            nav_done = True
        k = tabs.index(t.tab)
        out.append(f"<div class=tabpane data-tab='{k}'" + ("" if k == 0 else " hidden") + ">" + _table_html(t) + "</div>")
    if report.notes:
        out.append("<h2>Notes</h2>")
        for note in report.notes:
            kind = ("good" if note.startswith("OK") else "bad" if note.startswith("DOES NOT MATCH")
                    else "warn" if note.startswith("Not checked") else "")
            out.append(f"<p class='note {kind}'>{e(note)}</p>")
    out.append(f"</main><script>{SORT_JS}</script></body></html>")
    return "\n".join(out)


def write_page(report: Report, out: str | None = None) -> Path:
    path = Path(out) if out else STATS_DIR / f"{report.name}_{datetime.now():%Y%m%d_%H%M%S}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_page(report), encoding="utf-8")
    return path


def show_page(path: Path, no_open: bool) -> None:
    print(f"\n[Stats] report saved: {path}")
    print(f"[Stats] open in browser: {path.resolve().as_uri()}")
    if not no_open:
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception:
            pass


def publish(args, report: Report) -> None:
    """--csv: the chosen table as CSV on stdout. Otherwise only the saved page (nothing is printed besides its path)."""
    if args.csv:
        table = report.tables[report.csv_table]
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(table.header)
        writer.writerows([["" if x is None else x for x in row] for row in table.rows])
        print("\n".join(report.notes), file=sys.stderr)
        return
    show_page(write_page(report, args.out), args.no_open)