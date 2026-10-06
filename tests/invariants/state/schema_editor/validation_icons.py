"""INV-S4. Loaded by invariants_core.load_invariant_plugins()."""
from __future__ import annotations
from tests.invariants.schema_validity import schema_meta_violations
from tests.invariants.invariants_core import InvariantResult

ID = "INV-S4"
NAME = "_inv_s4_gui_validation_icons_match_meta_schema"


def check(ctx) -> InvariantResult:
    """The Schema GUI shows a validation-error-icon exactly when the schema being edited violates the
    meta-schema.

    Oracle: schema_meta_violations() - the same meta-schema the Schema Editor itself validates against.
    Granularity is deliberately coarse (any icon vs. none): the icon sits on the keyword row
    (e.g. 'properties') while a oneOf in the meta-schema reports the parent subschema, so path-level
    matching would produce false mismatches.

    Assumes the Schema panel uses the same 'validation-error-icon' test id as the Data panel. If an invalid
    schema fails here with zero icons, check first that the offending row is not inside a collapsed node.

    Selector: validation-error-icon (anywhere in the Schema GUI View)
    """
    name = "INV-S4 Schema GUI validation icons match meta-schema violations"
    schema = ctx.tp_data.get_schema()

    if not isinstance(schema, dict):
        return InvariantResult(name, True,
                               "SKIPPED: schema is not an object - see INV-S0", skipped=True)

    violations = schema_meta_violations(ctx.page, schema)
    if violations is None:
        return InvariantResult(name, True,
                               "SKIPPED: validity could not be determined", skipped=True)

    icons = ctx.page.get_by_test_id("validation-error-icon").count()
    should_show = bool(violations)
    shows = icons > 0
    passed = should_show == shows

    evidence = [f"meta-schema violations: {len(violations)}, validation icons shown: {icons} -> "
                f"{'AGREE ✓' if passed else 'DISAGREE ✗'}"]
    if passed:
        return InvariantResult(name, True,
                               "Schema GUI icons agree with meta-schema validity ✓",
                               evidence=evidence)
    message = ("Schema is invalid against the meta-schema but the Schema GUI shows no validation icon"
               if should_show else
               f"Schema is valid against the meta-schema but the Schema GUI shows {icons} validation icon(s)")
    return InvariantResult(name, False, message,
                           details={"meta_violations": violations[:5], "icons_in_schema_gui": icons},
                           evidence=evidence)
