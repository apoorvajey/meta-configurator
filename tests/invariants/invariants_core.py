"""Mode-specific invariant checkers for MetaConfigurator mutation testing.

    InvariantDispatcher reads the active mode from the UI and runs:
        InvariantCheckerDataEditor    (Data Editor)
        InvariantCheckerSchemaEditor  (Schema Editor)
        nothing                       (Settings)

    Data Editor
        INV-D1          Text View reflects internal data
        INV-D3          GUI required stars match schema.required
        INV-D4          GUI rows exist for all data keys
        INV-D5          GUI string values match data
        INV-D6          GUI validation icons match schema violations
        INV-GUI-ERR-01  GUI validation icon matches the Text View additionalProperties error

    Schema Editor
        INV-S0  Schema state is an object
        INV-S1  Text View reflects internal schema
        INV-S2  GUI rows exist for all top-level schema keys
        INV-S3  Schema title field matches internal schema

There is no INV-D2. "Required fields present in data" only holds as a transition postcondition
(TEMP-ADD-03), because undo can legitimately break it as a standing rule.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from playwright.sync_api import Page
from jsonschema import validate, Draft202012Validator, RefResolver
from tests.shared.python.utils import SessionMode
from tests.shared.python.test_panel import TestPanel
from tests.shared.python.utils_code_editor import read_text_editor_content, read_text_editor_raw
from tests.shared.python.utils_gui_editor import (
    read_property_exists,
    read_top_level_property_keys,
    read_string_property,
    edit_string_property,  # used by the INV-GUI-ERR-01 positive control
    read_required_star_visible,
)

_ADDITIONAL_PROPS_MSG = "must NOT have additional properties"
_JSON_POINTER_RE = re.compile(r"(/[^\s\"']+)")


def _path_to_testid_string(path: list) -> str:
    """Mirrors dom_discoverer.path_to_testid_string(). Duplicated so this module does not depend on the
    mutation_testing package. Keep in sync with pathToString() in MetaConfigurator's pathUtils.ts.
    """
    result = ""
    for p in path:
        if isinstance(p, int):
            result += f"[{p}]"
        else:
            result += f".{p}"
    return result[1:] if result.startswith(".") else result


def _find_additional_properties_violations(data, schema, path: list | None = None) -> list[tuple[list, str]]:
    """Walk `data` alongside `schema` and return (parent_path, offending_key) for every key in an object
    whose schema has additionalProperties: false and does not list that key under properties or
    patternProperties.

    This only follows properties and patternProperties through plain dict schemas. It does not resolve
    additionalProperties behind $ref, allOf or oneOf.
    """
    path = path or []
    violations: list[tuple[list, str]] = []
    if not isinstance(data, dict) or not isinstance(schema, dict):
        return violations

    properties = schema.get("properties", {}) or {}
    pattern_properties = schema.get("patternProperties", {}) or {}
    additional_properties = schema.get("additionalProperties", True)

    if additional_properties is False:
        for key in data:
            if key in properties:
                continue
            if any(re.search(pat, key) for pat in pattern_properties):
                continue
            violations.append((list(path), key))

    for key, value in data.items():
        child_schema = properties.get(key)
        if child_schema is None:
            for pat, pschema in pattern_properties.items():
                if re.search(pat, key):
                    child_schema = pschema
                    break
        if isinstance(child_schema, dict) and isinstance(value, dict):
            violations.extend(
                _find_additional_properties_violations(value, child_schema, path + [key])
            )

    return violations


def _value_violates_schema(value, prop_schema, root_schema=None) -> bool:
    """True if `value` violates `prop_schema`. $refs resolve against `root_schema`; errors count as valid."""
    if not prop_schema:
        return False
    try:
        if root_schema:
            resolver = RefResolver.from_schema(root_schema)
            validator = Draft202012Validator(prop_schema, resolver=resolver)
            return not validator.is_valid(value)
        validate(instance=value, schema=prop_schema)
        return False
    except Exception:
        return False


def _get_ace_annotations(page: Page, mode: SessionMode) -> list[dict]:
    """Ace's gutter annotations for the given editor, [{row, column, text, type}, ...]. This is how AJV
    errors are assumed to reach the Text View; not checked against every app version.
    """
    mode_str = "dataEditor" if mode == SessionMode.DataEditor else "schemaEditor"
    try:
        annotations = page.evaluate(f"""
            () => {{
                const editorEl = [...document.querySelectorAll('[id^="code-editor-{mode_str}"]')][0];
                if (!editorEl) return null;
                const editor = window.ace?.edit(editorEl.id);
                if (!editor) return null;
                return editor.session.getAnnotations();
            }}
        """)
    except Exception:
        return []
    return annotations or []


def _text_view_error_for_key(page: Page, mode: SessionMode, raw_text: str | None,
                             key: str, message_substr: str = _ADDITIONAL_PROPS_MSG) -> list[dict]:
    """Ace annotations whose text contains `message_substr` and that can be attributed to `key` by either:
    (a) a JSON pointer in the annotation text whose last segment is `key`, or
    (b) the annotation's row, looked up in the raw text for `"key"`. AJV keeps instancePath apart from
        the message, so this is the more likely case.
    Either counts as a match. Used by the invariant and by the positive control.
    """
    annotations = _get_ace_annotations(page, mode)
    lines = raw_text.split("\n") if raw_text else []
    matches = []
    for ann in annotations:
        text = ann.get("text") or ""
        if message_substr not in text:
            continue
        row = ann.get("row")
        line = lines[row] if isinstance(row, int) and 0 <= row < len(lines) else ""
        pointer_match = _JSON_POINTER_RE.search(text)
        key_in_pointer = bool(pointer_match) and pointer_match.group(1).rstrip("/").split("/")[-1] == key
        key_in_line = f'"{key}"' in line
        if key_in_pointer or key_in_line:
            matches.append({
                "message": text,
                "row": row,
                "column": ann.get("column"),
                "line_text": line.strip(),
                "matched_via": "json_pointer" if key_in_pointer else "line_scan",
            })
    return matches


def _gui_error_icon_and_tooltip_for_path(page: Page, full_path: list) -> dict:
    """Path-aware version of InvariantCheckerDataEditor's _property_has_error_icon() and
    _get_validation_error_messages(), which only handle top-level keys. Nested paths are untested,
    since INV-D3, D5 and D6 only use top-level keys.
    """
    path_str = _path_to_testid_string(full_path)
    testid = f"property-metadata-{path_str}"
    container = page.get_by_test_id(testid)
    if container.count() == 0:
        return {"row_found": False, "icon_shown": False, "tooltip_text": None}

    icon_shown = container.get_by_test_id("validation-error-icon").count() > 0
    tooltip_text = None
    if icon_shown:
        try:
            container.hover()
            page.wait_for_timeout(1000)
            texts_loc = page.locator(".p-popover .text-red-600")
            texts = [texts_loc.nth(i).inner_text().strip() for i in range(texts_loc.count())]
            tooltip_text = " | ".join(t for t in texts if t)
        finally:
            page.keyboard.press("Escape")
            page.mouse.move(0, 0)
            try:
                page.wait_for_selector(".p-popover", state="hidden", timeout=1000)
            except Exception:
                pass
    return {"row_found": True, "icon_shown": icon_shown, "tooltip_text": tooltip_text}


def _diagnose_unparseable_text(page: Page, mode, ground_truth=None) -> tuple[str, dict]:
    """Returns (message, details). If the content parses under the other format than the selected one
    (a known desync) and `ground_truth` is given, the alternate parse is diffed against ground_truth,
    to show whether the data diverged too or only the format label is wrong.
    """
    import json as _json
    import yaml as _yaml

    raw = read_text_editor_raw(page, mode)
    if raw is None:
        return "Text View content could not be read at all (editor not found)", {}

    try:
        current_format = page.get_by_test_id("format-selector").inner_text().lower().strip()
    except Exception:
        current_format = "unknown"

    preview = raw[:300].replace("\n", "\\n")

    def _compare(parsed, label: str) -> dict:
        details = {"raw_preview": preview, "actually_parses_as": label}
        if ground_truth is not None:
            diffs = _diff_nested(ground_truth, parsed)
            if diffs:
                details["verdict"] = (f"{len(diffs)} field(s) differ - this is REAL data "
                                      f"divergence, not just a stale label")
                details["diffs"] = [f"{p}: {a!r}  →  {b!r}" for p, a, b in diffs]
            else:
                details["verdict"] = ("content matches internal state exactly - this is a "
                                      "COSMETIC label desync only, the data itself is fine")
        return details

    if "json" in current_format:
        try:
            parsed_yaml = _yaml.safe_load(raw)
            try:
                _json.loads(raw)
                is_also_json = True
            except Exception:
                is_also_json = False
            if not is_also_json:
                return ("content is valid YAML but format selector shows 'json' - likely "
                        "stale Ace undo history desynced from the format label",
                        _compare(parsed_yaml, "YAML"))
        except Exception:
            pass
        try:
            _json.loads(raw)
        except Exception as e:
            return f"format=json, JSON parse error: {e}", {"raw_preview": preview}
    elif "yaml" in current_format:
        try:
            parsed_json = _json.loads(raw)
            return ("content is valid JSON but format selector shows 'yaml' - likely "
                    "stale Ace undo history desynced from the format label",
                    _compare(parsed_json, "JSON"))
        except Exception:
            pass
        try:
            _yaml.safe_load(raw)
        except Exception as e:
            return f"format=yaml, YAML parse error: {e}", {"raw_preview": preview}

    return f"format={current_format}, content unparseable in either JSON or YAML", {"raw_preview": preview}


@dataclass
class InvariantResult:
    name: str
    passed: bool
    message: str
    details: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def __str__(self):
        icon = "✓" if self.passed else "✗"
        return f"  [{icon}] {self.name}: {self.message}"


def _print_detail_value(key: str, value) -> None:
    """Pretty-print a details[key] value. Lists of dicts (e.g. INV-D6's per-property mismatches) are
    printed one item per line with field labels. Anything else is printed on one line.
    """
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        print(f"      {key} ({len(value)}):")
        for i, item in enumerate(value):
            fields = ", ".join(f"{k}={v!r}" for k, v in item.items())
            print(f"        [{i}] {fields}")
    elif isinstance(value, list) and value:
        print(f"      {key} ({len(value)}):")
        for item in value:
            print(f"        • {item}")
    else:
        print(f"      {key}: {value}")


def _diff_nested(a, b, path: str = "") -> list[tuple[str, object, object]]:
    """Walk two values in parallel and return (path, a_value, b_value) for every difference. Used for the
    detail pairs of INV-D1 and INV-S1. Only dicts are recursed into; any other difference, lists
    included, is one leaf difference at that path.
    """
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            sub_path = f"{path}.{key}" if path else key
            if key not in a:
                diffs.append((sub_path, "<missing>", b[key]))
            elif key not in b:
                diffs.append((sub_path, a[key], "<missing>"))
            else:
                diffs.extend(_diff_nested(a[key], b[key], sub_path))
    elif a != b:
        diffs.append((path, a, b))
    return diffs


@dataclass
class InvariantReport:
    results: list[InvariantResult] = field(default_factory=list)
    mode: str = ""

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[InvariantResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        n = len(self.results)
        f = len(self.failures)
        prefix = f"[{self.mode}] " if self.mode else ""
        return (
            f"{prefix}All {n} invariants passed ✓"
            if f == 0
            else f"{prefix}{f}/{n} FAILED ✗"
        )

    def print(self):
        if self.passed:
            print(f"\n  {self.summary()}")
        else:
            for r in self.failures:
                print(r)
            print(f"\n  {self.summary()}")
            for r in self.failures:
                if r.evidence:
                    print(f"\n    Evidence for {r.name}:")
                    for line in r.evidence:
                        print(f"      • {line}")
                if r.details:
                    print(f"    Details for {r.name}:")
                    left_key = ("internal_state" if "internal_state" in r.details
                                else "internal_schema" if "internal_schema" in r.details
                    else None)
                    if left_key and "text_view" in r.details:
                        diffs = _diff_nested(r.details[left_key], r.details["text_view"])
                        if diffs:
                            print(f"      {len(diffs)} field(s) differ ({left_key} vs text_view):")
                            for path, a_val, b_val in diffs:
                                print(f"        {path}: {a_val!r}  →  {b_val!r}")
                        else:
                            print("      No field-level differences found by shallow diff - "
                                  "check for key ordering or a type mismatch it didn't catch")
                        for k, v in r.details.items():
                            if k not in (left_key, "text_view"):
                                _print_detail_value(k, v)
                    else:
                        for k, v in r.details.items():
                            _print_detail_value(k, v)


class _BaseInvariantChecker:
    """Runs every method whose name starts with _inv_. Subclasses define the invariants."""

    def __init__(self, page: Page, mode: SessionMode):
        self.page = page
        self.mode = mode
        self.tp = TestPanel(page, mode)

    def run_all(self) -> InvariantReport:
        report = InvariantReport(mode=self.mode.value)
        for name in sorted(dir(self)):
            if name.startswith("_inv_"):
                try:
                    result: InvariantResult = getattr(self, name)()
                    report.results.append(result)
                except Exception as e:
                    _toks = []
                    for _t in name[len("_inv_"):].split("_"):
                        _toks.append(_t.upper())
                        if re.fullmatch(r"[a-z]?\d+", _t):
                            break
                    report.results.append(InvariantResult(
                        name=f"CHK-ERR-INV-{'-'.join(_toks)} check crashed",
                        passed=False,
                        message=f"Exception during check ({type(e).__name__}): {e}",
                    ))
        return report


class InvariantCheckerDataEditor(_BaseInvariantChecker):
    """Invariants for Data Editor mode. Ground truth: TestPanel(page, SessionMode.DataEditor).get_data()"""

    def __init__(self, page: Page):
        super().__init__(page, SessionMode.DataEditor)

    def _gui_row_has_value(self, key: str) -> bool:
        """True if the top-level row for `key` shows content, False if it is an empty schema placeholder.

        Object properties are pre-rendered with a nested form for their required sub-properties even when
        the key is absent from data, so a nested property-data-* row is not evidence of a value. Array
        rows (property-data-{key}[0], [1], ...) only exist when the data has items, so they are.
        """
        row = self.page.locator(f'[data-testid="property-data-{key}"]')
        if row.count() == 0:
            return False

        for el in row.locator('input, textarea, select').all():
            if el.get_attribute('type') == 'checkbox':
                if el.is_checked():
                    return True
                continue
            if (el.input_value() or '').strip():
                return True

        if row.locator(f'[data-testid^="property-data-{key}["]').count() > 0:
            return True

        return False

    def _property_has_error_icon(self, key: str) -> dict:
        """Returns {"row_found": bool, "icon_shown": bool}, kept separate so INV-D6 evidence can tell a row
        that never rendered from a row without an icon.
        """
        try:
            container = self.page.get_by_test_id(f"property-metadata-{key}")
            if container.count() == 0:
                return {"row_found": False, "icon_shown": False}
            icon = container.get_by_test_id("validation-error-icon")
            return {"row_found": True, "icon_shown": icon.count() > 0}
        except Exception as e:
            print(f"   [INV-D6] error icon check for '{key}' raised: {e}")
            return {"row_found": False, "icon_shown": False}

    def _get_validation_error_messages(self, key: str) -> list[str]:
        """Hover over the property metadata row to open the overlay, then read the red error messages."""
        try:
            metadata = self.page.get_by_test_id(f"property-metadata-{key}")
            if not metadata.is_visible():
                return []
            metadata.hover()
            self.page.wait_for_timeout(1000)
            errors = self.page.locator(".p-popover .text-red-600")
            messages = []
            for i in range(errors.count()):
                text = errors.nth(i).inner_text().strip()
                if text:
                    messages.append(text)
            # dismiss the overlay with Escape and move the mouse away
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(100)
            self.page.mouse.move(0, 0)
            self.page.wait_for_timeout(300)
            # wait for the popover to disappear
            try:
                self.page.wait_for_selector(".p-popover", state="hidden", timeout=1000)
            except Exception:
                pass
            return messages
        except Exception:
            return []

    def _inv_d1_text_view_reflects_data(self) -> InvariantResult:
        """The Text View content must parse to exactly the internal data. An unparseable Text View is always
        a failure. Only json and yaml are selectable formats.

        Selector: [id^="code-editor-dataEditor"]. See panelTextEditor.spec.ts and test_basic.py.
        """
        name = "INV-D1 Text View reflects internal data"

        ground_truth = self.tp.get_data()
        text_data = read_text_editor_content(self.page, self.mode)

        if text_data is None:
            diagnosis, details = _diagnose_unparseable_text(self.page, self.mode, ground_truth=ground_truth)
            return InvariantResult(name, False,
                                   f"Text View could not be parsed - {diagnosis}",
                                   details)

        passed = ground_truth == text_data
        evidence = [
            f"Compared internal data ({len(ground_truth) if isinstance(ground_truth, dict) else '?'} keys) "
            f"against Text View content → {'match ✓' if passed else 'mismatch ✗'}"
        ]
        return InvariantResult(
            name, passed,
            "Text View matches internal data ✓" if passed
            else "Text View diverges from internal data",
            {} if passed else {
                "internal_state": ground_truth,
                "text_view": text_data,
            },
            evidence=evidence,
        )

    def _inv_d3_gui_required_stars_match_schema(self) -> InvariantResult:
        """For each top-level property in schema.properties, the GUI shows a required star iff the property
        is in schema.required.

        See checkPropertyRequired() in panelGuiEditor.spec.ts. Selector: property-metadata-{path} >
        required-star.
        """
        name = "INV-D3 GUI required-star icons match schema"
        schema = self.tp.get_schema()
        required = set(schema.get("required", []))
        properties = schema.get("properties", {})

        if not properties:
            return InvariantResult(name, True,
                                   "No properties in schema - skip")

        mismatches = []
        for prop_name in properties:
            should_have_star = prop_name in required
            has_star = read_required_star_visible(self.page, [prop_name])
            if should_have_star != has_star:
                mismatches.append({
                    "property": prop_name,
                    "schema_says_required": should_have_star,
                    "gui_shows_star": has_star,
                })

        passed = not mismatches
        evidence = [
            f"{prop}: {'required ✓' if prop in required else 'optional'} - "
            f"{'star shown' if read_required_star_visible(self.page, [prop]) else 'no star'}"
            for prop in properties
        ]
        return InvariantResult(
            name, passed,
            "All required-star icons consistent ✓" if passed
            else f"{len(mismatches)} required-star mismatch(es)",
            {} if passed else {"mismatches": mismatches},
            evidence=evidence,
        )

    def _inv_d4_gui_rows_exist_for_data_keys(self) -> InvariantResult:
        """GUI rows must be complete and free of stale values relative to data. Row existence need not match
        data keys exactly, because GUI View is a schema-driven form.

        1. Every data key must have a visible GUI row.
        2. Every GUI row that shows a value (populated input, checked checkbox, populated array items) must
           correspond to a data key. A row with no value is fine: MetaConfigurator pre-renders placeholders
           for object-typed and required properties.

        Check 2 only looks at rows with a value because flagging every unbacked row reported empty
        placeholders as orphans (Autonomous Vehicle schema with `{}` data: all 7 object properties). It
        catches a stale row that keeps its content after data moved on, e.g. undo removes a key from data.

        See checkPropertyExistence() in panelGuiEditor.spec.ts. Selector: property-data-{key}.
        """

        name = "INV-D4 GUI rows complete, no stale values"
        data = self.tp.get_data()

        if not isinstance(data, dict):
            return InvariantResult(name, True,
                                   "Data is not an object - skip")

        data_keys = set(data.keys())
        gui_keys = set(read_top_level_property_keys(self.page))

        missing_rows = sorted(data_keys - gui_keys)
        orphan_candidates = gui_keys - data_keys
        orphan_rows = sorted(k for k in orphan_candidates if self._gui_row_has_value(k))
        passed = not missing_rows and not orphan_rows

        evidence = [f"{key}: row visible ✓" for key in sorted(data_keys - set(missing_rows))]
        evidence += [f"{key}: MISSING from GUI ✗" for key in missing_rows]
        evidence += [f"{key}: stale row - shows a value but no matching data key ✗" for key in orphan_rows]

        details = {}
        if missing_rows:
            details["missing_in_gui"] = missing_rows
        if orphan_rows:
            details["stale_gui_rows"] = orphan_rows

        message = "GUI rows complete, no stale values ✓"
        if not passed:
            parts = []
            if missing_rows:
                parts.append(f"missing rows for {missing_rows}")
            if orphan_rows:
                parts.append(f"stale rows for {orphan_rows}")
            message = "; ".join(parts)

        return InvariantResult(name, passed, message, details, evidence=evidence)

    def _inv_d5_gui_string_values_match_data(self) -> InvariantResult:
        """For each top-level string property, the GUI text field shows the same value as the internal state.

        See checkStringProperty() in panelGuiEditor.spec.ts. Selector: property-data-{key} > textbox.
        """
        name = "INV-D5 GUI string values match data"
        data = self.tp.get_data()

        if not isinstance(data, dict):
            return InvariantResult(name, True,
                                   "Data is not an object - skip")

        schema = self.tp.get_schema()
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
            actual = read_string_property(self.page, [key])
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
            actual = read_string_property(self.page, [key])
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

    def _inv_d6_gui_validation_icons_match_violations(self) -> InvariantResult:
        """For each top-level property, the GUI shows a validation-error-icon iff the value violates the
        schema.

        See checkPropertySchemaViolation() in panelGuiEditor.spec.ts. Selector: property-metadata-{path} >
        validation-error-icon.
        """

        name = "INV-D6 GUI validation icons match violations"
        data = self.tp.get_data()

        if not isinstance(data, dict):
            return InvariantResult(name, True,
                                   "Data is not an object - skip")

        schema = self.tp.get_schema()
        props = schema.get("properties", {})

        mismatches = []
        evidence = []

        for key, value in data.items():
            prop_schema = props.get(key)
            if prop_schema is None:
                continue

            should_have_error = _value_violates_schema(value, prop_schema, root_schema=schema)
            icon_state = self._property_has_error_icon(key)
            gui_shows_error = icon_state["icon_shown"]
            row_found = icon_state["row_found"]

            status = "AGREE ✓" if (should_have_error == gui_shows_error) else "DISAGREE ✗"
            expected = "violation expected" if should_have_error else "no violation expected"
            actual = "icon shown" if gui_shows_error else "no icon shown"

            # Every simultaneous violation with its path, not just the first one validate() raises. A single error hides the others and lets the reported reason drift.
            violation_details = []
            if should_have_error:
                try:
                    prop_schema_combined = {**prop_schema, "$defs": schema.get("$defs", {})}
                    resolver = RefResolver.from_schema(schema)
                    validator = Draft202012Validator(prop_schema_combined, resolver=resolver)
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

            gui_messages = self._get_validation_error_messages(key) if gui_shows_error else []
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

    def _inv_gui_err_01_additional_properties_desync(self) -> InvariantResult:
        """An AJV additionalProperties violation shown in Text View must also appear as a
        validation-error-icon, with a matching tooltip, on the row in GUI View.

        Suspected desync: the generic fallback widget for keys with no schema node (which is what an
        additionalProperties: false violation is) may not use the same isInvalid() logic as schema-bound
        fields such as StringProperty.vue, which INV-D6 covers.

        The violating key is taken from comparing get_data() with get_schema() in Python
        (_find_additional_properties_violations), because the Text View display is what is under test.
        """
        name = "INV-GUI-ERR-01 GUI validation icon matches Text View additionalProperties error"

        data = self.tp.get_data()
        schema = self.tp.get_schema()
        violations = _find_additional_properties_violations(data, schema)

        if not violations:
            return InvariantResult(name, True,
                                   "No additionalProperties violations present in current data - skip",
                                   details={"violations_present": False})

        raw_text = read_text_editor_raw(self.page, self.mode)
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

            text_matches = _text_view_error_for_key(self.page, self.mode, raw_text, key)
            if not text_matches:
                evidence.append(
                    f"{label}: violation present in data, but no matching AJV error found in "
                    f"Text View annotations - cannot compare (check the annotation-source "
                    f"assumption in _get_ace_annotations() against the live app)")
                continue

            text_message = text_matches[0]["message"]
            gui = _gui_error_icon_and_tooltip_for_path(self.page, full_path)

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

def run_positive_control_schema_bound_violation(
        page: Page,
        property_path: list[str] | None = None,
        invalid_value: str = "NotMatchingPattern",
) -> InvariantResult:
    """Sanity check for INV-GUI-ERR-01: confirms the check recognises a case where GUI View does show
    the error icon, so a pass is not just the check never firing.

    It uses a schema-bound field (default: top-level SimulationName, pattern ^Sim_) instead of an
    additionalProperties violation, since INV-D6 already confirms the GUI handles that class. The value
    is written through edit_string_property(), the same path the mutation actions use, and the original
    state is restored with TestPanel.force_data() in a finally block.

    Not run by run_all() (name does not start with _inv_). Call it explicitly, e.g. at the start of a
    debugging session.
    """
    name = "POSITIVE CONTROL: schema-bound pattern violation shown in GUI"
    property_path = property_path or ["SimulationName"]
    key = property_path[-1]
    mode = SessionMode.DataEditor
    tp = TestPanel(page, mode)

    original_data = tp.get_data()

    try:
        edit_string_property(page, property_path, invalid_value)
        page.wait_for_timeout(500)  # let AJV and Vue reactivity settle

        raw_text = read_text_editor_raw(page, mode)
        if raw_text is None:
            return InvariantResult(name, False,
                                   "Text View could not be read - cannot verify setup")

        text_matches = _text_view_error_for_key(
            page, mode, raw_text, key, message_substr="must match pattern"
        )
        if not text_matches:
            return InvariantResult(
                name, False,
                f"Setup failed - Text View shows no pattern-violation error for "
                f"'{invalid_value}' at {property_path}. Either edit_string_property() "
                f"didn't register the change, or AJV's actual wording differs from "
                f"'must match pattern' - check a real annotation before trusting this "
                f"control.")

        gui = _gui_error_icon_and_tooltip_for_path(page, property_path)
        if not gui["row_found"]:
            return InvariantResult(name, False,
                                   f"GUI row not found at path {property_path} - "
                                   f"fix path/selector, control is invalid")

        passed = gui["icon_shown"]
        message = ("GUI correctly shows error indicator for schema-bound violation ✓"
                   if passed else
                   "GUI shows NO indicator for a schema-bound pattern violation - the "
                   "check itself may be broken (false-negative risk), separate from the "
                   "any-type fallback bug INV-GUI-ERR-01 targets")
        return InvariantResult(
            name, passed, message,
            details={} if passed else {"path": property_path, "gui_result": gui},
            evidence=[f"Text View: {text_matches[0]['message']}",
                      f"GUI icon_shown={gui['icon_shown']}, tooltip={gui['tooltip_text']!r}"],
        )
    finally:
        tp.force_data(original_data)
        page.wait_for_timeout(300)


class InvariantCheckerSchemaEditor(_BaseInvariantChecker):
    """Invariants for Schema Editor mode. Ground truth: TestPanel(page, SessionMode.SchemaEditor).get_schema()

    The Schema Editor GUI renders the schema against the JSON Schema meta-schema, so the top-level keys
    shown are the schema's own: $schema, title, type, properties, required, $defs, etc.
    """

    def __init__(self, page: Page):
        super().__init__(page, SessionMode.SchemaEditor)
        # the user's schema is exposed through the DataEditor test panel
        self.tp_data = TestPanel(page, SessionMode.DataEditor)

    def _inv_s0_schema_state_is_an_object(self) -> InvariantResult:
        """The schema must always be an object (or a boolean, which is valid whole-schema shorthand).
        Checked on its own because INV-S1, S2 and S3 assume a dict.

        INV-S2 used to skip a non-dict schema and pass, hiding exactly the corruption this catches. It was
        reachable: a root-level change-type-text-schemaEditor action collapsed the schema to a bare int.
        NEW-07 in temporal_invariants.py is the transition-based counterpart.
        """
        name = "INV-S0 schema state is an object"
        schema = self.tp_data.get_schema()
        passed = isinstance(schema, dict) or isinstance(schema, bool)

        return InvariantResult(
            name, passed,
            "schema is an object ✓" if passed
            else f"schema is not an object (or boolean) - got "
                 f"{type(schema).__name__}: {str(schema)[:100]}",
            {} if passed else {
                "actual_type": type(schema).__name__,
                "actual_value": str(schema)[:200],
            },
        )

    def _inv_s1_text_view_reflects_schema(self) -> InvariantResult:
        """The schema Text View content must parse to exactly the internal schema. Same as INV-D1 but reads
        get_schema() and code-editor-schemaEditor. Only json and yaml are selectable.

        Selector: [id^="code-editor-schemaEditor"]
        """
        name = "INV-S1 Text View reflects internal schema"

        ground_truth = self.tp_data.get_schema()
        text_data = read_text_editor_content(self.page, self.mode)

        if text_data is None:
            diagnosis, details = _diagnose_unparseable_text(self.page, self.mode, ground_truth=ground_truth)
            return InvariantResult(name, False,
                                   f"Schema Text View could not be parsed - {diagnosis}",
                                   details)

        passed = ground_truth == text_data
        evidence = [
            f"Compared internal schema ({len(ground_truth) if isinstance(ground_truth, dict) else '?'} keys) "
            f"against Schema Text View → {'match ✓' if passed else 'mismatch ✗'}"
        ]
        return InvariantResult(
            name, passed,
            "Schema Text View matches internal schema ✓" if passed
            else "Schema Text View diverges from internal schema",
            {} if passed else {
                "internal_schema": ground_truth,
                "text_view": text_data,
            },
            evidence=evidence,
        )

    def _inv_s2_gui_rows_exist_for_schema_keys(self) -> InvariantResult:
        """Every top-level key in the schema must have a visible row in the Schema Editor GUI View. The GUI
        renders the schema as a form against the meta-schema, so 'title', 'type', 'properties', 'required',
        '$defs' etc. each get a property-data-{key} row. Keys starting with '$' use the key name directly,
        e.g. property-data-$schema.

        A non-dict schema is not treated like an empty {}. It fails here, because skipping the per-key walk
        is not the same as all keys being present. INV-S0 reports the corruption itself.
        """
        name = "INV-S2 GUI rows exist for top-level schema keys"
        schema = self.tp.get_schema()

        if not isinstance(schema, dict):
            return InvariantResult(
                name, False,
                f"Cannot check GUI rows - schema is not an object "
                f"(got {type(schema).__name__}); see INV-S0",
                {"actual_type": type(schema).__name__, "actual_value": str(schema)[:200]},
            )

        if not schema:
            return InvariantResult(name, True,
                                   "Schema is empty - skip")

        skip_keys = {"$schema"}  # rendered as a link/badge, not a property row

        missing_rows = [
            key for key in schema
            if key not in skip_keys
               and not read_property_exists(self.page, [key])
        ]

        passed = not missing_rows
        evidence = [
            f"{key}: {'row visible ✓' if key not in missing_rows and key not in skip_keys else ('skipped' if key in skip_keys else 'row missing ✗')}"
            for key in schema
        ]
        return InvariantResult(
            name, passed,
            "All schema keys have visible GUI rows ✓" if passed
            else f"Schema GUI missing rows for: {missing_rows}",
            {} if passed else {
                "schema_keys": list(schema.keys()),
                "missing_in_gui": missing_rows,
            },
            evidence=evidence,
        )

    def _inv_s3_schema_title_matches_internal(self) -> InvariantResult:
        """The 'title' shown in the Schema Editor GUI View must match the title in the internal schema. This
        also covers the toolbar, which reads the same state.

        Selector: property-data-title > textbox
        """
        name = "INV-S3 Schema title field matches internal schema"
        schema = self.tp_data.get_schema()

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

        actual_title = read_string_property(self.page, ["title"])

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


class InvariantDispatcher:
    """Reads the active editor mode from the UI and runs the matching checker.

        report = InvariantDispatcher(page).run_all()
        report.print()

    DataEditor and SchemaEditor run their own checker. Settings returns an empty passing report.
    """

    def __init__(self, page: Page):
        self.page = page

    def current_mode(self) -> SessionMode | None:
        """Read the active mode from the UI."""
        try:
            active = self.page.get_by_test_id("mode-active-true")
            text = active.inner_text().strip()
            if "Data" in text:
                return SessionMode.DataEditor
            if "Schema" in text:
                return SessionMode.SchemaEditor
            if "Settings" in text:
                return SessionMode.Settings
        except Exception:
            pass
        return None

    def run_all(self) -> InvariantReport:
        """Detect the current mode and run the matching invariants."""
        mode = self.current_mode()

        if mode == SessionMode.DataEditor:
            return InvariantCheckerDataEditor(self.page).run_all()

        if mode == SessionMode.SchemaEditor:
            return InvariantCheckerSchemaEditor(self.page).run_all()

        if mode == SessionMode.Settings:
            # Settings mode is out of scope
            report = InvariantReport(mode="settings")
            report.results.append(InvariantResult(
                name="Settings mode",
                passed=True,
                message="Settings mode - invariant checks skipped",
            ))
            return report

        # mode could not be detected
        report = InvariantReport(mode="unknown")
        report.results.append(InvariantResult(
            name="Mode detection",
            passed=False,
            message="Could not detect current editor mode",
        ))
        return report


class InvariantChecker(InvariantDispatcher):
    """Alias for InvariantDispatcher. The mode parameter is ignored, since the mode is read from the UI at
    check time.
    """

    def __init__(self, page: Page, mode: SessionMode = SessionMode.DataEditor):
        super().__init__(page)
        # mode is ignored, see the docstring
