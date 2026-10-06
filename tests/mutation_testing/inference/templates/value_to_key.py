"""Template value_to_key: each key newly added to A is a top-level key of B afterwards."""
from __future__ import annotations

from tests.mutation_testing.inference.facets import FACETS
from tests.mutation_testing.inference.common import Observation, _keys


def rule_value_to_key(before: dict, after: dict, a: str, b: str) -> list[Observation]:
    added = _keys(FACETS[a](after)) - _keys(FACETS[a](before))
    kb1 = _keys(FACETS[b](after))
    return [(key in kb1, {"key": key}) for key in sorted(added)]


NAME = "value_to_key"
RULE = rule_value_to_key
