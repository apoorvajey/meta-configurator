"""Learned bugs: a rule marked 'learned_bug' in the review can also become a checked invariant that fails when the bug happens.

The invariant is the negation of the learned rule ("data does NOT have exactly 2 changes"), limited to the same category
and use stratum. Only rules with a clean negation are supported: change_count and edit_distance in 'exact' mode.
"""
from __future__ import annotations

import hashlib
import json
import re

from tests.mutation_testing.inference.descriptions import describe_rule, rule_statement


def negation_unsupported_reason(rule: dict) -> str | None:
    """None if the rule can be negated into a checked invariant, otherwise why not."""
    if rule["direction"] != "positive":
        return "the rule already says that something does not happen"
    if rule["template"] == "change_count":
        return None
    if rule["template"] == "edit_distance" and rule["params"].get("mode") == "exact":
        return None
    if rule["template"] == "edit_distance":
        return "'at most' edit distance has no exact opposite"
    return f"template '{rule['template']}' has no supported opposite yet"


def negate_rule(rule: dict, bug: str = "") -> dict:
    """The checked invariant for a learned bug: fails whenever the learned rule's statement holds."""
    core = {"template": rule["template"], "category": rule["category"], "params": rule["params"], "direction": "negative"}
    rule_id = "INF-" + hashlib.sha1(json.dumps({**core, "negates": rule["id"]}, sort_keys=True).encode()).hexdigest()[:8].upper()
    statement = rule_statement(rule["template"], rule["category"], rule["params"], False)
    return {"id": rule_id, **core, "statement": statement, "card": describe_rule(core), "min_support": 1.0,
            "evidence": {}, "verdict": "LEARNED_BUG", "promotion": "learned_bug", "negates": rule["id"],
            "bug": bug, "learned_from": rule["statement"]}


def check_wanted(rule_id: str, reviewed: dict) -> bool:
    """Did the review mark this rule as a learned bug and ask for a checking invariant?"""
    entry = reviewed.get(rule_id, {})
    return entry.get("status") == "learned_bug" and bool(entry.get("check"))


def learned_bug_checks(rules: list[dict], reviewed: dict) -> list[dict]:
    """Negated invariants for every given rule whose review entry asks for one (and that can be negated)."""
    return [negate_rule(r, reviewed[r["id"]].get("bug", "")) for r in rules
            if check_wanted(r["id"], reviewed) and negation_unsupported_reason(r) is None]


def log_learned_bugs(checks: list[dict], reviewed: dict) -> None:
    """Put the bugs of the learned-bug checks into the known bugs list (tests/mutation_testing/config/triage_config.json).

    A bug that is already in the list only gets the new invariants added. A new bug needs the bug id and 'where' from the
    review page; it is flagged "learned" and "confirmed": false until it is confirmed once on the bugs page.
    """
    from datetime import datetime
    from tests.mutation_testing.reports.known_bugs_tool import KNOWN, read_lists, write_lists

    by_bug: dict[str, list[dict]] = {}
    for check in checks:
        if check.get("bug"):
            by_bug.setdefault(check["bug"], []).append(check)
    if not by_bug:
        return
    lists = read_lists()
    changed = False
    for bug_id, items in by_bug.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", bug_id):
            print(f"[Learned bug] '{bug_id}' is not a usable bug ID (letters, digits, - _ . only, e.g. BUG-08) - not logged. Fix it on the review page")
            continue
        ids = sorted({c["id"] for c in items})
        entry = reviewed.get(items[0]["negates"], {})
        existing = next((b for lst in lists.values() for b in lst if b.get("id") == bug_id), None)
        if existing is not None and not existing.get("learned"):
            print(f"[Learned bug] {bug_id} is already used by another bug ({existing.get('name') or existing.get('summary', '')[:50]}) - "
                  "its invariants are left alone. Use a new bug ID on the review page")
            continue
        if existing is not None:
            missing = [i for i in ids if i not in existing.get("invariants", [])]
            if missing:
                existing["invariants"] = list(existing.get("invariants", [])) + missing
                changed = True
                print(f"[Learned bug] {bug_id}: added {', '.join(missing)} to its invariants")
            continue
        if entry.get("bugWhere") not in ("data", "schema", "both"):
            print(f"[Learned bug] {bug_id}: not logged - the review has no 'Where'. Fill it in on the review page and run infer --export-only again")
            continue
        bug = {"id": bug_id, "summary": str(entry.get("bugSummary", "")).strip() or "Learned bug: " + items[0]["learned_from"], "invariants": ids,
               "where": entry["bugWhere"], "learned": True, "confirmed": False, "added_at": f"{datetime.now():%Y-%m-%d %H:%M}"}
        for key, field in (("name", "bugName"), ("group", "bugGroup"), ("comment", "bugNotes")):
            if str(entry.get(field, "")).strip():
                bug[key] = str(entry[field]).strip()
        lists[KNOWN].append(bug)
        changed = True
        print(f"[Learned bug] {bug_id} logged in the known bugs list (confirm it once on the bugs page): {', '.join(ids)}")
    if changed:
        write_lists(lists)