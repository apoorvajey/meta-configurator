"""TEMP-COLLAPSE-01. Loaded by temporal_invariants.load_temporal_plugins()."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_collapse

ID = "TEMP-COLLAPSE-01"
NAME = "_inv_temp07_collapse_preserves_schema"


def check(ctx) -> InvariantResult | None:
    """TEMP-COLLAPSE-01: collapsing a tree node does not modify the schema."""
    if not _is_collapse(ctx.action):
        return None

    name = "TEMP-COLLAPSE-01 collapse preserves schema"
    passed = ctx.prev_schema == ctx.curr_schema

    return InvariantResult(
        name, passed,
        "schema unchanged after collapse ✓" if passed
        else "schema changed after collapse - unexpected",
        {} if passed else {
            "prev_prop_keys": sorted(ctx.prev_prop_keys),
            "curr_prop_keys": sorted(ctx.curr_prop_keys),
        },
        evidence=[f"action={ctx.action}",
                  f"→ {'schema unchanged ✓' if passed else 'schema changed ✗'}"],
    )
