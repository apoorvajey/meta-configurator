"""INV-D7."""
from __future__ import annotations
import json
import re
from jsonschema import Draft202012Validator, FormatChecker, RefResolver
from tests.invariants.invariants_core import InvariantResult, _get_ace_annotations
from tests.invariants.schema_validity import schema_meta_violations
from tests.shared.python.utils_code_editor import read_text_editor_content

ID = "INV-D7"
NAME = "_inv_d7_text_view_markers_match_schema_violations"

# Time for the app's debounced validation to finish before the markers are read.
SETTLE_MS = 600

# jsonschema keyword -> start of the message AJV shows for it in the Text View.
# None = unknown: matches any marker.
_AJV_MESSAGE = {
    "additionalProperties": "must NOT have additional properties",
    "required": "must have required property",
    "type": "must be ",
    "enum": "must be equal to one of the allowed values",
    "const": "must be equal to constant",
    "minimum": "must be >=",
    "maximum": "must be <=",
    "exclusiveMinimum": "must be >",
    "exclusiveMaximum": "must be <",
    "multipleOf": "must be multiple of",
    "minLength": "must NOT have fewer than",
    "maxLength": "must NOT have more than",
    "minItems": "must NOT have fewer than",
    "maxItems": "must NOT have more than",
    "minProperties": "must NOT have fewer than",
    "maxProperties": "must NOT have more than",
    "uniqueItems": "must NOT have duplicate items",
    "pattern": "must match pattern",
    "format": "must match format",
    "propertyNames": "property name must be valid",
}
# "type" errors read "must be string / number / integer / ...": a plain "must be " would also match "must be < 10".
_TYPE_MESSAGES = tuple(f"must be {t}" for t in ("string", "number", "integer", "boolean", "object", "array", "null"))


def _extra_keys(err) -> list[str]:
    """The keys an additionalProperties error is about: those of the object not covered by properties/patternProperties."""
    sub = err.schema
    declared = set(sub.get("properties", {}))
    patterns = list(sub.get("patternProperties", {}))
    return [k for k in err.instance
            if k not in declared and not any(re.search(p, k) for p in patterns)]


def _short(value, limit: int = 80) -> str:
    """A value as compact JSON, cut to `limit` characters, for the evidence lines."""
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def _predict(text_data, schema) -> list[dict]:
    """The Text View errors we expect: one {keyword, message_start, path} per violation of the whole schema.

    additionalProperties is one error per extra key: the app shows one marker per extra key (checked in the live app:
    {x, y} gives two markers). The blank key "" is predicted like any other; the app does not mark it, so it is
    reported as a missing marker.
    """
    validator = Draft202012Validator(schema, resolver=RefResolver.from_schema(schema),
                                     format_checker=FormatChecker())
    expected = []
    property_names_seen = set()
    for err in validator.iter_errors(text_data):
        base = "".join(f"/{p}" for p in err.absolute_path)   # "" for the root object
        if "propertyNames" in err.absolute_schema_path:
            # The app shows ONE "property name must be valid" marker per object, however many names or keywords fail.
            if base not in property_names_seen:
                property_names_seen.add(base)
                expected.append({"keyword": "propertyNames", "path": base,
                                 "message_start": _AJV_MESSAGE["propertyNames"],
                                 "reason": err.message, "value": _short(err.instance),
                                 "rule": "/".join(str(p) for p in err.absolute_schema_path)})
            continue
        if err.validator == "additionalProperties" and isinstance(err.instance, dict):
            paths = [f"{base}/{k}" for k in _extra_keys(err)] or [base]
        else:
            paths = [base]
        for path in paths:
            value = err.instance[path.rsplit("/", 1)[-1]] if (
                err.validator == "additionalProperties" and isinstance(err.instance, dict) and path != base
            ) else err.instance
            expected.append({"keyword": err.validator, "path": path,
                             "message_start": _AJV_MESSAGE.get(err.validator),
                             "reason": err.message, "value": _short(value),
                             "rule": "/".join(str(p) for p in err.absolute_schema_path)})
    # The app never marks the blank key "". Markers are matched to predictions by message only, so put the blank-key
    # predictions last: when there are fewer markers than predictions, the one reported missing is then the blank key.
    expected.sort(key=lambda e: e["keyword"] == "additionalProperties" and e["path"].endswith("/"))
    return expected


def _gutter_error_cells(page, mode) -> int | None:
    """Rendered red error icons in the Text View gutter (what the user sees), as opposed to Ace's annotation list."""
    mode_val = mode.value if hasattr(mode, "value") else mode
    try:
        return page.evaluate(f"""
            () => {{
                const el = document.querySelector('[id^="code-editor-{mode_val}"]');
                return el ? el.querySelectorAll('.ace_gutter-cell.ace_error').length : null;
            }}
        """)
    except Exception:
        return None


def check(ctx) -> InvariantResult:
    """The red error markers in the Text View gutter must be exactly the schema violations of the text shown there.

    Extracts Ace's annotations (type 'error') and the rendered gutter cells, predicts the AJV errors by validating the
    parsed Text View content against the schema with jsonschema, and matches them one to one. Fails when
    - a marker has no predicted violation (stale warning, e.g. left over after deleting an entry in the GUI View), or
    - a predicted violation has no marker (missing warning).
    Marker text is only compared in the evidence; pass/fail is decided by the match.
    """
    name = "INV-D7 Text View markers match schema violations"

    schema = ctx.tp.get_schema()
    meta_violations = schema_meta_violations(ctx.page, schema)
    if meta_violations is None or meta_violations:
        return InvariantResult(name, True, "SKIPPED: schema validity unknown or invalid - markers not comparable",
                               skipped=True)

    ctx.page.wait_for_timeout(SETTLE_MS)
    text_data = read_text_editor_content(ctx.page, ctx.mode)
    if text_data is None:
        return InvariantResult(name, True, "SKIPPED: Text View not parseable (INV-D1 reports this)", skipped=True)

    expected = _predict(text_data, schema)
    markers = [a for a in _get_ace_annotations(ctx.page, ctx.mode) if a.get("type") == "error"]
    gutter_cells = _gutter_error_cells(ctx.page, ctx.mode)

    unused = list(markers)
    missing = []
    matched = []
    for exp in expected:
        start = _TYPE_MESSAGES if exp["keyword"] == "type" else exp["message_start"]
        hit = next((m for m in unused if start is None or (m.get("text") or "").startswith(start)), None)
        if hit is None:
            missing.append(exp)
        else:
            unused.remove(hit)
            matched.append({"expected": exp, "marker": hit.get("text"), "row": hit.get("row")})

    evidence = [f"predicted {len(expected)} error(s), Text View shows {len(markers)} marker(s), "
                f"{gutter_cells} rendered gutter icon(s)"]
    evidence += [f"{m['expected']['path'] or '/'}: {m['expected']['keyword']} -> marker '{m['marker']}' (row {m['row']}) ✓"
                 for m in matched]
    for e in missing:
        evidence.append(f"{e['path'] or '/'}: {e['keyword']} predicted, NO marker ✗")
        evidence.append(f"   jsonschema: {e['reason']}   value: {e['value']}   rule: {e['rule']}")
    evidence += [f"row {m.get('row')}: marker '{m.get('text')}' has no predicted violation ✗" for m in unused]
    if missing or unused:
        evidence.append("markers shown by app: " + (
            "; ".join(f"row {m.get('row')}: '{m.get('text')}'" for m in markers) or "(none)"))

    passed = not missing and not unused
    return InvariantResult(
        name, passed,
        "Text View markers match schema violations ✓" if passed
        else f"{len(unused)} stale marker(s), {len(missing)} missing marker(s)",
        {} if passed else {
            "stale_markers": [{"row": m.get("row"), "text": m.get("text")} for m in unused],
            "missing_markers": missing,
            "gutter_error_cells": gutter_cells,
            "text_view": text_data,
        },
        evidence=evidence,
    )