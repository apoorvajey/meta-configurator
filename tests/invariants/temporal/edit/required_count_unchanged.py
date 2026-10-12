"""TEMP-EDIT-03."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_edit_field

ID = "TEMP-EDIT-03"
NAME = "_inv_temp03_edit_field_preserves_required_count"


def check(ctx) -> InvariantResult | None:
    """Editing a field value never changes schema.required. It only changes through
    add-item-required or remove-property.
    """
    if not _is_edit_field(ctx.action):
        return None

    name = "TEMP-EDIT-03 edit-field preserves schema.required count"
    prev_count = len(ctx.prev_schema.get("required", []))
    curr_count = len(ctx.curr_schema.get("required", []))
    passed = prev_count == curr_count

    return InvariantResult(
        name, passed,
        f"schema.required count unchanged after edit-field ({curr_count}) ✓" if passed
        else f"schema.required count changed after edit-field: {prev_count} → {curr_count}",
        {} if passed else {"before": prev_count, "after": curr_count},
        evidence=[
            f"action={ctx.action}",
            f"schema.required: {prev_count} → {curr_count}",
            f"→ {'unchanged ✓' if passed else f'delta={curr_count - prev_count} ✗'}",
        ],
    )
