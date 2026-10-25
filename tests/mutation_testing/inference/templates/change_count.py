"""Template change_count: a facet changes in exactly k places."""
from __future__ import annotations

from deepdiff import DeepDiff

from typing import Any
from tests.mutation_testing.inference.facets import FACETS
from tests.mutation_testing.inference.common import Observation


def change_count(before_value: Any, after_value: Any) -> int:
    """Number of DeepDiff change entries (-1 if DeepDiff fails or a value is None: the facet is not recorded)."""
    if before_value is None or after_value is None:
        return -1
    if before_value == after_value:
        return 0
    if isinstance(before_value, dict) and isinstance(after_value, dict) and (
            not before_value or not after_value or not (before_value.keys() & after_value.keys())):
        return len(before_value) + len(after_value)
    try:
        diff = DeepDiff(before_value, after_value, ignore_order=True)
        return sum(len(v) for v in diff.values() if hasattr(v, "__len__"))
    except Exception:
        return -1


def change_summary(before_value, after_value, limit=6) -> dict:
    """Shown with a rule's examples: top-level keys added/removed, plus the first DeepDiff lines."""
    if not isinstance(before_value, dict) or not isinstance(after_value, dict):
        return {}
    b, a = set(before_value), set(after_value)
    out = {}
    if a - b: out["keys +"] = sorted(a - b)[:limit]
    if b - a: out["keys -"] = sorted(b - a)[:limit]
    try:
        out["diff"] = DeepDiff(before_value, after_value, ignore_order=True).pretty().splitlines()[:limit]
    except Exception:
        pass
    return out


def rule_change_count(before: dict, after: dict, facet: str, expected: int) -> list[Observation]:
    d = change_count(FACETS[facet](before), FACETS[facet](after))
    if d < 0:
        return []
    return [(d == expected, {"change_count": d, **change_summary(FACETS[facet](before), FACETS[facet](after))})]

NAME = "change_count"
RULE = rule_change_count