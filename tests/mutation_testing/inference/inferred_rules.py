"""Loading and evaluation of the exported rules in inferred_rules.json."""
from __future__ import annotations

import json
from pathlib import Path
from tests.mutation_testing.inference.constants import INFERRED_RULES_PATH
from tests.mutation_testing.inference.templates import RULES
from tests.mutation_testing.inference.categories import _tabbed_category


_RULES_CACHE: dict[str, list[dict]] = {}


def load_inferred_rules(path: str = INFERRED_RULES_PATH) -> list[dict]:
    """Rules from inferred_rules.json (cached per path); [] if the file is missing."""
    if path not in _RULES_CACHE:
        p = Path(path)
        _RULES_CACHE[path] = json.loads(p.read_text(encoding="utf-8"))["rules"] if p.exists() else []
    return _RULES_CACHE[path]


def evaluate_inferred_rules(prev: dict | None, curr: dict | None, action: str,
                            path: str = INFERRED_RULES_PATH) -> list:
    """Check every promoted rule whose trigger matches this step.
    prev must be the full previous snapshot, since the tab is read from it; without it only '*' rules run.
    """
    from tests.invariants.invariants_core import InvariantResult

    prev, curr = prev or {}, dict(curr or {})
    curr.setdefault("action", action)
    category = _tabbed_category(prev, {"action": action})

    results = []
    for rule in load_inferred_rules(path):
        if rule["category"] not in ("*", category):
            continue
        observations = RULES[rule["template"]](prev, curr, **rule["params"])
        if not observations:
            continue
        expect = rule["direction"] == "positive"
        violations = [d for held, d in observations if held != expect]
        passed = not violations
        name = f"{rule['id']} {rule['statement']}"
        if passed:
            message = f"{rule['statement']} ✓"
        else:
            message = f"{rule['statement']} - violated ({len(violations)}/{len(observations)} observation(s))"
            if rule.get("min_support", 1.0) < 1.0:
                message += (f" [promoted at {rule['min_support']:.0%} minimum support: "
                            f"known exceptions exist in the traces]")
        results.append(InvariantResult(
            name, passed, message,
            {} if passed else {"violations": violations[:3], "asserts": rule["card"]["asserts"]},
            evidence=[f"rule={rule['id']}", f"action={action}", f"category={category}",
                      f"asserts: {rule['card']['asserts']}"],
        ))
    return results
