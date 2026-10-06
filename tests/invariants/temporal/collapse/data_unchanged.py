"""TEMP-COLLAPSE-02. Loaded by temporal_invariants.load_temporal_plugins()."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_collapse

ID = "TEMP-COLLAPSE-02"
NAME = "_inv_temp06_collapse_preserves_data"


def check(ctx) -> InvariantResult | None:
    """TEMP-COLLAPSE-02: collapsing a tree node is navigation only, so the data is identical before and
    after.
    """
    if not _is_collapse(ctx.action):
        return None

    name = "TEMP-COLLAPSE-02 collapse preserves data"
    passed = ctx.prev_data == ctx.curr_data

    return InvariantResult(
        name, passed,
        "data unchanged after collapse ✓" if passed
        else "data changed after collapse - unexpected",
        {} if passed else {
            "prev_data_keys": sorted(ctx.prev_data_keys),
            "curr_data_keys": sorted(ctx.curr_data_keys),
        },
        evidence=[f"action={ctx.action}",
                  f"→ {'data unchanged ✓' if passed else 'data changed ✗'}"],
    )
