"""The list of every invariant: hand-written ones (tests/invariants) and inferred ones (inferred_rules.json).

`python -m tests.cli invariants` collects them here, prints them and rewrites the HTML report
tests/output/reports/invariants/invariants.html (one fixed file name, refreshed on every call). The report can be
filtered by origin (hand-written / inferred), by type and by free text.

Type of a hand-written invariant: the editor it runs in (data, schema, any) or "temporal".
Type of an inferred invariant: the rule template it was mined with (e.g. stability, key_containment).
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.common.paths import INFERRED_RULES_PATH, INVARIANTS_DIR
from tests.mutation_testing.common.report_theme import DARK_VARS

HAND = "hand-written"
INFERRED = "inferred"
REPORT_NAME = "invariants.html"


def first_sentence(doc: str | None) -> str:
    if not doc:
        return ""
    paragraph = " ".join(doc.strip().split("\n\n")[0].split())
    return paragraph.split(". ")[0].rstrip(".")


def _full_doc(doc: str | None) -> str:
    return "\n".join(line.strip() for line in (doc or "").strip().splitlines())


def _read_rules(path: Path) -> list[dict]:
    """Rules straight from the file (not the cache of inferred_rules.py), so the list is always current."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))["rules"]
    except (OSError, ValueError, KeyError):
        return []


def _bug_label(bug_id: str) -> str:
    """'BUG-12 · name' from the known bugs list; just the ID if the bug is not in the list."""
    if not bug_id:
        return ""
    try:
        from tests.mutation_testing.runs.known_bugs import bug_label_for
        return bug_label_for(bug_id) or bug_id
    except Exception:
        return bug_id


def collect_invariants(rules_path: Path | str = INFERRED_RULES_PATH) -> list[dict]:
    """One dict per invariant: id, origin, type, text, where, detail (label -> text), evidence."""
    from tests.invariants.invariants_core import list_invariants
    from tests.invariants.temporal_invariants import list_temporal_invariants

    rows: list[dict] = []
    for p in list_invariants():
        rows.append({"id": p.id, "origin": HAND, "type": p.mode, "text": first_sentence(p.check.__doc__),
                     "where": f"tests/invariants/state/{p.module}.py",
                     "detail": {"Description": _full_doc(p.check.__doc__)}, "evidence": {}})
    for p in list_temporal_invariants():
        rows.append({"id": p.id, "origin": HAND, "type": "temporal",
                     "text": first_sentence(p.check.__doc__).removeprefix(f"{p.id}: "),
                     "where": f"tests/invariants/temporal/{p.module}.py",
                     "detail": {"Description": _full_doc(p.check.__doc__)}, "evidence": {}})
    for r in _read_rules(Path(rules_path)):
        card = r.get("card", {})
        learned = r.get("promotion") == "learned_bug"
        bug = _bug_label(r.get("bug", "")) if learned else ""
        detail = {"Trigger": card.get("trigger", ""), "Applies when": card.get("applies_when", ""),
                  "Asserts": card.get("asserts", ""), "Definitions": card.get("definitions", ""),
                  "Direction": r.get("direction", ""), "Promotion": r.get("promotion", ""),
                  "Minimum support": f"{r.get('min_support', 0.0):.0%}"}
        if learned:   # a check made from a rule flagged as a learned bug in the rule review (inference/learned_bug.py)
            detail = {"Learned bug": bug or "(no bug ID)", "Negates rule": r.get("negates", ""),
                      "Learned from": r.get("learned_from", ""), **detail}
        from tests.mutation_testing.inference.rule_history import detail_rows   # when it was accepted, and its review log
        detail = {**detail, **detail_rows(r["id"])}
        rows.append({"id": r["id"], "origin": INFERRED, "type": r["template"], "text": r["statement"],
                     "where": f"action category: {r['category']}", "kind": "learned" if learned else "", "bug": bug,
                     "detail": detail, "evidence": r.get("evidence", {})})
    return rows


CSS = ":root{" + DARK_VARS + "}" + """
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 60px}h1{font-size:26px;margin:0 0 4px}
.sub{color:var(--mute);margin:0 0 22px}code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.card b{display:block;font-size:24px}.card span{color:var(--mute);font-size:13px}
.bar{position:sticky;top:0;z-index:2;background:var(--bg);padding:10px 0;border-bottom:1px solid var(--line);margin-bottom:12px}
.frow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:4px 0}.flbl{color:var(--mute);font-size:12.5px;text-transform:uppercase;letter-spacing:.03em;min-width:62px;flex:0 0 62px}
button.f{font:inherit;font-size:13px;padding:3px 11px;border:1px solid var(--line);border-radius:20px;background:var(--chip);color:var(--ink);cursor:pointer}
button.f:hover{border-color:var(--accent)}button.f.on{background:var(--accent);border-color:var(--accent);color:#14161b;font-weight:600}
button.f small{opacity:.7;margin-left:4px}
input.q{font:inherit;font-size:14px;padding:5px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);width:260px;max-width:100%}
#count{color:var(--mute);font-size:13px;margin-left:auto}
details.inv{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:6px 0}
details.inv>summary{cursor:pointer;list-style:none;padding:9px 14px;display:grid;grid-template-columns:130px 96px 1fr;gap:12px;align-items:baseline}
details.inv>summary::-webkit-details-marker{display:none}details.inv>summary:hover{background:#222733}
.id{font-weight:600;font-variant-numeric:tabular-nums}.tag{display:inline-block;background:var(--chip);border-radius:20px;padding:1px 9px;font-size:12px;white-space:nowrap}
.tag.hand{color:var(--good)}.tag.inferred{color:var(--accent)}.tag.learned{color:var(--warn,#f0a93b);margin-left:6px}.ty{color:var(--mute);font-size:12.5px;margin-left:8px}
.body{border-top:1px solid var(--line);padding:10px 16px 14px}.body dl{margin:0;display:grid;grid-template-columns:130px 1fr;gap:6px 14px}
.body dt{color:var(--mute);font-size:13px}.body dd{margin:0;white-space:pre-wrap}
.ev{width:auto;border-collapse:collapse;margin-top:10px;font-size:13.5px}.ev th,.ev td{padding:3px 12px 3px 0;text-align:left;border-bottom:1px solid var(--line)}.ev th{color:var(--mute);font-weight:600}
.grp{display:flex;gap:10px;align-items:flex-start;margin:3px 0 3px 74px;padding:6px 10px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
.grp[hidden]{display:none}.glbl{flex:0 0 118px;font-size:13px;font-weight:600;line-height:1.25;padding-top:4px}.glbl em{display:block;font-style:normal;font-weight:400;color:var(--mute);font-size:11.5px}
.chips{display:flex;flex-wrap:wrap;gap:5px}button.f.zero:not(.on){opacity:.4}
.empty{color:var(--mute);padding:20px 0;display:none}
@media(max-width:700px){details.inv>summary{grid-template-columns:1fr}.body dl{grid-template-columns:1fr}}
"""

JS = """
var cards=[].slice.call(document.querySelectorAll('details.inv')),chips=[].slice.call(document.querySelectorAll('button.f[data-g=type][data-v]:not([data-v=""])')),
sel={origin:new Set(),type:new Set(),kind:new Set()},q='';
function apply(){
document.querySelectorAll('.grp').forEach(function(g){var show=!sel.origin.size||sel.origin.has(g.dataset.o);g.hidden=!show;
if(!show)g.querySelectorAll('button.f.on').forEach(function(b){b.classList.remove('on');sel.type.delete(b.dataset.v)})});
var any=document.querySelector('button.f[data-g=type][data-v=""]');any.classList.toggle('on',!sel.type.size);
chips.forEach(function(b){var n=cards.filter(function(c){return c.dataset.type===b.dataset.v&&(!sel.origin.size||sel.origin.has(c.dataset.origin))&&(!sel.kind.size||sel.kind.has(c.dataset.kind))&&(!q||c.dataset.text.indexOf(q)>=0)}).length;
b.querySelector('small').textContent=n;b.classList.toggle('zero',!n)});
var n=0;cards.forEach(function(c){var ok=(!sel.origin.size||sel.origin.has(c.dataset.origin))&&(!sel.kind.size||sel.kind.has(c.dataset.kind))&&(!sel.type.size||sel.type.has(c.dataset.type))&&(!q||c.dataset.text.indexOf(q)>=0);c.hidden=!ok;if(ok)n++});
document.getElementById('count').textContent=n+' of '+cards.length+' shown';
document.getElementById('empty').style.display=n?'none':'block'}
document.querySelectorAll('button.f').forEach(function(b){b.addEventListener('click',function(){
var g=b.dataset.g,s=sel[g];
if(b.dataset.v===''){s.clear();document.querySelectorAll('button.f[data-g='+g+']').forEach(function(x){x.classList.remove('on')});b.classList.add('on')}
else{document.querySelector('button.f[data-g='+g+'][data-v=""]').classList.remove('on');
if(s.has(b.dataset.v)){s.delete(b.dataset.v);b.classList.remove('on')}else{s.add(b.dataset.v);b.classList.add('on')}
if(!s.size)document.querySelector('button.f[data-g='+g+'][data-v=""]').classList.add('on')}apply()})});
document.getElementById('q').addEventListener('input',function(e){q=e.target.value.toLowerCase();apply()});
document.getElementById('expand').addEventListener('click',function(e){var open=e.target.dataset.open!=='1';e.target.dataset.open=open?'1':'';
e.target.textContent=open?'Collapse all':'Expand all';cards.forEach(function(c){if(!c.hidden)c.open=open})});
apply();
"""


def render(rows: list[dict]) -> str:
    e = html.escape
    origins = [HAND, INFERRED]
    types = sorted({r["type"] for r in rows}, key=lambda t: (
        0 if t in ("data", "schema", "any", "temporal") else 1, t))

    def origin_chips() -> str:
        out = ['<button class="f on" data-g="origin" data-v="">All</button>']
        for o in origins:
            n = sum(1 for r in rows if r["origin"] == o)
            out.append(f'<button class="f" data-g="origin" data-v="{e(o)}">{e(o)}<small>{n}</small></button>')
        return "".join(out)

    def kind_row() -> str:
        n = sum(1 for r in rows if r.get("kind") == "learned")
        if not n:
            return ""
        return ('<div class=frow><span class=flbl>Kind</span><button class="f on" data-g="kind" data-v="">All</button>'
                f'<button class="f" data-g="kind" data-v="learned">learned bug<small>{n}</small></button></div>')

    def type_groups() -> str:
        out = []
        for o, label in ((HAND, "editor / kind"), (INFERRED, "rule template")):
            own = [t for t in types if any(r["type"] == t and r["origin"] == o for r in rows)]
            btns = "".join(f'<button class="f" data-g="type" data-v="{e(t)}">{e(t.replace("_", " "))}'
                           f'<small>{sum(1 for r in rows if r["type"] == t and r["origin"] == o)}</small></button>' for t in own)
            out.append(f'<div class=grp data-o="{o}"><span class=glbl>{o}<em>{label}</em></span><span class=chips>{btns}</span></div>')
        return "".join(out)

    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width,initial-scale=1'><title>Invariants</title>"
             f"<style>{CSS}</style></head><body><main><h1>Invariants</h1>",
             f"<p class=sub>Hand-written (<code>tests/invariants</code>) and inferred (<code>inferred_rules.json</code>) · "
             f"generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p><div class=cards>",
             f"<div class=card><b>{len(rows)}</b><span>invariants</span></div>"]
    for o in origins:
        parts.append(f"<div class=card><b>{sum(1 for r in rows if r['origin'] == o)}</b><span>{o}</span></div>")
    parts.append(f"<div class=card><b>{len(types)}</b><span>types</span></div></div>")
    parts.append('<div class=bar>'
                 f'<div class=frow><span class=flbl>Origin</span>{origin_chips()}</div>'
                 '<div class=frow><span class=flbl>Type</span><button class="f on" data-g="type" data-v="">All</button></div>'
                 f'{kind_row()}{type_groups()}'
                 '<div class=frow><span class=flbl>Search</span><input class=q id=q placeholder="id, text, file ...">'
                 '<button class=f id=expand type=button>Expand all</button><span id=count></span></div></div>')
    for r in sorted(rows, key=lambda r: (r["origin"] != HAND, r["type"], r["id"])):
        blob = " ".join([r["id"], r["type"], r["text"], r["where"], r.get("bug", ""), *r["detail"].values()]).lower()
        detail = "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in r["detail"].items() if v)
        detail += f"<dt>Where</dt><dd>{e(r['where'])}</dd>"
        evidence = ""
        if r["evidence"]:
            evidence = ("<table class=ev><tr><th>Dataset</th><th>Held</th><th>Observations</th><th>Support</th></tr>" +
                        "".join(f"<tr><td>{e(ds)}</td><td>{v.get('held', '')}</td><td>{v.get('observations', '')}</td>"
                                f"<td>{v.get('support', 0):.0%}</td></tr>" for ds, v in r["evidence"].items()) + "</table>")
        cls = "hand" if r["origin"] == HAND else "inferred"
        learned_tag = f'<span class="tag learned">learned bug · {e(r["bug"] or "?")}</span>' if r.get("kind") == "learned" else ""
        parts.append(f'<details class=inv data-origin="{e(r["origin"])}" data-type="{e(r["type"])}" data-kind="{e(r.get("kind", ""))}" data-text="{e(blob)}">'
                     f'<summary><span class=id>{e(r["id"])}</span><span><span class="tag {cls}">{e(r["origin"])}</span>{learned_tag}</span>'
                     f'<span>{e(r["text"])}<span class=ty>{e(r["type"])}</span></span></summary>'
                     f'<div class=body><dl>{detail}</dl>{evidence}</div></details>')
    parts.append(f"<p class=empty id=empty>No invariant matches the filter.</p><script>{JS}</script></main></body></html>")
    return "".join(parts)


def write_report(rows: list[dict], out_dir: Path = INVARIANTS_DIR) -> Path:
    """Write (replace) the report; the file name is fixed, so there is always one current report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / REPORT_NAME
    out.write_text(render(rows), encoding="utf-8")
    return out