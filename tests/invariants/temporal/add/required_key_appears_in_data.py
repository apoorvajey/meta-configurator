"""TEMP-ADD-03."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_add_item_required

ID = "TEMP-ADD-03"
NAME = "_inv_temp_add03_add_item_required_propagates_to_data"


def check(ctx) -> InvariantResult | None:
    """After add-item-required, any new required key appears in data, whether or not
    it is defined in schema.properties.

    The app creates the data slot even when the key has no definition in schema.properties.
    """
    if not _is_add_item_required(ctx.action):
        return None

    name = "TEMP-ADD-03 add-item-required propagates to data"

    prev_required = set(ctx.prev_schema.get("required", []))
    curr_required = set(ctx.curr_schema.get("required", []))
    new_keys = curr_required - prev_required

    if not new_keys:
        return InvariantResult(name, True, "No new required keys - skip")

    curr_data_keys = set(ctx.curr_data.keys()) if isinstance(ctx.curr_data, dict) else set()
    curr_props = set(ctx.curr_schema.get("properties", {}).keys())  # informational only

    failures = []
    for key in new_keys:
        if key not in curr_data_keys:
            failures.append(f"'{key}' added to schema.required but missing from data")

    passed = not failures
    return InvariantResult(
        name, passed,
        "required key propagation to data is correct ✓" if passed
        else f"required key propagation broken: {failures}",
        {} if passed else {"failures": failures},
        evidence=[
            f"action={ctx.action}",
            f"new required keys={sorted(new_keys)}",
            f"defined in properties={sorted(new_keys & curr_props)}",
            f"→ {'correct ✓' if passed else 'broken ✗'}",
        ],
    )
