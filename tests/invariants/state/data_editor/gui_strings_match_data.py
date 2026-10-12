"""INV-D5."""
from __future__ import annotations
from tests.shared.python.utils_gui_editor import read_string_property
from tests.invariants.invariants_core import InvariantResult

ID = "INV-D5"
NAME = "_inv_d5_gui_string_values_match_data"


def check(ctx) -> InvariantResult:
    """For each top-level string property, the GUI text field shows the same value as the internal state.

    See checkStringProperty() in panelGuiEditor.spec.ts. Selector: property-data-{key} > textbox.
    """
    name = "INV-D5 GUI string values match data"
    data = ctx.tp.get_data()

    if not isinstance(data, dict):
        return InvariantResult(name, True,
                               "Data is not an object - skip")

    schema = ctx.tp.get_schema()
    props = schema.get("properties", {})

    string_props = {
        k: v for k, v in data.items()
        if isinstance(v, str)
           and props.get(k, {}).get("type") == "string"
    }

    if not string_props:
        return InvariantResult(name, True,
                               "No top-level string properties - skip")

    mismatches = []
    for key, expected in string_props.items():
        actual = read_string_property(ctx.page, [key])
        if actual is None:
            continue  # not visible, caught by INV-D4
        if actual != expected:
            mismatches.append({
                "property": key,
                "internal_state": expected,
                "gui_shows": actual,
            })

    passed = not mismatches
    evidence = []
    for key, expected in string_props.items():
        actual = read_string_property(ctx.page, [key])
        if actual is None:
            evidence.append(f"{key}: not visible - skipped")
        elif actual == expected:
            evidence.append(f"{key}: GUI='{actual}' matches data ✓")
        else:
            evidence.append(f"{key}: GUI='{actual}' ≠ data='{expected}' ✗")
    return InvariantResult(
        name, passed,
        "All GUI string values match data ✓" if passed
        else f"{len(mismatches)} string value mismatch(es)",
        {} if passed else {"mismatches": mismatches},
        evidence=evidence,
    )
