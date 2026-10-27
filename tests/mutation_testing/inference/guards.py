"""Guards: a condition under which a template's rule is evaluated.

A guarded rule is the same template with params["guard"] = <name>. templates/__init__.py wraps every RULE so the
guard is checked first, so mining and checking (evaluate_inferred_rules) apply it identically. A guard
guard(before, after) returns True | False | None; only True lets the rule run, False and None (no reading, as in a
trace recorded before the field existed) give no observation, never "held".

The engine counts guarded variants in the same pass as the unguarded rule and keeps one only if it is promotable
and the unguarded rule is not (InvariantInferenceEngine._prune_guarded). A guard must not be false for a whole
dataset: promotion needs observations in every dataset.
"""
from __future__ import annotations

from typing import Callable

Guard = Callable[[dict, dict], "bool | None"]


def _schema_valid(before: dict, after: dict) -> bool | None:
    value = after.get("schema_valid_meta")
    return value if isinstance(value, bool) else None


GUARDS: dict[str, Guard] = {
    "schema_valid": _schema_valid,
}


# Completes "<rule statement> when ..." in rule statements.
GUARD_STATEMENTS = {
    "schema_valid": "the edited schema is valid",
}


GUARD_DEFINITIONS = {
    "schema_valid": "snapshot['schema_valid_meta'] is True after the step (the edited schema passes the meta-schema, "
                    "see tests/invariants/schema_validity.py); a step without that reading is skipped",
}


# Which guards are tried for which template. With an invalid schema the app shows no validation icons, throws in
# the GUI and can stay on the spinner, so these templates hold only while the schema is valid.
# A template that is not listed is never guarded.
GUARDABLE: dict[str, list[str]] = {
    "gui_row_set":              ["schema_valid"],
    "gui_rows_complete":        ["schema_valid"],
    "gui_validation_agreement": ["schema_valid"],
    "gui_required_agreement":   ["schema_valid"],
    "gui_value_agreement":      ["schema_valid"],
    "gui_renders":              ["schema_valid"],
    "app_health":               ["schema_valid"],
}