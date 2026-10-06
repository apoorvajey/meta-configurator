"""TEMP-MODE-01. Loaded by temporal_invariants.load_temporal_plugins()."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_mode_switch

ID = "TEMP-MODE-01"
NAME = "_inv_new05_mode_switch_preserves_data"


def check(ctx) -> InvariantResult | None:
    """TEMP-MODE-01: switching between Data Editor and Schema Editor does not modify the user data."""
    if not _is_mode_switch(ctx.action):
        return None

    name = "TEMP-MODE-01 mode switch preserves data"
    passed = ctx.prev_data == ctx.curr_data

    return InvariantResult(
        name, passed,
        "data unchanged after mode switch ✓" if passed
        else "data changed after mode switch - unexpected",
        {} if passed else {
            "prev_data_keys": sorted(ctx.prev_data_keys),
            "curr_data_keys": sorted(ctx.curr_data_keys),
        },
        evidence=[
            f"action={ctx.action}",
            f"→ {'data unchanged ✓' if passed else 'data changed ✗'}",
        ],
    )
