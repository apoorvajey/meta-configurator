"""TEMP-RENAME-02. Loaded by temporal_invariants.load_temporal_plugins()."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_rename

ID = "TEMP-RENAME-02"
NAME = "_inv_temp09_rename_preserves_schema_property_count"


def check(ctx) -> InvariantResult | None:
    """TEMP-RENAME-02: renaming a property does not add or remove schema.properties entries."""
    if not _is_rename(ctx.action):
        return None

    name = "TEMP-RENAME-02 rename preserves schema.properties count"
    prev_count = len(ctx.prev_prop_keys)
    curr_count = len(ctx.curr_prop_keys)
    passed = prev_count == curr_count

    return InvariantResult(
        name, passed,
        f"schema.properties count unchanged after rename ({curr_count}) ✓" if passed
        else f"schema.properties count changed: {prev_count} → {curr_count}",
        {} if passed else {"before": prev_count, "after": curr_count},
        evidence=[f"action={ctx.action}",
                  f"→ {'count unchanged ✓' if passed else f'{prev_count}→{curr_count} ✗'}"],
    )
