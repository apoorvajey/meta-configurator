"""TEMP-EXPAND-01."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_expand

ID = "TEMP-EXPAND-01"
NAME = "_inv_temp04_expand_preserves_data"


def check(ctx) -> InvariantResult | None:
    """Expanding a tree node is navigation only, so the data is identical before and after."""
    if not _is_expand(ctx.action):
        return None

    name = "TEMP-EXPAND-01 expand preserves data"
    passed = ctx.prev_data == ctx.curr_data

    return InvariantResult(
        name, passed,
        "data unchanged after expand ✓" if passed
        else "data changed after expand - unexpected",
        {} if passed else {
            "prev_data_keys": sorted(ctx.prev_data_keys),
            "curr_data_keys": sorted(ctx.curr_data_keys),
        },
        evidence=[f"action={ctx.action}",
                  f"→ {'data unchanged ✓' if passed else 'data changed ✗'}"],
    )
