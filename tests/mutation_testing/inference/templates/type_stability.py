"""Template type_stability: no value of the data (or the schema) changes its JSON type during the step.

Types are object, array, string, number (int and float alike), boolean and null. Object keys are matched by name. Array items are
matched by index, and only when the array keeps its length (after an insert or a removal the indexes no longer line up, and
comparing them would blame the action for shifts). A value that appears or disappears is not a type change.
"""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation
from tests.mutation_testing.inference.json_edit_distance import json_type

# target -> the snapshot field it reads
TARGET_FIELDS = {"data": "de_data", "schema": "de_schema"}


def type_changes(a, b) -> list[tuple[str, str, str]]:
    """(path, type before, type after) for every value whose JSON type differs between a and b."""
    out: list[tuple[str, str, str]] = []

    def walk(x, y, path: str) -> None:
        tx, ty = json_type(x), json_type(y)
        if tx != ty:
            out.append((path or "(root)", tx, ty))
        elif tx == "object":
            for k in x.keys() & y.keys():
                walk(x[k], y[k], f"{path}.{k}" if path else str(k))
        elif tx == "array" and len(x) == len(y):
            for i, (xi, yi) in enumerate(zip(x, y)):
                walk(xi, yi, f"{path}[{i}]")

    walk(a, b, "")
    return out


def rule_type_stability(before: dict, after: dict, target: str) -> list[Observation]:
    field = TARGET_FIELDS[target]
    if field not in before or field not in after:  # a snapshot without the field: nothing to compare
        return []
    changes = type_changes(before[field], after[field])
    if not changes:
        return [(True, {})]
    return [(False, {"type_changes": len(changes),
                     "changes": [f"{p}: {x} -> {y}" for p, x, y in sorted(changes)[:5]]})]


NAME = "type_stability"
RULE = rule_type_stability
