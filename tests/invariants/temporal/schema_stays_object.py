"""TEMP-ANY-01."""
from __future__ import annotations

from tests.invariants.invariants_core import InvariantResult

ID = "TEMP-ANY-01"
NAME = "_inv_new07_schema_state_remains_an_object"


def check(ctx) -> InvariantResult | None:
    """The schema state (de_schema) is always an object, never a bare scalar or array. Checked on every
    transition, because any action could corrupt it. Reads ctx.curr["de_schema"] directly: ctx.curr_schema
    coerces non-dicts to {}, which would hide the violation.
    """
    name = "TEMP-ANY-01 schema state remains an object"
    raw_curr_schema = ctx.curr.get("de_schema")
    passed = isinstance(raw_curr_schema, dict)

    return InvariantResult(
        name, passed,
        "schema is an object ✓" if passed
        else f"schema is no longer an object - got "
             f"{type(raw_curr_schema).__name__}: {str(raw_curr_schema)[:100]}",
        {} if passed else {
            "actual_type": type(raw_curr_schema).__name__,
            "actual_value": str(raw_curr_schema)[:200],
        },
        evidence=[
            f"action={ctx.action}",
            f"→ {'object ✓' if passed else 'NOT an object ✗'}",
        ],
    )