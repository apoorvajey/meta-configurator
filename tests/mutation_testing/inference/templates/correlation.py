"""Template correlation: if the keys of facet A change, the keys of facet B change."""
from __future__ import annotations

from tests.mutation_testing.inference.facets import FACETS
from tests.mutation_testing.inference.common import Observation, _keys


def rule_correlation(before: dict, after: dict, a: str, b: str) -> list[Observation]:
    ka0, ka1 = _keys(FACETS[a](before)), _keys(FACETS[a](after))
    if ka0 == ka1:
        return []
    kb0, kb1 = _keys(FACETS[b](before)), _keys(FACETS[b](after))
    return [(kb0 != kb1, {
        f"{a} +": sorted(ka1 - ka0)[:5], f"{a} -": sorted(ka0 - ka1)[:5],
        f"{b} +": sorted(kb1 - kb0)[:5], f"{b} -": sorted(kb0 - kb1)[:5],
    })]


NAME = "correlation"
RULE = rule_correlation
