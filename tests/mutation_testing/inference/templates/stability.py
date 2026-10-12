"""Template stability: a facet is identical before and after the step."""
from __future__ import annotations

from tests.mutation_testing.inference.facets import FACETS
from tests.mutation_testing.inference.common import Observation, _MISSING, leaf_changes, schema_path_value


def rule_stability(before: dict, after: dict, facet: str | None = None,
                   path: list[str] | None = None) -> list[Observation]:
    if path is not None:
        a, b = schema_path_value(before, path), schema_path_value(after, path)
        if a is _MISSING and b is _MISSING:
            return []
    else:
        a, b = FACETS[facet](before), FACETS[facet](after)
        if a is None or b is None:  # the facet is not recorded in this snapshot: nothing to compare
            return []
    same = a == b
    if same:
        return [(True, {})]
    changes, total = leaf_changes(a, b)
    return [(False, {"changed_fields": total, "changes": changes})]


NAME = "stability"
RULE = rule_stability