"""Finds which earlier step likely caused each invariant failure.

Uses the per-step fields that replay_runner.export_history() writes into history.json:

    checked_invariants : ids of every rule evaluated after the step
    failed_invariants  : full names of the rules that failed
    failure_evidence   : [{"id", "message", "paths": [...]}] for each failure
    facet_hashes       : short hash per state facet (de_data, se_schema, de_text, ...)

A rule can only fail at a step where it is evaluated, but the state may have gone wrong earlier.
For each rule, every action between the last step where it was evaluated and passed and the first
step where it failed is a suspect. Suspects are ranked by whether the action touched a path named
in the failure evidence and by which state facets changed at that step.

Used by analyze_mutation_logs. Pure functions, no I/O.
"""
from __future__ import annotations

import html
import re
from collections import Counter, defaultdict

FACET_LABEL = {
    "de_data": "Data (internal model)",
    "de_schema": "Schema seen from Data panel",
    "se_schema": "Schema (internal model)",
    "se_data": "Data seen from Schema panel",
    "de_text": "Data Text View",
    "se_text": "Schema Text View",
}


def _esc(x) -> str:
    return html.escape(str(x), quote=True)


# Helpers

def segments(path: str) -> list[str]:
    """'A[0].b' / 'A.0.b' -> ['A', '0', 'b']"""
    p = re.sub(r"\[(\d+)\]", r".\1", str(path or ""))
    return [s for s in p.replace(":", ".").split(".") if s and s not in ("(root)", "(unresolved)")]


def relation(action_path: str, implicated: list[str]) -> str:
    """'same': the action touched the implicated path (or a parent or child of it).
    'sibling': same top-level key. 'none', or 'n/a' if the action has no path.
    """
    a = segments(action_path)
    if not a:
        return "n/a"
    best = "none"
    for imp in implicated:
        b = segments(imp)
        if not b:
            continue
        n = min(len(a), len(b))
        if a[:n] == b[:n]:
            return "same"
        if a[0] == b[0]:
            best = "sibling"
    return best


def _ids(step: dict, key: str) -> set[str]:
    return {str(x).partition(" ")[0] for x in (step.get(key) or [])}


def has_causal_fields(run) -> bool:
    return any("checked_invariants" in s for s in run.steps)


def _implicated(step: dict, inv_id: str) -> tuple[list[str], str]:
    paths, msg = [], ""
    for ev in step.get("failure_evidence") or []:
        if isinstance(ev, dict) and ev.get("id") == inv_id:
            paths += [str(p) for p in ev.get("paths") or []]
            msg = ev.get("message") or msg
    return paths, msg


def _changed_facets(run, i: int) -> list[str]:
    cur = run.steps[i].get("facet_hashes") or {}
    if not cur:
        return []
    for j in range(i - 1, -1, -1):
        if run.kinds[j] == "walkback":
            continue
        prev = run.steps[j].get("facet_hashes") or {}
        if prev:
            return [f for f in cur if prev.get(f) != cur[f]]
        break
    return []


# Analysis

def analyse(ep, inv_id: str, cfg: dict, parse_fn, panel_fn) -> dict:
    run, ccfg = ep.run, cfg["causal"]
    first = next((i for i in range(ep.start, ep.end + 1)
                  if run.kinds[i] != "walkback" and inv_id in _ids(run.steps[i], "failed_invariants")),
                 ep.start)

    clean = None
    for i in range(first - 1, -1, -1):
        if run.kinds[i] == "walkback":
            continue
        s = run.steps[i]
        if inv_id in _ids(s, "checked_invariants") and inv_id not in _ids(s, "failed_invariants"):
            clean = i
            break

    lo = clean + 1 if clean is not None else max(0, first - ccfg["max_lookback_when_no_clean_step"])
    window = [i for i in range(lo, first + 1) if run.kinds[i] != "walkback"]
    implicated, message = _implicated(run.steps[first], inv_id)

    rows = []
    for i in window:
        s = run.steps[i]
        rows.append({
            "i": i,
            "panel": panel_fn(s),
            "kind": parse_fn(s.get("action_id", ""))["kind"],
            "auto": run.kinds[i] == "auto_undo",
            "checked": inv_id in _ids(s, "checked_invariants"),
            "rel": relation(parse_fn(s.get("action_id", ""))["path"], implicated) if implicated else "n/a",
            "facets": _changed_facets(run, i),
        })

    manual = [r for r in rows if not r["auto"] and r["i"] != first]
    reads = set(ccfg.get("rule_reads", {}).get(inv_id, []))
    for r in rows:
        # a rule not listed in rule_reads counts any state change as relevant
        r["relevant"] = bool(r["facets"]) and (not reads or bool(reads & set(r["facets"])))
    lead, why = None, ""
    same = [r for r in manual if r["rel"] == "same"]
    sib = [r for r in manual if r["rel"] == "sibling"]
    silent = [r for r in manual if r["relevant"] and not r["checked"]]
    if inv_id.startswith(tuple(ccfg.get("transition_rule_prefixes", []))):
        # a transition rule compares the states around one action, so the failing action itself is the lead
        same, sib, silent = [], [], []
    if same:
        lead, why = same[-1], "this is the latest action that touched the path named in the failure evidence"
    elif sib:
        lead, why = sib[-1], "this is the latest action on the same top-level key as the failure evidence"
    elif len(silent) == 1:
        lead, why = silent[0], ("this is the only action in the window that changed state this rule reads "
                                "while the rule was not being evaluated")
    elif silent:
        lead, why = silent[0], (f"this is the first of {len(silent)} actions that changed state this rule "
                                "reads while it was not being evaluated; any of them could be the cause")
    return {
        "first": first, "clean": clean, "rows": rows, "lead": lead, "why": why,
        "candidates": silent, "reads": sorted(reads),
        "implicated": implicated, "message": message,
        "unchecked": sum(1 for r in rows if not r["checked"] and r["i"] != first),
        "panel_clean": panel_fn(run.steps[clean]) if clean is not None else None,
        "panel_fail": panel_fn(run.steps[first]),
    }


# Rendering

def lead_lines(ep, cfg: dict, label_fn, parse_fn, panel_fn) -> list[str]:
    """One sentence per invariant in this episode, naming the best-lead action (see analyse()).

    Empty if the run predates the cause-tracking fields or the episode has no named invariant.
    """
    if not has_causal_fields(ep.run) or not ep.invariants:
        return []
    out = []
    for inv_id in sorted(ep.invariants):
        a = analyse(ep, inv_id, cfg, parse_fn, panel_fn)
        rule = cfg["invariants"].get(inv_id, ep.invariants[inv_id] or inv_id)
        if a["lead"]:
            r = a["lead"]
            fac = ", ".join(FACET_LABEL.get(f, f) for f in r["facets"]) or "no state change recorded"
            out.append(f'<b>{_esc(inv_id)}</b> ({_esc(rule)}): likely started at '
                       f'<b>action {r["i"]}</b> ({_esc(label_fn(r["i"]))}) &ndash; {_esc(a["why"])}. '
                       f'State changed: {_esc(fac)}.')
        else:
            out.append(f'<b>{_esc(inv_id)}</b> ({_esc(rule)}): no earlier action stands out; '
                       'the failing action itself is the best lead.')
    return out


def causal_html(ep, cfg: dict, label_fn, parse_fn, panel_fn) -> str:
    run, ccfg = ep.run, cfg["causal"]
    if not has_causal_fields(run):
        return ('<h3>Where it likely started</h3><p class="muted small">This run was recorded before the '
                'cause-tracking fields existed (checked_invariants / failure_evidence / facet_hashes). '
                'Re-run the mutation loop to get them.</p>')
    if not ep.invariants:
        return ""
    out = ["<h3>Where it likely started</h3>"]
    for inv_id in sorted(ep.invariants):
        a = analyse(ep, inv_id, cfg, parse_fn, panel_fn)
        rule = cfg["invariants"].get(inv_id, ep.invariants[inv_id] or inv_id)
        out.append(f"<p><b>{_esc(inv_id)}</b> &ndash; {_esc(rule)}</p><ul>")
        if a["clean"] is None:
            out.append("<li>No earlier step is recorded where this rule was evaluated and passed, "
                       "so the window below is only the last actions before the failure.</li>")
        else:
            out.append(f"<li>Last evaluated and passing at action <b>{a['clean']}</b> "
                       f"({_esc(label_fn(a['clean']))}); first failing at action <b>{a['first']}</b>.</li>")
        if a["clean"] is not None and a["panel_clean"] != a["panel_fail"]:
            out.append(f"<li>It was last checked in the <b>{_esc(a['panel_clean'])}</b> and first failed in the "
                       f"<b>{_esc(a['panel_fail'])}</b>. The failing step is where the rule became "
                       "<i>checkable</i>, not necessarily where things went wrong.</li>")
        if a["unchecked"]:
            panels_seen = sorted({r["panel"] for r in a["rows"] if not r["checked"] and r["i"] != a["first"]})
            out.append(f"<li>The rule was <b>not evaluated</b> at {a['unchecked']} of the "
                       f"{len(a['rows']) - 1} actions before the failure. Rules are only checked while the "
                       f"editor shows their own panel; the active panel at those actions was: "
                       f"{_esc(', '.join(panels_seen))}.</li>")
            if a["reads"] and a["candidates"]:
                reads_txt = ", ".join(FACET_LABEL.get(f, f) for f in a["reads"])
                out.append(f"<li>Of those, <b>{len(a['candidates'])}</b> changed state this rule reads "
                           f"({_esc(reads_txt)}): action(s) {', '.join(str(r['i']) for r in a['candidates'])}. "
                           "The remaining actions changed none of it (GUI-only changes such as expand/collapse "
                           "are not tracked, so they cannot be fully ruled out).</li>")
        if a["implicated"]:
            shown = ", ".join(f"<code>{_esc(p)}</code>" for p in a["implicated"][:ccfg["max_paths_shown"]])
            out.append(f"<li>Failure evidence points at: {shown}.</li>")
        if a["message"]:
            out.append(f"<li>Check message: {_esc(a['message'])}</li>")
        if a["lead"]:
            r = a["lead"]
            fac = ", ".join(FACET_LABEL.get(f, f) for f in r["facets"]) or "no state change recorded"
            out.append(f"<li><b>Best lead: action {r['i']}</b> ({_esc(label_fn(r['i']))}) &ndash; "
                       f"{_esc(a['why'])}. State changed at that step: {_esc(fac)}.</li>")
        else:
            out.append("<li>No earlier action stands out; the failing action itself is the best lead.</li>")
        out.append("</ul>")

        rows = a["rows"][-ccfg["max_window_rows"]:]
        out.append('<table><tr><th>Action</th><th>What was done</th><th>Panel</th>'
                   '<th>Rule evaluated after it?</th><th>Touches failing path?</th><th>State changed</th></tr>')
        for r in rows:
            mark = " &larr; best lead" if a["lead"] and r["i"] == a["lead"]["i"] else \
                   " &larr; first failure" if r["i"] == a["first"] else ""
            rel = {"same": "yes", "sibling": "same top-level key", "none": "no", "n/a": "&ndash;"}[r["rel"]]
            fac = ", ".join(_esc(FACET_LABEL.get(f, f)) for f in r["facets"]) or "&ndash;"
            out.append(f"<tr><td>{r['i']}{mark}</td><td>{_esc(label_fn(r['i']))}{' (auto)' if r['auto'] else ''}</td>"
                       f"<td>{_esc(r['panel'])}</td><td>{'yes' if r['checked'] else 'no'}</td>"
                       f"<td>{rel}</td><td>{fac}</td></tr>")
        out.append("</table>")
        if len(a["rows"]) > len(rows):
            out.append(f'<p class="muted small">Showing the last {len(rows)} of {len(a["rows"])} actions in the window.</p>')
    return "".join(out)


def lead_table_html(eps, cfg: dict, parse_fn, panel_fn) -> str:
    """Cross-run view: which action kinds are over-represented in the suspect windows of each rule,
    compared with how often they occur overall.
    """
    ccfg = cfg["causal"]
    runs = {id(e.run): e.run for e in eps}
    base, base_total = Counter(), 0
    for run in runs.values():
        for i, s in enumerate(run.steps):
            if run.kinds[i] == "walkback":
                continue
            base[parse_fn(s.get("action_id", ""))["kind"]] += 1
            base_total += 1
    if not base_total:
        return ""

    per_rule: dict[str, dict] = {}
    for ep in eps:
        if not has_causal_fields(ep.run):
            continue
        for inv_id in ep.invariants:
            a = analyse(ep, inv_id, cfg, parse_fn, panel_fn)
            d = per_rule.setdefault(inv_id, {"n": 0, "kinds": Counter(), "steps": 0, "latent": 0, "lead_same": 0})
            d["n"] += 1
            d["latent"] += bool(a["unchecked"])
            d["lead_same"] += bool(a["lead"] and a["why"].startswith("this is the latest action that touched"))
            for r in a["rows"]:
                if r["i"] != a["first"] and not r["auto"]:
                    d["kinds"][r["kind"]] += 1
                    d["steps"] += 1
    if not per_rule:
        return ""

    out = ["<h2>Which actions come before each rule's failures</h2>",
           '<p class="muted small">Lift = how much more often an action kind appears in the suspect window '
           'than in the run overall (1.0 = no different). Only rules with at least '
           f'{ccfg["min_windows_for_stats"]} failure episodes are ranked.</p>',
           "<table><tr><th>Rule</th><th>Episodes</th><th>Episodes with unevaluated gap</th>"
           "<th>Episodes with a path-matched lead</th><th>Most over-represented actions (lift)</th></tr>"]
    for inv_id, d in sorted(per_rule.items(), key=lambda kv: -kv[1]["n"]):
        if d["n"] >= ccfg["min_windows_for_stats"] and d["steps"]:
            lifts = sorted(((k, (c / d["steps"]) / (base[k] / base_total)) for k, c in d["kinds"].items() if base[k]),
                           key=lambda kv: -kv[1])[:ccfg["top_kinds"]]
            txt = ", ".join(f"{_esc(k)} ({v:.1f}x)" for k, v in lifts)
        else:
            txt = "not enough episodes"
        out.append(f"<tr><td>{_esc(inv_id)}</td><td>{d['n']}</td><td>{d['latent']}</td>"
                   f"<td>{d['lead_same']}</td><td>{txt}</td></tr>")
    out.append("</table>")
    return "".join(out)


def invariant_explanations_html(eps, cfg: dict, parse_fn, panel_fn) -> str:
    """For every invariant that broke in at least cfg["causal"]["min_windows_for_stats"] sessions,
    aggregate its failing episodes across all sessions and describe the common pattern in a short
    paragraph.

    Each sentence reads out a statistic (dominant lead-action kind, panel or top-level path, or an
    over-represented action kind as in lead_table_html) and is only written when it holds for a clear
    majority of that invariant's occurrences. Otherwise only the count is given, with a note that no
    single pattern stands out. Runs without the cause-tracking fields are excluded.
    """
    ccfg = cfg["causal"]
    by_inv: dict[str, list[tuple]] = defaultdict(list)
    for ep in eps:
        if not has_causal_fields(ep.run):
            continue
        for inv_id in ep.invariants:
            by_inv[inv_id].append((ep, analyse(ep, inv_id, cfg, parse_fn, panel_fn)))

    base, base_total = Counter(), 0
    for run in {id(ep.run): ep.run for ep in eps}.values():
        for i, s in enumerate(run.steps):
            if run.kinds[i] == "walkback":
                continue
            base[parse_fn(s.get("action_id", ""))["kind"]] += 1
            base_total += 1

    sections = []
    for inv_id, items in sorted(by_inv.items(), key=lambda kv: -len(kv[1])):
        sessions = {id(ep.run) for ep, _ in items}
        if len(sessions) < ccfg["min_windows_for_stats"]:
            continue
        n = len(items)
        rule = cfg["invariants"].get(inv_id, next(ep.invariants.get(inv_id) for ep, _ in items if inv_id in ep.invariants) or inv_id)

        lead_kinds, panels, top_paths, window_kinds = Counter(), Counter(), Counter(), Counter()
        window_steps = 0
        leads_found = 0
        for ep, a in items:
            panels[panel_fn(ep.run.steps[a["first"]])] += 1
            if a["lead"]:
                leads_found += 1
                lead_kinds[parse_fn(ep.run.steps[a["lead"]["i"]].get("action_id", ""))["kind"]] += 1
            for p in a["implicated"]:
                seg = segments(p)
                if seg:
                    top_paths[seg[0]] += 1
            for r in a["rows"]:
                if r["i"] != a["first"] and not r["auto"]:
                    window_kinds[r["kind"]] += 1
                    window_steps += 1

        sentences = [f"Failed <b>{n}</b> time(s) across <b>{len(sessions)}</b> session(s)."]
        if leads_found:
            kind, cnt = lead_kinds.most_common(1)[0]
            pct = round(100 * cnt / leads_found)
            if pct >= 50:
                sentences.append(f"In {pct}% of the occurrences with an identified lead, that lead was a "
                                 f"<b>{_esc(kind)}</b> action.")
        if panels:
            panel, cnt = panels.most_common(1)[0]
            pct = round(100 * cnt / n)
            if pct >= 60:
                sentences.append(f"{pct}% of the failures first became checkable in the <b>{_esc(panel)}</b>.")
        if top_paths:
            top, cnt = top_paths.most_common(1)[0]
            pct = round(100 * cnt / n)
            if pct >= 40:
                sentences.append(f"The failure evidence most often names a path under <code>{_esc(top)}</code> "
                                 f"({pct}% of occurrences).")
        if window_steps and window_kinds:
            lifts = sorted(((k, (c / window_steps) / (base[k] / base_total)) for k, c in window_kinds.items() if base[k]),
                           key=lambda kv: -kv[1])[:ccfg["top_kinds"]]
            strong = [(k, v) for k, v in lifts if v >= 1.5]
            if strong:
                txt = ", ".join(f"<b>{_esc(k)}</b> ({v:.1f}x)" for k, v in strong)
                sentences.append(f"Actions that show up more often than usual in the run-up to this failure: {txt}.")
        if len(sentences) == 1:
            sentences.append("No single action kind, panel, or path dominates enough of these occurrences "
                             "to call out a pattern -- see the individual occurrences below.")

        occurrences = ", ".join(
            f"{_esc(ep.run.run_id)} action {a['first']}" for ep, a in
            sorted(items, key=lambda ea: (ea[0].run.run_id, ea[0].start))[:12])
        more = f", and {n - 12} more" if n > 12 else ""

        sections.append(
            f'<div class="explain"><h3>{_esc(inv_id)}</h3><p class="muted small">{_esc(rule)}</p>'
            f'<p>{" ".join(sentences)}</p>'
            f'<p class="small"><b>Occurrences:</b> {occurrences}{more}. Full detail on each is in the '
            f'Candidates section below (search the invariant ID).</p></div>'
        )

    if not sections:
        return ""
    return ("<h2>Recurring failures across sessions</h2>"
           '<p class="muted small">Each broken rule below failed in enough separate sessions to look for a '
           "common cause. Every sentence names a real majority among that rule's own occurrences -- nothing "
           "here is inferred beyond what the numbers say.</p>" + "".join(sections))