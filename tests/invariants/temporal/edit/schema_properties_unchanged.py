"""TEMP-EDIT-01."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_edit_field

ID = "TEMP-EDIT-01"
NAME = "_inv_temp01_edit_field_preserves_schema_properties"


def check(ctx) -> InvariantResult | None:
    """Editing a field value never adds or removes keys in schema.properties."""
    if not _is_edit_field(ctx.action):
        return None

    name = "TEMP-EDIT-01 edit-field preserves schema.properties.keys()"
    passed = ctx.prev_prop_keys == ctx.curr_prop_keys
    added   = sorted(ctx.curr_prop_keys - ctx.prev_prop_keys)
    removed = sorted(ctx.prev_prop_keys - ctx.curr_prop_keys)

    return InvariantResult(
        name, passed,
        "schema.properties.keys() unchanged after edit-field ✓" if passed
        else "schema.properties.keys() changed after edit-field",
        {} if passed else {"added": added, "removed": removed},
        evidence=[
            f"action={ctx.action}",
            f"→ {'unchanged ✓' if passed else f'added={added} removed={removed} ✗'}",
        ],
    )
