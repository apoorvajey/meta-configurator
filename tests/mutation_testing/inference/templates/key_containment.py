"""Template key_containment: after the step, the keys of A are a subset of the keys of B."""
from __future__ import annotations

from tests.mutation_testing.inference.facets import FACETS
from tests.mutation_testing.inference.common import Observation, _keys


def rule_key_containment(before: dict, after: dict, a: str, b: str) -> list[Observation]:
    ka = _keys(FACETS[a](after))
    if not ka:
        return []
    missing = sorted(ka - _keys(FACETS[b](after)))
    return [(not missing, {} if not missing else {"missing": missing[:5]})]


NAME = "key_containment"
RULE = rule_key_containment
