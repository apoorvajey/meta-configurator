"""Per-rule activity counters of one mutation run, written to rule_activity.json next to history.json.

evaluate_inferred_rules() calls record_rule() for every rule that applies to a step:
    "no_observations"     the rule applies but found nothing to check
    "pass" / "violation"  the rule was evaluated; a violation counts as evaluated and as violated
Rules whose category does not apply to the step are not recorded. Nothing here may break a run: every function
catches its errors.
"""
from __future__ import annotations

import json
from pathlib import Path

FILENAME = "rule_activity.json"

_COUNTS: dict[str, dict[str, int]] = {}


def reset_rule_activity() -> None:
    """Start counting from zero (called once before a run's loop)."""
    _COUNTS.clear()


def record_rule(rule_id: str, outcome: str) -> None:
    try:
        c = _COUNTS.setdefault(rule_id, {"evaluated": 0, "no_observations": 0, "violated": 0})
        if outcome == "no_observations":
            c["no_observations"] += 1
        else:
            c["evaluated"] += 1
            if outcome == "violation":
                c["violated"] += 1
    except Exception:
        pass


def write_rule_activity(run_dir) -> None:
    """Write <run_dir>/rule_activity.json: every accepted rule with its counts (zeros for rules never seen)."""
    try:
        from tests.mutation_testing.inference.inferred_rules import load_inferred_rules
        zero = {"evaluated": 0, "no_observations": 0, "violated": 0}
        rules = {r["id"]: {"template": r["template"], "category": r["category"], **_COUNTS.get(r["id"], zero)}
                 for r in load_inferred_rules()}
        (Path(run_dir) / FILENAME).write_text(json.dumps({"rules": rules}, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[!] Could not write {FILENAME}: {e}")
