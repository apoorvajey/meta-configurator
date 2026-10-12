"""TEMP-RENAME-03."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult
from tests.invariants.temporal_invariants import _is_rename

ID = "TEMP-RENAME-03"
NAME = "_inv_new01_rename_swaps_schema_property_key"


def check(ctx) -> InvariantResult | None:
    """A rename removes one key from schema.properties and adds one. A nested rename
    changes no top-level key, which is also valid.
    """
    if not _is_rename(ctx.action) or ctx.prev_step == 0:
        return None

    name = "TEMP-RENAME-03 rename swaps exactly one schema.properties key"
    added   = ctx.curr_prop_keys - ctx.prev_prop_keys
    removed = ctx.prev_prop_keys - ctx.curr_prop_keys

    # valid: one swap (top-level rename) or no change (nested rename)
    passed = len(added) == len(removed) and len(added) <= 1

    return InvariantResult(
        name, passed,
        "schema.properties key swap is clean after rename ✓" if passed
        else "schema.properties had unexpected key changes after rename",
        {} if passed else {"added": sorted(added), "removed": sorted(removed)},
        evidence=[
            f"action={ctx.action}",
            f"added={sorted(added)} removed={sorted(removed)}",
            f"→ {'clean swap ✓' if passed else 'unexpected change ✗'}",
        ],
    )
