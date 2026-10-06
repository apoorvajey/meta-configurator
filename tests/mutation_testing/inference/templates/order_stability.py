"""Template order_stability: the order of schema.required is unchanged."""
from __future__ import annotations

from tests.mutation_testing.inference.facets import ORDERED_FACETS
from tests.mutation_testing.inference.common import Observation


def rule_order_stability(before: dict, after: dict, facet: str) -> list[Observation]:
    """Raw order of a list-valued facet is unchanged. The other rules see only order-blind versions of it.

    Only meaningful when the set of elements did not change, otherwise a reorder and a real edit cannot be told apart.
    """
    a, b = ORDERED_FACETS[facet](before), ORDERED_FACETS[facet](after)
    if not a or set(a) != set(b):
        return []
    same_order = a == b
    return [(same_order, {} if same_order else {"before": a[:10], "after": b[:10]})]


NAME = "order_stability"
RULE = rule_order_stability
