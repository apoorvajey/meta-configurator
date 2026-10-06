"""INV-S3. Loaded by invariants_core.load_invariant_plugins()."""
from __future__ import annotations
from tests.shared.python.utils_gui_editor import read_string_property
from tests.invariants.invariants_core import InvariantResult

ID = "INV-S3"
NAME = "_inv_s3_schema_title_matches_internal"


def check(ctx) -> InvariantResult:
    """The 'title' shown in the Schema Editor GUI View must match the title in the internal schema. This
    also covers the toolbar, which reads the same state.

    Selector: property-data-title > textbox
    """
    name = "INV-S3 Schema title field matches internal schema"
    schema = ctx.tp_data.get_schema()

    if not isinstance(schema, dict):
        return InvariantResult(
            name, False,
            f"Cannot check title - schema is not an object "
            f"(got {type(schema).__name__}); see INV-S0",
            {"actual_type": type(schema).__name__},
        )

    expected_title = schema.get("title")

    if expected_title is None:
        return InvariantResult(name, True,
                               "Schema has no title - skip")

    if not isinstance(expected_title, str):
        return InvariantResult(name, True,
                               f"Schema title is not a string (got {type(expected_title).__name__}) - skip")

    actual_title = read_string_property(ctx.page, ["title"])

    if actual_title is None:
        return InvariantResult(name, False,
                               "Title field not visible in Schema GUI View",
                               {"expected": expected_title})

    passed = actual_title == expected_title
    evidence = [
        f"internal schema title='{expected_title}', "
        f"GUI shows='{actual_title}' → {'match ✓' if passed else 'mismatch ✗'}"
    ]
    return InvariantResult(
        name, passed,
        f"Schema title '{expected_title}' matches GUI ✓" if passed
        else "Schema title mismatch",
        {} if passed else {
            "internal_schema": expected_title,
            "gui_shows": actual_title,
        },
        evidence=evidence,
    )
