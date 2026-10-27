"""INV-D8."""
from __future__ import annotations
from tests.invariants.invariants_core import InvariantResult
from tests.invariants.state.data_editor.text_view_validation_matches_schema import analyse, _is_blank_key

ID = "INV-D8"
NAME = "_inv_d8_text_view_marks_blank_key"


def check(ctx) -> InvariantResult:
    """The Text View marks the blank key "" like any other extra key when the schema forbids additional properties.

    Fails only when the marker of the blank key is missing while every other predicted marker is shown. If other
    markers are missing as well (all markers lost after a mode switch, BUG-08), this says nothing about the blank key
    and the check passes: INV-D7 reports that case. Uses the same analysis as INV-D7.
    """
    name = "INV-D8 Text View marks the blank key"
    skipped, data = analyse(ctx)
    if skipped is not None:
        return InvariantResult(name, True, skipped.message if hasattr(skipped, "message") else "SKIPPED", skipped=True)
    missing = data["missing"]
    blank = [e for e in missing if _is_blank_key(e)]
    others = [e for e in missing if not _is_blank_key(e)]
    if not blank or others:
        return InvariantResult(name, True, "Text View marks the blank key ✓ (or nothing to compare)")
    evidence = [f"{e['path'] or '/'}: {e['keyword']} predicted, NO marker ✗" for e in blank]
    evidence += [f"   jsonschema: {e['reason']}   value: {e['value']}   rule: {e['rule']}" for e in blank]
    evidence.append("all other predicted markers are shown: " + str(len(data["matched"])) + " marker(s) matched")
    return InvariantResult(
        name, False, f"{len(blank)} blank-key marker(s) missing while the other markers are shown",
        {"missing_markers": blank, "gutter_error_cells": data["gutter_cells"], "text_view": data["text_data"]},
        evidence=evidence,
    )
