"""TEMP-ADD-01. Loaded by temporal_invariants.load_temporal_plugins()."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_add_item

ID = "TEMP-ADD-01"
NAME = "_inv_temp16_add_item_preserves_schema_properties"


def check(ctx) -> InvariantResult | None:
    """TEMP-ADD-01: adding an array item never adds or removes schema.properties keys."""
    if not _is_add_item(ctx.action):
        return None

    name = "TEMP-ADD-01 add-item preserves schema.properties.keys()"
    passed = ctx.prev_prop_keys == ctx.curr_prop_keys
    added   = sorted(ctx.curr_prop_keys - ctx.prev_prop_keys)
    removed = sorted(ctx.prev_prop_keys - ctx.curr_prop_keys)

    return InvariantResult(
        name, passed,
        "schema.properties.keys() unchanged after add-item ✓" if passed
        else "schema.properties.keys() changed after add-item - unexpected",
        {} if passed else {"added": added, "removed": removed},
        evidence=[f"action={ctx.action}",
                  f"→ {'unchanged ✓' if passed else f'added={added} removed={removed} ✗'}"],
    )
