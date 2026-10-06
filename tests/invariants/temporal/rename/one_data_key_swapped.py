"""TEMP-RENAME-04. Loaded by temporal_invariants.load_temporal_plugins()."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_rename

ID = "TEMP-RENAME-04"
NAME = "_inv_new02_rename_swaps_data_key"


def check(ctx) -> InvariantResult | None:
    """TEMP-RENAME-04: a rename removes one key from data and adds one. A nested rename changes no
    top-level key, which is also valid.
    """
    if not _is_rename(ctx.action) or ctx.prev_step == 0:
        return None

    name = "TEMP-RENAME-04 rename swaps exactly one data key"
    added   = ctx.curr_data_keys - ctx.prev_data_keys
    removed = ctx.prev_data_keys - ctx.curr_data_keys

    # valid: one swap or no change (nested rename)
    passed = len(added) == len(removed) and len(added) <= 1

    return InvariantResult(
        name, passed,
        "data key swap is clean after rename ✓" if passed
        else "data had unexpected key changes after rename",
        {} if passed else {"added": sorted(added), "removed": sorted(removed)},
        evidence=[
            f"action={ctx.action}",
            f"added={sorted(added)} removed={sorted(removed)}",
            f"→ {'clean swap ✓' if passed else 'unexpected change ✗'}",
        ],
    )
