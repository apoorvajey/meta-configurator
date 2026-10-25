"""INV-S0."""
from __future__ import annotations
from tests.invariants.invariants_core import InvariantResult

ID = "INV-S0"
NAME = "_inv_s0_schema_state_is_an_object"


def check(ctx) -> InvariantResult:
    """The schema must always be an object (or a boolean, which is valid whole-schema shorthand).
    Checked on its own because INV-S1, S2 and S3 assume a dict.

    A root-level change-type action in the Schema Editor Text View can collapse the schema to a bare
    scalar. TEMP-ANY-01 is the temporal counterpart.
    """
    name = "INV-S0 schema state is an object"
    schema = ctx.tp_data.get_schema()
    passed = isinstance(schema, dict) or isinstance(schema, bool)

    return InvariantResult(
        name, passed,
        "schema is an object ✓" if passed
        else f"schema is not an object (or boolean) - got "
             f"{type(schema).__name__}: {str(schema)[:100]}",
        {} if passed else {
            "actual_type": type(schema).__name__,
            "actual_value": str(schema)[:200],
        },
    )