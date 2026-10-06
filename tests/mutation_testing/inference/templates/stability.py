"""Template stability: a facet is identical before and after the step."""
from __future__ import annotations

from tests.mutation_testing.inference.facets import FACETS
from tests.mutation_testing.inference.common import Observation, _MISSING, _short, schema_path_value


def rule_stability(before: dict, after: dict, facet: str | None = None,
                   path: list[str] | None = None) -> list[Observation]:
    if path is not None:
        a, b = schema_path_value(before, path), schema_path_value(after, path)
        if a is _MISSING and b is _MISSING:
            return []
    else:
        a, b = FACETS[facet](before), FACETS[facet](after)
    same = a == b
    return [(same, {} if same else {"before": _short(a), "after": _short(b)})]


NAME = "stability"
RULE = rule_stability
