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
    try:
        diff = DeepDiff(before_value, after_value, ignore_order=True)
        return sum(len(v) for v in diff.values() if isinstance(v, (dict, set, list)))
    except Exception:
        return -1


def rule_change_count(before: dict, after: dict, facet: str, expected: int) -> list[Observation]:
    d = change_count(FACETS[facet](before), FACETS[facet](after))
    if d < 0:
        return []
    return [(d == expected, {"change_count": d})]


NAME = "change_count"
RULE = rule_change_count