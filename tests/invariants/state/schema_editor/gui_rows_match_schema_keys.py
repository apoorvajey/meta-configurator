"""INV-S2."""
from __future__ import annotations
from tests.shared.python.utils_gui_editor import read_property_exists
from tests.invariants.invariants_core import InvariantResult

ID = "INV-S2"
NAME = "_inv_s2_gui_rows_exist_for_schema_keys"


def check(ctx) -> InvariantResult:
    """Every top-level schema key has a visible row in the Schema Editor GUI View (property-data-{key}; keys
    starting with '$' use the key name directly, e.g. property-data-$schema).

    A non-dict schema fails here instead of being treated as {}: skipping the per-key walk is not the same as
    all keys being present. INV-S0 reports the corruption itself.
    """
    name = "INV-S2 GUI rows exist for top-level schema keys"
    schema = ctx.tp.get_schema()

    if not isinstance(schema, dict):
        return InvariantResult(
            name, False,
            f"Cannot check GUI rows - schema is not an object "
            f"(got {type(schema).__name__}); see INV-S0",
            {"actual_type": type(schema).__name__, "actual_value": str(schema)[:200]},
        )

    if not schema:
        return InvariantResult(name, True,
                               "Schema is empty - skip")

    skip_keys = {"$schema"}  # rendered as a link/badge, not a property row

    missing_rows = [
        key for key in schema
        if key not in skip_keys
           and not read_property_exists(ctx.page, [key])
    ]

    passed = not missing_rows
    evidence = [
        f"{key}: {'row visible ✓' if key not in missing_rows and key not in skip_keys else ('skipped' if key in skip_keys else 'row missing ✗')}"
        for key in schema
    ]
    return InvariantResult(
        name, passed,
        "All schema keys have visible GUI rows ✓" if passed
        else f"Schema GUI missing rows for: {missing_rows}",
        {} if passed else {
            "schema_keys": list(schema.keys()),
            "missing_in_gui": missing_rows,
        },
        evidence=evidence,
    )
