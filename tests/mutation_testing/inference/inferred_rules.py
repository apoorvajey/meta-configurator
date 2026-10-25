"""Loading and evaluation of the exported rules in inferred_rules.json."""
from __future__ import annotations

import json
from pathlib import Path
from tests.mutation_testing.inference.constants import INFERRED_RULES_PATH
from tests.mutation_testing.inference.templates import RULES
from tests.mutation_testing.inference.rule_activity import record_rule
from tests.mutation_testing.inference.categories import UNDO_INVERSE_CATEGORY, _tabbed_category, _undo_pair, _use_stratum


_RULES_CACHE: dict[str, list[dict]] = {}


def _not_applicable(rule: dict, prev: dict, curr: dict) -> bool:
    """Situations where a promoted rule is not checked at all. Decided here, in the loop; mining is unaffected.
    Rules on the data facet assume the Data root is an object. While it stays a non-object (e.g. [] after a
    root type change) there are no keys to add, so they say nothing."""
    params = rule.get("params", {})
    if "data" in (params.get("a"), params.get("b"), params.get("facet")):
        return not isinstance(prev.get("de_data"), dict) and not isinstance(curr.get("de_data"), dict)
    return False


def load_inferred_rules(path: str = INFERRED_RULES_PATH) -> list[dict]:
    """Rules from inferred_rules.json (cached per path); [] if the file is missing."""
    if path not in _RULES_CACHE:
        p = Path(path)
        _RULES_CACHE[path] = json.loads(p.read_text(encoding="utf-8"))["rules"] if p.exists() else []
    return _RULES_CACHE[path]


def evaluate_inferred_rules(prev: dict | None, curr: dict | None, action: str,
                            path: str = INFERRED_RULES_PATH, pre_edit: dict | None = None) -> list:
    """Check every promoted rule whose trigger matches this step.
    prev must be the full previous snapshot, since the tab is read from it; without it only '*' rules run.

    pre_edit: the snapshot from before the edit that prev records. If this step is an undo right after such an edit,
    the undo_inverse rules compare the state after the undo (curr) with pre_edit, as they were mined (_undo_pair);
    without pre_edit they cannot be checked and are skipped.
    """
    from tests.invariants.invariants_core import InvariantResult

    prev, curr = prev or {}, dict(curr or {})
    curr.setdefault("action", action)
    category = _tabbed_category(prev, {"action": action})
    # A rule can be limited to the first or to the later uses of its category in a run ('...#first' / '...#later')
    stratum = _use_stratum(curr)
    applicable = ("*", category) + ((f"{category}#{stratum}",) if stratum else ())

    undo_pair = _undo_pair(pre_edit, prev, curr)

    results = []
    for rule in load_inferred_rules(path):
        if rule["template"] == "undo_inverse":
            # Not a per-action category: it applies to an undo that directly follows an edit (see _undo_pair)
            if undo_pair is None or rule["category"] not in ("*", UNDO_INVERSE_CATEGORY):
                continue
            observations = RULES[rule["template"]](undo_pair[0], undo_pair[1], **rule["params"])
        elif rule["category"] not in applicable:
            continue
        elif _not_applicable(rule, prev, curr):
            record_rule(rule["id"], "no_observations")
            continue
        else:
            observations = RULES[rule["template"]](prev, curr, **rule["params"])
        if not observations:
            record_rule(rule["id"], "no_observations")
            continue
        expect = rule["direction"] == "positive"
        violations = [d for held, d in observations if held != expect]
        passed = not violations
        record_rule(rule["id"], "pass" if passed else "violation")
        name = f"{rule['id']} {rule['statement']}"
        if passed:
            message = f"{rule['statement']} ✓"
        else:
            message = f"{rule['statement']} - violated ({len(violations)}/{len(observations)} observation(s))"
            if rule.get("promotion") == "learned_bug":
                message += f" [learned bug {rule['bug']}: this behaviour was flagged as a bug]" if rule.get("bug") else " [learned bug: this behaviour was flagged as a bug]"
            elif rule.get("promotion") == "manual":
                message += (f" [accepted by hand below the threshold ({rule.get('verdict', '?')}); "
                            f"minimum support {rule.get('min_support', 0.0):.0%}]")
            elif rule.get("promotion") == "insufficient_evidence":
                message += (f" [accepted; not promotable in the latest mining only because some dataset has too few "
                            f"observations (the observed datasets clear the threshold); "
                            f"minimum support {rule.get('min_support', 0.0):.0%}]")
            elif rule.get("min_support", 1.0) < 1.0:
                message += (f" [promoted at {rule['min_support']:.0%} minimum support: "
                            f"known exceptions exist in the traces]")
            from tests.mutation_testing.inference.rule_history import accepted_note
            if (since := accepted_note(rule["id"])):
                message += f" [{since}]"
        results.append(InvariantResult(
            name, passed, message,
            {} if passed else {"violations": violations[:3], "asserts": rule["card"]["asserts"],
                               "definitions": rule["card"].get("definitions")},
            evidence=[f"rule={rule['id']}", f"action={action}", f"category={category}",
                      f"asserts: {rule['card']['asserts']}"],
        ))
    return results