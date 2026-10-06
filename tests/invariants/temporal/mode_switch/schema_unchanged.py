"""TEMP-MODE-02. Loaded by temporal_invariants.load_temporal_plugins()."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_mode_switch

ID = "TEMP-MODE-02"
NAME = "_inv_new06_mode_switch_preserves_schema"


def check(ctx) -> InvariantResult | None:
    """TEMP-MODE-02: switching between Data Editor and Schema Editor does not modify the schema."""
    if not _is_mode_switch(ctx.action):
        return None

    name = "TEMP-MODE-02 mode switch preserves schema"
    passed = ctx.prev_schema == ctx.curr_schema

    return InvariantResult(
        name, passed,
        "schema unchanged after mode switch ✓" if passed
        else "schema changed after mode switch - unexpected",
        {} if passed else {
            "prev_prop_keys": sorted(ctx.prev_prop_keys),
            "curr_prop_keys": sorted(ctx.curr_prop_keys),
        },
        evidence=[
            f"action={ctx.action}",
            f"→ {'schema unchanged ✓' if passed else 'schema changed ✗'}",
        ],
    )
