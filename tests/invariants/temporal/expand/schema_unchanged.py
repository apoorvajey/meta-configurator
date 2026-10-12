"""TEMP-EXPAND-02."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_expand

ID = "TEMP-EXPAND-02"
NAME = "_inv_temp05_expand_preserves_schema"


def check(ctx) -> InvariantResult | None:
    """Expanding a tree node does not modify the schema."""
    if not _is_expand(ctx.action):
        return None

    name = "TEMP-EXPAND-02 expand preserves schema"
    passed = ctx.prev_schema == ctx.curr_schema

    return InvariantResult(
        name, passed,
        "schema unchanged after expand ✓" if passed
        else "schema changed after expand - unexpected",
        {} if passed else {
            "prev_prop_keys": sorted(ctx.prev_prop_keys),
            "curr_prop_keys": sorted(ctx.curr_prop_keys),
        },
        evidence=[f"action={ctx.action}",
                  f"→ {'schema unchanged ✓' if passed else 'schema changed ✗'}"],
    )
