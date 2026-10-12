"""TEMP-RENAME-01."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_rename

ID = "TEMP-RENAME-01"
NAME = "_inv_temp08_rename_preserves_data_key_count"


def check(ctx) -> InvariantResult | None:
    """Renaming a property does not add or remove top-level data keys."""
    if not _is_rename(ctx.action):
        return None

    name = "TEMP-RENAME-01 rename preserves data key count"
    prev_count = len(ctx.prev_data_keys)
    curr_count = len(ctx.curr_data_keys)
    passed = prev_count == curr_count

    return InvariantResult(
        name, passed,
        f"data key count unchanged after rename ({curr_count}) ✓" if passed
        else f"data key count changed after rename: {prev_count} → {curr_count}",
        {} if passed else {"before": prev_count, "after": curr_count},
        evidence=[f"action={ctx.action}",
                  f"→ {'count unchanged ✓' if passed else f'{prev_count}→{curr_count} ✗'}"],
    )
