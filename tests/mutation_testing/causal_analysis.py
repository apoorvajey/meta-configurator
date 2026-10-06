"""Causal analysis: which earlier step caused an invariant failure? Pure functions, no I/O.

Uses the per-step fields written into history.json:

    checked_invariants : ids of every rule evaluated after that step
    failed_invariants  : full names ("INV-D6 GUI validation ...") that failed
    failure_evidence   : [{"id", "message", "paths": [...]}] for each failure
    facet_hashes       : short hash per state facet (de_data, se_schema, de_text, ...)

A rule can only fail at a step where it is evaluated, but the state can go wrong earlier. For each rule the
window runs from the last step where it was evaluated and passed to the first step where it failed. Every action
in the window is a suspect, ranked by whether it touched a path named in the failure evidence and by which state
facets it changed.
"""
from __future__ import annotations

import re


# helpers
def segments(path: str) -> list[str]:
    """'A[0].b' / 'A.0.b' -> ['A', '0', 'b']"""
    p = re.sub(r"\[(\d+)\]", r".\1", str(path or ""))
    return [s for s in p.replace(":", ".").split(".") if s and s not in ("(root)", "(unresolved)")]


def relation(action_path: str, implicated: list[str]) -> str:
    """'same' = action touched the implicated path (or a parent/child of it),
    'sibling' = same top-level key, 'none', or 'n/a' (action has no path)."""
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


# analysis
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
        # relevant = this step changed state the rule reads (rule not listed in rule_reads -> any change counts)
        r["relevant"] = bool(r["facets"]) and (not reads or bool(reads & set(r["facets"])))
    lead, why = None, ""
    same = [r for r in manual if r["rel"] == "same"]
    sib = [r for r in manual if r["rel"] == "sibling"]
    silent = [r for r in manual if r["relevant"] and not r["checked"]]
    if inv_id.startswith(tuple(ccfg.get("transition_rule_prefixes", []))):
        # A transition rule ("after <action>: <property> is unchanged") only compares the states before and
        # after ONE action, so no earlier action can be its cause: the failing action itself is the lead.
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


# rendering
