"""Template root_type: the root value of the data (or of the schema) has a given JSON type."""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation
from tests.mutation_testing.inference.json_edit_distance import json_type

# store -> the snapshot field it reads
STORES = {"data": "de_data", "schema": "de_schema"}


def rule_root_type(before: dict, after: dict, store: str, value_type: str) -> list[Observation]:
    """One observation per step: the root of the store after the step has the JSON type `value_type`.
    A snapshot without the field gives none; a field that is present but null is the type 'null'."""
    field = STORES[store]
    if field not in after:
        return []
    actual = json_type(after[field])
    return [(actual == value_type, {} if actual == value_type else {"actual_type": actual, "value": str(after[field])[:80]})]


NAME = "root_type"
RULE = rule_root_type