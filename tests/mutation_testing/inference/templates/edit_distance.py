"""Template edit_distance: the data (or the schema) is a bounded number of JSON edits away from its state before the step.

The distance is the top-down JEDI-style edit distance of inference/json_edit_distance.py (insert, delete and rename of a node,
objects unordered, arrays ordered). Two modes: "exact" (the distance is k) and "at_most" (it is at most k). A step that does
nothing has distance 0, which breaks "exact" but not "at_most", so both are mined. k is chosen by the engine from the traces (the
most common non-zero distance of the category) and agreed across datasets like change_count's k; work_limit comes from
inference_config.json (edit_distance_work_limit). A pair too expensive to compare gives no observation.
"""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation
from tests.mutation_testing.inference.json_edit_distance import edit_distance
from tests.mutation_testing.inference.memo import memoized

# facet -> the snapshot field it reads
FACET_FIELDS = {"data": "de_data", "schema": "de_schema"}
MODES = ("exact", "at_most")


def pair_distance(before: dict, after: dict, facet: str, work_limit: int) -> int | None:
    """Distance of the facet between two snapshots, or None if a snapshot lacks the field or the work limit is exceeded."""
    field = FACET_FIELDS[facet]
    if field not in before or field not in after:
        return None
    return memoized((id(before), id(after), facet, work_limit),
                    lambda: edit_distance(before[field], after[field], work_limit))


def rule_edit_distance(before: dict, after: dict, facet: str, mode: str, k: int, work_limit: int) -> list[Observation]:
    if mode not in MODES:
        raise ValueError(f"edit_distance: unknown mode {mode!r}")
    d = pair_distance(before, after, facet, work_limit)
    if d is None:
        return []
    return [(d == k if mode == "exact" else d <= k, {"edit_distance": d})]


NAME = "edit_distance"
RULE = rule_edit_distance
