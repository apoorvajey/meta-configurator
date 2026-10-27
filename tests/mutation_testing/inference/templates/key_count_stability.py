"""Template key_count_stability: the number of top-level keys of a facet is the same before and after the step."""
from __future__ import annotations

from tests.mutation_testing.inference.facets import FACETS
from tests.mutation_testing.inference.common import Observation


def rule_key_count_stability(before: dict, after: dict, facet: str) -> list[Observation]:
    a, b = FACETS[facet](before), FACETS[facet](after)
    if a is None or b is None:  # the facet is not recorded in this snapshot
        return []
    if len(a) == len(b):
        return [(True, {})]
    return [(False, {"keys before": len(a), "keys after": len(b),
                     f"{facet} +": sorted(set(b) - set(a))[:5], f"{facet} -": sorted(set(a) - set(b))[:5]})]


NAME = "key_count_stability"
RULE = rule_key_count_stability
