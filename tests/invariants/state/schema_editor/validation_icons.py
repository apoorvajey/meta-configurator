"""INV-S4."""
from __future__ import annotations
from tests.invariants.schema_validity import schema_meta_violations
from tests.invariants.invariants_core import InvariantResult

ID = "INV-S4"
NAME = "_inv_s4_gui_validation_icons_match_meta_schema"

_ICON_WAIT_MS = 3000   # how long the icons may disagree with the meta-schema before it counts as a failure
_ICON_POLL_MS = 250


def check(ctx) -> InvariantResult:
    """The Schema GUI shows a validation-error-icon exactly when the edited schema violates the meta-schema.

    Oracle: schema_meta_violations(). Granularity is deliberately coarse (any icon vs. none): the icon sits on
    the keyword row (e.g. 'properties') while a meta-schema oneOf reports the parent subschema, so path-level
    matching would give false mismatches. The Schema panel uses the same 'validation-error-icon' test id as
    the Data panel.

    Selector: validation-error-icon (anywhere in the Schema GUI View)
    """
    name = "INV-S4 Schema GUI validation icons match meta-schema violations"
    schema = ctx.tp_data.get_schema()

    if not isinstance(schema, dict):
        return InvariantResult(name, True,
                               "SKIPPED: schema is not an object - see INV-S0", skipped=True)

    violations = schema_meta_violations(ctx.page, schema, include_app_compile_problems=False)
    if violations is None:
        return InvariantResult(name, True,
                               "SKIPPED: validity could not be determined", skipped=True)

    should_show = bool(violations)
    # The GUI draws its icons after the app has updated the schema, so an icon may appear (or go away) a moment
    # late. Wait for the icons to agree with the meta-schema before reporting a mismatch, and note the delay.
    waited_ms = 0
    while True:
        icons = ctx.page.get_by_test_id("validation-error-icon").count()
        shows = icons > 0
        passed = should_show == shows
        if passed or waited_ms >= _ICON_WAIT_MS:
            break
        ctx.page.wait_for_timeout(_ICON_POLL_MS)
        waited_ms += _ICON_POLL_MS

    evidence = [f"meta-schema violations: {len(violations)}, validation icons shown: {icons} -> "
                f"{'AGREE ✓' if passed else 'DISAGREE ✗'}"]
    if waited_ms and passed:
        evidence.append(f"GUI icons took ~{waited_ms} ms to agree with the meta-schema (render lag, not a mismatch)")
    if passed:
        return InvariantResult(name, True,
                               "Schema GUI icons agree with meta-schema validity ✓",
                               evidence=evidence)
    message = ("Schema is invalid against the meta-schema but the Schema GUI shows no validation icon"
               if should_show else
               f"Schema is valid against the meta-schema but the Schema GUI shows {icons} validation icon(s)")
    message += f" (still so after {waited_ms} ms)"
    return InvariantResult(name, False, message,
                           details={"meta_violations": violations[:5], "icons_in_schema_gui": icons},
                           evidence=evidence)