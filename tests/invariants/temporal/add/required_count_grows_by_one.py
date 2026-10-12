"""TEMP-ADD-02."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_add_item_required

ID = "TEMP-ADD-02"
NAME = "_inv_new04_add_item_required_increases_required_count"


def check(ctx) -> InvariantResult | None:
    """Adding an item to the required array increases len(schema.required) by exactly 1."""
    if not _is_add_item_required(ctx.action):
        return None

    name = "TEMP-ADD-02 add-item-required increases required count by 1"
    prev_count = len(ctx.prev_schema.get("required", []))
    curr_count = len(ctx.curr_schema.get("required", []))
    passed = curr_count == prev_count + 1

    return InvariantResult(
        name, passed,
        f"required count increased by 1 ({prev_count} → {curr_count}) ✓" if passed
        else f"required count unexpected change: {prev_count} → {curr_count}",
        {} if passed else {"before": prev_count, "after": curr_count},
        evidence=[
            f"action={ctx.action}",
            f"required: {prev_count} → {curr_count}",
            f"→ {'+1 ✓' if passed else f'delta={curr_count - prev_count} ✗'}",
        ],
    )
