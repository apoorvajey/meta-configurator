"""Template path_key: a key added or removed in A is added or removed in B too."""
from __future__ import annotations

from tests.mutation_testing.inference.facets import FACETS
from tests.mutation_testing.inference.common import Observation, _keys


def rule_path_key(before: dict, after: dict, a: str, b: str) -> list[Observation]:
    ka0, ka1 = _keys(FACETS[a](before)), _keys(FACETS[a](after))
    a_add, a_rem = ka1 - ka0, ka0 - ka1
    if not a_add and not a_rem:
        return []
    kb0, kb1 = _keys(FACETS[b](before)), _keys(FACETS[b](after))
    b_add, b_rem = kb1 - kb0, kb0 - kb1
    return [(bool((a_add & b_add) or (a_rem & b_rem)), {
        f"{a} +": sorted(a_add)[:5], f"{a} -": sorted(a_rem)[:5],
        f"{b} +": sorted(b_add)[:5], f"{b} -": sorted(b_rem)[:5],
    })]


NAME = "path_key"
RULE = rule_path_key
