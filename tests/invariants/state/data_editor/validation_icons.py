"""INV-D6. Loaded by invariants_core.load_invariant_plugins()."""
from __future__ import annotations
from jsonschema import RefResolver
from jsonschema import Draft202012Validator, FormatChecker
from tests.invariants.schema_validity import schema_meta_violations
from tests.invariants.invariants_core import InvariantResult, _path_to_testid_string, _value_violates_schema

ID = "INV-D6"
NAME = "_inv_d6_gui_validation_icons_match_violations"


def check(ctx) -> InvariantResult:
    """For each top-level property, the GUI shows a validation-error-icon iff the value violates the
    schema.

    See checkPropertySchemaViolation() in panelGuiEditor.spec.ts. Selector: property-metadata-{path} >
    validation-error-icon.
    """

    name = "INV-D6 GUI validation icons match violations"
    data = ctx.tp.get_data()

    if not isinstance(data, dict):
        return InvariantResult(name, True,
                               "Data is not an object - skip", skipped=True)

    schema = ctx.tp.get_schema()

    # The schema must be valid against the meta-schema. The app compiles the whole schema and enforces
    # nothing if that fails, while the reference check below validates one property's subschema only.
    # With an invalid schema the expected icon state is undefined. The evidence is kept: no error icons
    # at all in the data view while the schema is invalid means the app ignores an invalid schema.
    meta_violations = schema_meta_violations(ctx.page, schema)
    if meta_violations is None:
        return InvariantResult(name, True, "SKIPPED: schema validity could not be determined",
                               skipped=True)
    if meta_violations:
        icons_in_data_gui = ctx.page.get_by_test_id("validation-error-icon").count()
        return InvariantResult(
            name, True,
            f"SKIPPED: schema is invalid against the meta-schema "
            f"({len(meta_violations)} violation(s)) - icons not comparable",
            details={"schema_meta_violations": meta_violations[:5],
                     "data_gui_error_icons": icons_in_data_gui,
                     "observation": "data view validates against an invalid schema"},
            skipped=True,
        )

    props = schema.get("properties", {})

    mismatches = []
    evidence = []

    for key, value in data.items():
        prop_schema = props.get(key)
        if prop_schema is None:
            continue

        should_have_error = _value_violates_schema(value, prop_schema, root_schema=schema)
        icon_state = ctx._property_has_error_icon(key)
        gui_shows_error = icon_state["icon_shown"]
        row_found = icon_state["row_found"]

        status = "AGREE ✓" if (should_have_error == gui_shows_error) else "DISAGREE ✗"
        expected = "violation expected" if should_have_error else "no violation expected"
        actual = "icon shown" if gui_shows_error else "no icon shown"

        # collect every violation with its path; validate() raises only the first
        violation_details = []
        if should_have_error:
            try:
                prop_schema_combined = {**prop_schema, "$defs": schema.get("$defs", {})}
                resolver = RefResolver.from_schema(schema)
                validator = Draft202012Validator(prop_schema_combined, resolver=resolver, format_checker=FormatChecker())
                all_errors = sorted(
                    validator.iter_errors(value),
                    key=lambda e: [str(p) for p in e.absolute_path],
                )
                for ve in all_errors:
                    path_str = _path_to_testid_string(list(ve.absolute_path)) or "(root)"
                    violation_details.append({"path": path_str, "message": ve.message})
            except Exception as e:
                violation_details.append({"path": "(unresolved)", "message": f"could not extract: {e}"})
        reason = "; ".join(f"at {d['path']}: {d['message']}" for d in violation_details)

        gui_messages = ctx._get_validation_error_messages(key) if gui_shows_error else []
        gui_msg_str = " | ".join(gui_messages) if gui_messages else ""

        suffix_parts = []
        if reason:
            suffix_parts.append(f"jsonschema:[{reason}]")
        if gui_msg_str:
            suffix_parts.append(f"gui:{gui_msg_str}")
        suffix = f" [{'; '.join(suffix_parts)}]" if suffix_parts else ""

        evidence.append(f"{key}: {expected}, {actual} → {status}{suffix}")

        if should_have_error != gui_shows_error:
            mismatches.append({
                "property": key,
                "value": value,
                "should_show_error": should_have_error,
                "gui_shows_error": gui_shows_error,
                "row_found": row_found,
                "reason": violation_details,
                "gui_messages": gui_messages,
            })

    if mismatches:
        return InvariantResult(
            name=name,
            passed=False,
            message=f"{len(mismatches)} validation icon mismatch(es)",
            details={"mismatches": mismatches},
            evidence=evidence,
        )
    return InvariantResult(
        name=name,
        passed=True,
        message="All validation icons correct ✓",
        evidence=evidence,
    )
