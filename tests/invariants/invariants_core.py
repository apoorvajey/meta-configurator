"""
invariants_core.py
==================
Located at: tests/invariants/invariants_core.py

Mode-specific invariant checkers for MetaConfigurator mutation testing.

Architecture:
    InvariantDispatcher
        → reads current active mode from the UI
        → runs InvariantCheckerDataEditor   (when mode is DataEditor)
        → runs InvariantCheckerSchemaEditor (when mode is SchemaEditor)
        → skips                             (when mode is Settings)

    InvariantCheckerDataEditor
        INV-D1  Text View reflects internal data
        INV-D2  Required fields present in data
        INV-D3  GUI required-star icons match schema.required
        INV-D4  GUI rows exist for all data keys
        INV-D5  GUI string values match data
        INV-D6  GUI validation icons match schema violations

    InvariantCheckerSchemaEditor
        INV-S1  Text View reflects internal schema
        INV-S2  GUI rows exist for all top-level schema keys
        INV-S3  Schema title field matches internal schema
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from playwright.sync_api import Page
import jsonschema
from jsonschema import validate, ValidationError
from jsonschema.validators import validator_for
from tests.shared.python.utils import SessionMode, path_to_string
from tests.shared.python.test_panel import TestPanel
from tests.shared.python.utils_code_editor import read_text_editor_content
from tests.shared.python.utils_gui_editor import (
    read_property_exists,
    read_string_property,
    read_required_star_visible,
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

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
                if r.details:
                    print(f"    Details for {r.name}:")
                    for k, v in r.details.items():
                        print(f"      {k}: {v}")


# ---------------------------------------------------------------------------
# Base checker — shared discovery logic
# ---------------------------------------------------------------------------

class _BaseInvariantChecker:
    """
    Auto-discovers and runs every method starting with _inv_.
    Subclasses define the actual invariant methods.
    """

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
                    report.results.append(InvariantResult(
                        name=name,
                        passed=False,
                        message=f"Exception during check: {e}",
                    ))
        return report


# ---------------------------------------------------------------------------
# DataEditor invariants
# ---------------------------------------------------------------------------

class InvariantCheckerDataEditor(_BaseInvariantChecker):
    """
    Invariants for DataEditor mode.
    Ground truth: TestPanel(page, SessionMode.DataEditor).get_data()
    """

    def __init__(self, page: Page):
        super().__init__(page, SessionMode.DataEditor)

    def _property_has_error_icon(self, key: str) -> bool:
        """
        Check whether the GUI shows a validation error icon for a top-level property.
        Looks for validation-error-icon inside property-metadata-{key}.
        """
        try:
            container = self.page.get_by_test_id(f"property-metadata-{key}")
            icon = container.get_by_test_id("validation-error-icon")
            return icon.count() > 0
        except Exception:
            return False

    def _get_validation_error_messages(self, key: str) -> list[str]:
        """
        Hover over the property metadata row to trigger the overlay panel,
        then extract the red error messages from it.
        """
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
            # Dismiss overlay by pressing Escape and moving mouse away
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(100)
            self.page.mouse.move(0, 0)
            self.page.wait_for_timeout(300)
            # Wait for popover to fully disappear
            try:
                self.page.wait_for_selector(".p-popover", state="hidden", timeout=1000)
            except Exception:
                pass
            return messages
        except Exception:
            return []
    # ------------------------------------------------------------------
    # INV-D1  Text View reflects internal data
    # ------------------------------------------------------------------

    def _inv_d1_text_view_reflects_data(self) -> InvariantResult:
        """
        The text editor must display content that parses to exactly
        the same object as the internal data state.

        Skipped when format is XML (not parseable to dict).

        Grounded in: panelTextEditor.spec.ts, test_basic.py
        Selector: [id^="code-editor-dataEditor"]
        """
        name = "INV-D1 Text View reflects internal data"

        ground_truth = self.tp.get_data()
        text_data = read_text_editor_content(self.page, self.mode)

        if text_data is None:
            return InvariantResult(name, False,
                                   "Text View could not be parsed — may be XML format or empty",
                                   {"ground_truth_keys": list(ground_truth.keys())
                                   if isinstance(ground_truth, dict) else str(ground_truth)})

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

    # ------------------------------------------------------------------
    # INV-D2  schema.required fields present in data
    # ------------------------------------------------------------------

    def _inv_d2_required_fields_in_data(self) -> InvariantResult:
        """
        Every property in schema.required must exist as a key in data.

        Grounded in: check_invariant_data_schema_consistency() in test_basic.py
        View: internal state self-consistency check
        """
        name = "INV-D2 Required fields present in data"
        data = self.tp.get_data()
        schema = self.tp.get_schema()

        required = schema.get("required", [])
        if not required:
            return InvariantResult(name, True,
                                   "No required fields in schema — skip")

        if not isinstance(data, dict):
            return InvariantResult(name, False,
                                   f"Data is not an object (got {type(data).__name__})")

        missing = [r for r in required if r not in data]
        passed = not missing
        evidence = [
            f"{req}: {'present in data ✓' if req in data else 'MISSING ✗'}"
            for req in required
        ]
        return InvariantResult(
            name, passed,
            "All required fields present ✓" if passed
            else f"Missing required fields: {missing}",
            {} if passed else {
                "schema_required": required,
                "missing": missing,
                "data_keys": list(data.keys()),
            },
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # INV-D3  GUI required-star icons match schema.required
    # ------------------------------------------------------------------

    def _inv_d3_gui_required_stars_match_schema(self) -> InvariantResult:
        """
        For each top-level property in schema.properties, the GUI must
        show a required-star (*) iff that property is in schema.required.

        Grounded in: checkPropertyRequired() in panelGuiEditor.spec.ts
        Source: PropertyMetadata.vue — required-star span empty when not required
        Selector: property-metadata-{path} > required-star
        """
        name = "INV-D3 GUI required-star icons match schema"
        schema = self.tp.get_schema()
        required = set(schema.get("required", []))
        properties = schema.get("properties", {})

        if not properties:
            return InvariantResult(name, True,
                                   "No properties in schema — skip")

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
            f"{prop}: {'required ✓' if prop in required else 'optional'} — "
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

    # ------------------------------------------------------------------
    # INV-D4  GUI rows exist for all data keys
    # ------------------------------------------------------------------

    def _inv_d4_gui_rows_exist_for_data_keys(self) -> InvariantResult:
        """
        Every top-level key in data must have a visible GUI row.

        Grounded in: checkPropertyExistence() in panelGuiEditor.spec.ts
        Source: PropertyData.vue — property-data-{pathToString(path)}
        Selector: property-data-{key}
        """
        name = "INV-D4 GUI rows exist for all data keys"
        data = self.tp.get_data()

        if not isinstance(data, dict) or not data:
            return InvariantResult(name, True,
                                   "Data is empty or non-object — skip")

        missing_rows = [
            key for key in data
            if not read_property_exists(self.page, [key])
        ]

        passed = not missing_rows
        evidence = [
            f"{key}: {'row visible ✓' if key not in missing_rows else 'row missing ✗'}"
            for key in data
        ]
        return InvariantResult(
            name, passed,
            "All data keys have visible GUI rows ✓" if passed
            else f"GUI missing rows for: {missing_rows}",
            {} if passed else {
                "data_keys": list(data.keys()),
                "missing_in_gui": missing_rows,
            },
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # INV-D5  GUI string values match data
    # ------------------------------------------------------------------

    def _inv_d5_gui_string_values_match_data(self) -> InvariantResult:
        """
        For each top-level string property, the GUI text field must
        display the same value as the internal state.

        Grounded in: checkStringProperty() in panelGuiEditor.spec.ts
        Source: StringProperty.vue — input textbox inside property-data-{path}
        Selector: property-data-{key} > textbox (role)
        """
        name = "INV-D5 GUI string values match data"
        data = self.tp.get_data()
        schema = self.tp.get_schema()
        props = schema.get("properties", {})

        string_props = {
            k: v for k, v in data.items()
            if isinstance(v, str)
               and props.get(k, {}).get("type") == "string"
        }

        if not string_props:
            return InvariantResult(name, True,
                                   "No top-level string properties — skip")

        mismatches = []
        for key, expected in string_props.items():
            actual = read_string_property(self.page, [key])
            if actual is None:
                continue  # not visible — caught by INV-D4
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
                evidence.append(f"{key}: not visible — skipped")
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

    # ------------------------------------------------------------------
    # INV-D6  GUI validation icons match schema violations
    # ------------------------------------------------------------------

    def _inv_d6_gui_validation_icons_match_violations(self) -> InvariantResult:
        """
        For each top-level property, the GUI must show a
        validation-error-icon iff the value violates the schema.

        Grounded in: checkPropertySchemaViolation() in panelGuiEditor.spec.ts
        Source: PropertyMetadata.vue — validation-error-icon span v-if="isInvalid()"
        Selector: property-metadata-{path} > validation-error-icon
        """

        name = "INV-D6 GUI validation icons match violations"
        data = self.tp.get_data()
        schema = self.tp.get_schema()
        props = schema.get("properties", {})

        mismatches = []
        evidence = []  # ← new

        for key, value in data.items():
            prop_schema = props.get(key)
            if prop_schema is None:
                continue

            should_have_error = _value_violates_schema(value, prop_schema, root_schema=schema)
            gui_shows_error = self._property_has_error_icon(key)

            # Build evidence line
            status = "AGREE ✓" if (should_have_error == gui_shows_error) else "DISAGREE ✗"
            expected = "violation expected" if should_have_error else "no violation expected"
            actual = "icon shown" if gui_shows_error else "no icon shown"

            # Add violation reason when there is one
            if should_have_error:
                try:
                    from jsonschema import validate, ValidationError, RefResolver
                    prop_schema_combined = {**prop_schema, "$defs": schema.get("$defs", {})}
                    validate(instance=value, schema=prop_schema_combined)
                    reason = ""
                except ValidationError as ve:
                    reason = f" [{ve.message[:60]}]"
                except Exception:
                    reason = ""
            else:
                reason = ""

            if gui_shows_error:
                error_messages = self._get_validation_error_messages(key)
                msg_str = " | ".join(error_messages) if error_messages else ""
                evidence.append(f"{key}: {expected}, {actual} → {status}" +
                                (f" [{msg_str}]" if msg_str else ""))
            else:
                if gui_shows_error:
                    messages = self._get_validation_error_messages(key)
                    msg_str = " | ".join(messages) if messages else ""
                    suffix = f" [{msg_str}]" if msg_str else ""
                else:
                    suffix = ""
                    evidence.append(f"{key}: {expected}, {actual} → {status}{suffix}")

            if should_have_error != gui_shows_error:
                mismatches.append({
                    "property": key,
                    "value": value,
                    "should_show_error": should_have_error,
                    "gui_shows_error": gui_shows_error,
                    "violation_reason": reason.strip(" []") if reason else None,
                    "gui_check_scope": f"checked property-metadata-{key} (top-level row only — "
                                       f"does not recurse into nested fields)",
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

    # # ------------------------------------------------------------------
    # # INV-D7  No empty or orphan keys in data
    # # ------------------------------------------------------------------
    # def _inv_d7_data_keys_match_schema_properties(self) -> InvariantResult:
    #     name = "INV-D7 Data keys match schema properties"
    #     data = self.tp.get_data()
    #     schema = self.tp.get_schema()
    #
    #     if not isinstance(data, dict) or not isinstance(schema, dict):
    #         return InvariantResult(name, True, "Data or schema is not an object — skip")
    #
    #     schema_props = set(schema.get("properties", {}).keys())
    #     schema_required = set(schema.get("required", []))
    #     known_keys = schema_props | schema_required  # union of both
    #
    #     data_keys = set(data.keys())
    #     orphan_keys = data_keys - known_keys
    #
    #     if orphan_keys:
    #         return InvariantResult(name, False,
    #                                "Data contains keys not defined in schema properties or required",
    #                                {"orphan_keys": list(orphan_keys)})
    #
    #     return InvariantResult(name, True, "Data keys match schema properties ✓")


# ---------------------------------------------------------------------------
# SchemaEditor invariants
# ---------------------------------------------------------------------------

class InvariantCheckerSchemaEditor(_BaseInvariantChecker):
    """
    Invariants for SchemaEditor mode.
    Ground truth: TestPanel(page, SessionMode.SchemaEditor).get_schema()

    The Schema Editor GUI View renders the schema object itself against
    the JSON Schema Meta-Schema. Top-level keys shown are the schema's
    own keys: $schema, title, type, properties, required, $defs, etc.
    """

    def __init__(self, page: Page):
        super().__init__(page, SessionMode.SchemaEditor)
        # The user's actual schema is exposed via the DataEditor test panel
        self.tp_data = TestPanel(page, SessionMode.DataEditor)

    # ------------------------------------------------------------------
    # INV-S1  Text View reflects internal schema
    # ------------------------------------------------------------------

    def _inv_s1_text_view_reflects_schema(self) -> InvariantResult:
        """
        The schema text editor must display content that parses to
        exactly the same object as the internal schema state.

        Same logic as INV-D1 but reads get_schema() and looks at
        code-editor-schemaEditor.

        Skipped when format is XML.

        Selector: [id^="code-editor-schemaEditor"]
        """
        name = "INV-S1 Text View reflects internal schema"

        ground_truth = self.tp_data.get_schema()
        text_data = read_text_editor_content(self.page, self.mode)

        if text_data is None:
            return InvariantResult(name, False,
                                   "Schema Text View could not be parsed — may be XML or empty",
                                   {"ground_truth_keys": list(ground_truth.keys())
                                   if isinstance(ground_truth, dict) else str(ground_truth)})

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

    # ------------------------------------------------------------------
    # INV-S2  GUI rows exist for top-level schema keys
    # ------------------------------------------------------------------

    def _inv_s2_gui_rows_exist_for_schema_keys(self) -> InvariantResult:
        """
        Every top-level key in the schema object must have a visible
        GUI row in the Schema Editor GUI View.

        The Schema Editor GUI renders the schema as a form against the
        meta-schema, so top-level keys like 'title', 'type', 'properties',
        'required', '$defs' etc. each get a property-data-{key} row.

        Keys starting with '$' use the key name directly in the testid.
        e.g. '$schema' → property-data-$schema

        Selector: property-data-{key}
        """
        name = "INV-S2 GUI rows exist for top-level schema keys"
        schema = self.tp.get_schema()

        if not isinstance(schema, dict) or not schema:
            return InvariantResult(name, True,
                                   "Schema is empty or non-object — skip")

        # Some keys are always hidden or rendered differently — skip them
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

    # ------------------------------------------------------------------
    # INV-S3  Schema title field matches internal schema
    # ------------------------------------------------------------------

    def _inv_s3_schema_title_matches_internal(self) -> InvariantResult:
        """
        The 'title' field shown in the Schema Editor GUI View must match
        the title value in the internal schema state.

        This also cross-checks the toolbar display since the toolbar
        reads from the same internal state.

        Selector: property-data-title > textbox (role)
        """
        name = "INV-S3 Schema title field matches internal schema"
        schema = self.tp_data.get_schema()
        expected_title = schema.get("title")

        if expected_title is None:
            return InvariantResult(name, True,
                                   "Schema has no title — skip")

        if not isinstance(expected_title, str):
            return InvariantResult(name, True,
                                   f"Schema title is not a string (got {type(expected_title).__name__}) — skip")

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
            else f"Schema title mismatch",
            {} if passed else {
                "internal_schema": expected_title,
                "gui_shows": actual_title,
            },
            evidence=evidence,
        )

# ---------------------------------------------------------------------------
# Dispatcher — runs the right checker based on current app mode
# ---------------------------------------------------------------------------

class InvariantDispatcher:
    """
    Reads the currently active editor mode from the UI and delegates
    to the appropriate mode-specific checker.

    Usage:
        dispatcher = InvariantDispatcher(page)
        report = dispatcher.run_all()
        report.print()

    Modes:
        DataEditor   → InvariantCheckerDataEditor
        SchemaEditor → InvariantCheckerSchemaEditor
        Settings     → skipped (returns empty passing report)
    """

    def __init__(self, page: Page):
        self.page = page

    def current_mode(self) -> SessionMode | None:
        """Read the currently active mode from the UI."""
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
        """Detect current mode and run the appropriate invariants."""
        mode = self.current_mode()

        if mode == SessionMode.DataEditor:
            return InvariantCheckerDataEditor(self.page).run_all()

        if mode == SessionMode.SchemaEditor:
            return InvariantCheckerSchemaEditor(self.page).run_all()

        if mode == SessionMode.Settings:
            # Settings mode is out of scope — return empty passing report
            report = InvariantReport(mode="settings")
            report.results.append(InvariantResult(
                name="Settings mode",
                passed=True,
                message="Settings mode — invariant checks skipped",
            ))
            return report

        # Could not detect mode
        report = InvariantReport(mode="unknown")
        report.results.append(InvariantResult(
            name="Mode detection",
            passed=False,
            message="Could not detect current editor mode",
        ))
        return report


# ---------------------------------------------------------------------------
# Backwards-compatible alias
# (MutationLoop currently instantiates InvariantChecker directly)
# ---------------------------------------------------------------------------

class InvariantChecker(InvariantDispatcher):
    """
    Backwards-compatible alias for InvariantDispatcher.
    MutationLoop can keep using InvariantChecker(page) unchanged.
    The mode parameter is accepted but ignored — mode is read live
    from the UI at check time instead.
    """

    def __init__(self, page: Page, mode: SessionMode = SessionMode.DataEditor):
        super().__init__(page)
        # mode param kept for compatibility but dispatcher reads it live

    # ---------------------------------------------------------------------------

    """
    Validate a value against a JSON Schema using the jsonschema library.
    Handles $ref, propertyNames, pattern, minItems, additionalProperties etc.
    Returns True if the value violates the schema, False if valid.
    """


def _value_violates_schema(value: any, prop_schema: dict, root_schema: dict | None = None) -> bool:
    if not prop_schema:
        return False
    try:
        # Build a combined schema with $defs from root for $ref resolution
        if root_schema and "$defs" in root_schema:
            combined = {**prop_schema, "$defs": root_schema["$defs"]}
        else:
            combined = prop_schema
        validate(instance=value, schema=combined)
        return False
    except ValidationError:
        return True
    except Exception:
        return False
