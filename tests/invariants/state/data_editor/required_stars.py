"""INV-D3."""
from __future__ import annotations
from tests.shared.python.utils import path_to_string
from tests.shared.python.utils_gui_editor import read_required_star_visible
from tests.invariants.invariants_core import InvariantResult

ID = "INV-D3"
NAME = "_inv_d3_gui_required_stars_match_schema"

_BLANK_PREFIX = "GUI is blank"   # same wording as INV-D4, so the reports recognise the same root cause


def _gui_row_present(page, prop_name: str) -> bool:
    """True if the GUI renders a row for the property. If the check itself fails, assume the row exists."""
    try:
        return page.get_by_test_id(f"property-metadata-{path_to_string([prop_name])}").count() > 0
    except Exception:
        return True


def check(ctx) -> InvariantResult:
    """For each top-level property in schema.properties that has a GUI row, the GUI shows a required star
    iff the property is in schema.required.

    If no property has a row the GUI is blank and there is nothing to compare: that is reported as a blank GUI
    (the same root cause as INV-D4), not as one star mismatch per property.

    See checkPropertyRequired() in panelGuiEditor.spec.ts. Selector: property-metadata-{path} >
    required-star.
    """
    name = "INV-D3 GUI required-star icons match schema"
    schema = ctx.tp.get_schema()
    required = set(schema.get("required", []))
    properties = schema.get("properties", {})

    if not properties:
        return InvariantResult(name, True,
                               "No properties in schema - skip")

    has_row = {prop: _gui_row_present(ctx.page, prop) for prop in properties}

    if not any(has_row.values()):
        return InvariantResult(
            name, False,
            f"{_BLANK_PREFIX} - none of the {len(properties)} schema properties has a row in the GUI, "
            f"so required-star icons cannot be checked (same cause as INV-D4)",
            {},
            evidence=[f"{prop}: {'required' if prop in required else 'optional'} - no GUI row"
                      for prop in properties],
        )

    mismatches = []
    evidence = []
    for prop_name in properties:
        should_have_star = prop_name in required
        kind = "required" if should_have_star else "optional"
        if not has_row[prop_name]:
            evidence.append(f"{prop_name}: {kind} - no GUI row (not checked)")
            continue
        has_star = read_required_star_visible(ctx.page, [prop_name])
        evidence.append(f"{prop_name}: {kind} - {'star shown' if has_star else 'no star'}")
        if should_have_star != has_star:
            mismatches.append({
                "property": prop_name,
                "schema_says_required": should_have_star,
                "gui_shows_star": has_star,
            })

    passed = not mismatches
    return InvariantResult(
        name, passed,
        "All required-star icons consistent ✓" if passed
        else f"{len(mismatches)} required-star mismatch(es)",
        {} if passed else {"mismatches": mismatches},
        evidence=evidence,
    )