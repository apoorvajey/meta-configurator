"""INV-GUI-ERR-01. Loaded by invariants_core.load_invariant_plugins()."""
from __future__ import annotations
from tests.shared.python.utils_code_editor import read_text_editor_raw
from tests.invariants.invariants_core import InvariantResult, _ADDITIONAL_PROPS_MSG, _find_additional_properties_violations, _gui_error_icon_and_tooltip_for_path, _path_to_testid_string, _text_view_error_for_key

ID = "INV-GUI-ERR-01"
NAME = "_inv_gui_err_01_additional_properties_desync"


def check(ctx) -> InvariantResult:
    """An AJV additionalProperties violation shown in Text View must also appear as a
    validation-error-icon, with a matching tooltip, on the row in GUI View.

    Suspected desync: the generic fallback widget for keys with no schema node (which is what an
    additionalProperties: false violation is) may not use the same isInvalid() logic as schema-bound
    fields such as StringProperty.vue, which INV-D6 covers.

    The violating key is taken from comparing get_data() with get_schema() in Python
    (_find_additional_properties_violations), because the Text View display is what is under test.
    """
    name = "INV-GUI-ERR-01 GUI validation icon matches Text View additionalProperties error"

    data = ctx.tp.get_data()
    schema = ctx.tp.get_schema()
    violations = _find_additional_properties_violations(data, schema)

    if not violations:
        return InvariantResult(name, True,
                               "No additionalProperties violations present in current data — skip",
                               details={"violations_present": False})

    raw_text = read_text_editor_raw(ctx.page, ctx.mode)
    if raw_text is None:
        return InvariantResult(
            name, False,
            f"Text View could not be read at all - cannot verify "
            f"{len(violations)} known additionalProperties violation(s)",
            {"expected_violations": [{"path": p, "key": k} for p, k in violations]})

    mismatches = []
    evidence = []
    for parent_path, key in violations:
        full_path = parent_path + [key]
        label = _path_to_testid_string(full_path) or key

        text_matches = _text_view_error_for_key(ctx.page, ctx.mode, raw_text, key)
        if not text_matches:
            evidence.append(
                f"{label}: violation present in data, but no matching AJV error found in "
                f"Text View annotations - cannot compare (check the annotation-source "
                f"assumption in _get_ace_annotations() against the live app)")
            continue

        text_message = text_matches[0]["message"]
        gui = _gui_error_icon_and_tooltip_for_path(ctx.page, full_path)

        if not gui["row_found"]:
            evidence.append(f"{label}: Text View='{text_message}' but no GUI row found at all")
            mismatches.append({"path": full_path, "key": key, "text_view_message": text_message,
                               "gui_row_found": False, "gui_icon_shown": False, "gui_tooltip": None})
            continue

        if not gui["icon_shown"]:
            evidence.append(f"{label}: Text View='{text_message}' - GUI row exists but shows "
                            f"NO error indicator ✗ (suspected any-type fallback desync)")
            mismatches.append({"path": full_path, "key": key, "text_view_message": text_message,
                               "gui_row_found": True, "gui_icon_shown": False, "gui_tooltip": None})
            continue

        tooltip = gui["tooltip_text"] or ""
        if _ADDITIONAL_PROPS_MSG in tooltip:
            evidence.append(f"{label}: Text View='{text_message}', GUI icon+tooltip match ✓")
        else:
            evidence.append(f"{label}: Text View='{text_message}', GUI icon shown but "
                            f"tooltip='{tooltip}' doesn't match ✗")
            mismatches.append({"path": full_path, "key": key, "text_view_message": text_message,
                               "gui_row_found": True, "gui_icon_shown": True, "gui_tooltip": tooltip})

    passed = not mismatches
    message = ("All additionalProperties errors consistently shown in GUI View ✓" if passed
               else f"{len(mismatches)}/{len(violations)} additionalProperties error(s) "
                    f"missing/mismatched in GUI View")
    details = {"violations_present": True}
    if mismatches:
        details["mismatches"] = mismatches
    return InvariantResult(name, passed, message, details, evidence=evidence)
