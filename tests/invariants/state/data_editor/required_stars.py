"""INV-D3. Loaded by invariants_core.load_invariant_plugins()."""
from __future__ import annotations
from tests.shared.python.utils_gui_editor import read_required_star_visible
from tests.invariants.invariants_core import InvariantResult

ID = "INV-D3"
NAME = "_inv_d3_gui_required_stars_match_schema"


def check(ctx) -> InvariantResult:
    """For each top-level property in schema.properties, the GUI shows a required star iff the property
    is in schema.required.

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

    mismatches = []
    for prop_name in properties:
        should_have_star = prop_name in required
        has_star = read_required_star_visible(ctx.page, [prop_name])
        if should_have_star != has_star:
            mismatches.append({
                "property": prop_name,
                "schema_says_required": should_have_star,
                "gui_shows_star": has_star,
            })

    passed = not mismatches
    evidence = [
        f"{prop}: {'required ✓' if prop in required else 'optional'} - "
        f"{'star shown' if read_required_star_visible(ctx.page, [prop]) else 'no star'}"
        for prop in properties
    ]
    return InvariantResult(
        name, passed,
        "All required-star icons consistent ✓" if passed
        else f"{len(mismatches)} required-star mismatch(es)",
        {} if passed else {"mismatches": mismatches},
        evidence=evidence,
    )
