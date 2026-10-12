"""TEMP-EDIT-02."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_edit_field_data_editor

ID = "TEMP-EDIT-02"
NAME = "_inv_new03_edit_field_data_editor_preserves_schema"


def check(ctx) -> InvariantResult | None:
    """Editing a data field in Data Editor mode never modifies the schema. Schema Editor
    meta-fields (title, type, $ref, ...) are excluded because they edit the schema by design.
    """
    if not _is_edit_field_data_editor(ctx.action):
        return None

    name = "TEMP-EDIT-02 edit-field (DataEditor) preserves schema entirely"
    passed = ctx.prev_schema == ctx.curr_schema

    return InvariantResult(
        name, passed,
        "schema entirely unchanged after DataEditor edit-field ✓" if passed
        else "schema changed after DataEditor edit-field - unexpected",
        {} if passed else {
            "prev_prop_keys": sorted(ctx.prev_prop_keys),
            "curr_prop_keys": sorted(ctx.curr_prop_keys),
        },
        evidence=[
            f"action={ctx.action}",
            f"→ {'schema unchanged ✓' if passed else 'schema changed ✗'}",
        ],
    )
