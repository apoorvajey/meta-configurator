"""DOM discovery and execution for MetaConfigurator.

DOMDiscoverer lists the actions available on the page, ValueGenerator produces schema-aware values
and InteractionExecutor performs an action.
"""

from __future__ import annotations

import json
import os
import random
import re
import string
from dataclasses import dataclass
from typing import Any
import rstr

from playwright.sync_api import Page
from tests.shared.python.utils import SessionMode, force_editor_mode
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.schema_path_enumerator import (
    enumerate_addable_properties,
    additional_properties_allowed,
    resolve_items_schema,
    resolve_subschema_at_path,
    resolve_effective_schema,
    infer_base_type,
)


def capture_text_view(page: Page, mode: SessionMode) -> dict:
    """Text View state for one mode: raw editor content, parsed content (None if unparseable) and format.

    The format is only read for the active mode, because the format selector testid is not mode-specific.
    Returns all None if the editor is not mounted.
    """
    from tests.shared.python.utils_code_editor import read_text_editor_raw, read_text_editor_content

    mode_str = "dataEditor" if mode == SessionMode.DataEditor else "schemaEditor"
    has_editor = page.evaluate(
        f'() => document.querySelector(\'[id^="code-editor-{mode_str}"]\') !== null'
    )
    if not has_editor:
        return {"raw": None, "parsed": None, "format": None}

    raw = read_text_editor_raw(page, mode)
    parsed = read_text_editor_content(page, mode)

    fmt = None
    try:
        active_label = page.get_by_test_id("mode-active-true").inner_text().strip().lower()
        currently_this_mode = ("schema" in active_label) == (mode == SessionMode.SchemaEditor)
        if currently_this_mode:
            fmt = page.get_by_test_id("format-selector").inner_text().lower().strip()
    except Exception:
        pass

    return {"raw": raw, "parsed": parsed, "format": fmt}


def capture_gui_facts(page: Page) -> dict:
    """Facts about every GUI row currently in the DOM (collapsed nodes have no row): error icon shown,
    required star shown and the row's value. One page.evaluate() call.
    """
    return page.evaluate("""
        () => {
            const result = {};
            document.querySelectorAll('[data-testid^="property-metadata-"]').forEach(el => {
                const path = el.getAttribute('data-testid').replace('property-metadata-', '');
                if (!result[path]) result[path] = {};
                result[path].icon_shown = el.querySelector('[data-testid="validation-error-icon"]') !== null;
                const star = el.querySelector('[data-testid="required-star"]');
                result[path].required_star_shown = !!(star && star.textContent.trim().length > 0);
            });
            document.querySelectorAll('[data-testid^="property-data-"]').forEach(el => {
                const path = el.getAttribute('data-testid').replace('property-data-', '');
                const input = el.querySelector(':scope > input, :scope > textarea, :scope > select');
                if (input) {
                    if (!result[path]) result[path] = {};
                    result[path].value = (input.type === 'checkbox') ? input.checked : input.value;
                }
            });
            return result;
        }
    """)


def _effective_schema_to_prop_dict(eff) -> dict:
    """Flatten an EffectiveSchema into the dict ValueGenerator expects."""
    out: dict = {}
    if eff.types:
        out["type"] = next(iter(eff.types))
    if eff.pattern is not None:
        out["pattern"] = eff.pattern
    if eff.enum is not None:
        out["enum"] = eff.enum
    if eff.min_length is not None:
        out["minLength"] = eff.min_length
    if eff.max_length is not None:
        out["maxLength"] = eff.max_length
    if eff.minimum is not None:
        out["minimum"] = eff.minimum
    if eff.maximum is not None:
        out["maximum"] = eff.maximum
    return out


def _resolve_prop_schema_dict(governing_schema: dict, path: list, instance: Any) -> dict:
    """Constraints (pattern, enum, type, length, range) governing the node at `path` in `instance`.
    Shared by GUI fill and Text View edits.
    """
    if not governing_schema:
        return {}
    node_schema = resolve_subschema_at_path(governing_schema, path, instance)
    if node_schema is None:
        return {}
    try:
        leaf_value = _value_at_path(instance, path)
    except (KeyError, IndexError, TypeError):
        leaf_value = None
    eff = resolve_effective_schema(governing_schema, node_schema, leaf_value)
    return _effective_schema_to_prop_dict(eff)


# Vendored copy of MetaConfigurator's meta-schema.
with open(os.path.join(os.path.dirname(__file__), "config", "meta_schema.json"), "r", encoding="utf-8") as _f:
    META_SCHEMA_SIMPLIFIED: dict = json.load(_f)


# Category labels are shared by print_discovered() and the interactive menus.
# Text View operations have their own labels (10-16) so narrowing by category stays useful.

CATEGORY_LABELS = {
    "0_undo_redo":              "Undo or redo the last change",
    "1_expand":                 "Show or hide a section",
    "2_edit_field":             "Change a value (using the visual editor)",
    "3_field":                  "Change a dropdown or checkbox (visual editor)",
    "4_add":                    "Add something new (visual editor)",
    "5_rename":                 "Rename something (visual editor)",
    "6_remove":                 "Delete something (visual editor)",
    "7_mode":                   "Switch between Data and Schema",
    "8_format":                 "Switch file format (JSON/YAML)",
    "10_text_edit_value":       "Change a value (by typing)",
    "11_text_add_child":        "Add a new field (by typing)",
    "12_text_change_type":      "Change what kind of thing this is (by typing)",
    "13_text_remove_child":     "Delete a field (by typing)",
    "14_text_rename_child":     "Rename a field (by typing)",
    "15_text_add_array_item":   "Add a list item (by typing)",
    "16_text_remove_array_item":"Delete a list item (by typing)",
    "17_text_clear_all":        "Clear the whole document (by typing)",
    "9_other":                  "Other",
}


@dataclass
class ElementDescriptor:
    testid: str
    tag: str
    role: str
    interaction: str
    label: str = ""
    css_selector: str | None = None
    js_click: str | None = None
    mode: str = ""  # "dataEditor" or "schemaEditor"

    @property
    def view(self) -> str:
        """View bucket of this action for the mode > view > action weights.
        Schema Diagram actions are not discovered, so that bucket is never returned yet.
        """
        _text_view_prefixes = (
            "edit-text-", "add-child-text-", "change-type-text-",
            "remove-child-text-", "rename-child-text-",
            "add-array-item-text-", "remove-array-item-text-",
            "clear-text-",
        )
        if self.testid.startswith(_text_view_prefixes):
            return "Text View"
        return "GUI View"

    @property
    def property_name(self) -> str | None:
        for prefix in ("input-string-", "input-number-", "input-boolean-",
                       "property-data-", "add-item-", "add-property-"):
            if self.testid.startswith(prefix):
                return self.testid[len(prefix):]
        return None

    @property
    def category(self) -> str:
        if self.testid.startswith("expand-") or self.testid.startswith("collapse-"):
            return "1_expand"
        if self.testid.startswith(("input-string-", "input-number-", "input-boolean-")):
            return "2_edit_field"
        if self.testid.startswith("input-select-"):
            return "3_field"
        if self.testid.startswith(("add-item-", "add-property-")):
            return "4_add"
        if self.testid.startswith("rename-property-"):
            return "5_rename"
        if self.testid.startswith("remove-property-"):
            return "6_remove"
        if self.testid == "mode-active-false":
            return "7_mode"
        if self.testid == "format-selector":
            return "8_format"
        if self.testid in ("undo", "redo", "schema_undo", "schema_redo"):
            return "0_undo_redo"
        if self.testid.startswith("edit-text-"):
            return "10_text_edit_value"
        if self.testid.startswith("add-child-text-"):
            return "11_text_add_child"
        if self.testid.startswith("change-type-text-"):
            return "12_text_change_type"
        if self.testid.startswith("remove-child-text-"):
            return "13_text_remove_child"
        if self.testid.startswith("rename-child-text-"):
            return "14_text_rename_child"
        if self.testid.startswith("add-array-item-text-"):
            return "15_text_add_array_item"
        if self.testid.startswith("remove-array-item-text-"):
            return "16_text_remove_array_item"
        if self.testid.startswith("clear-text-"):
            return "17_text_clear_all"
        return "9_other"

    @property
    def input_type(self) -> str | None:
        if self.testid.startswith("input-string-"):
            return "string"
        if self.testid.startswith("input-number-"):
            return "number"
        if self.testid.startswith("input-boolean-"):
            return "boolean"
        return None

    @property
    def human_label(self) -> str:
        if self.testid.startswith("input-string-"):
            prop = self.testid[len("input-string-"):]
            return f'Change "{humanize_path(prop)}"'
        if self.testid.startswith("input-number-"):
            prop = self.testid[len("input-number-"):]
            return f'Change the number "{humanize_path(prop)}"'
        if self.testid.startswith("input-boolean-"):
            prop = self.testid[len("input-boolean-"):]
            return f'Switch "{humanize_path(prop)}" on or off'
        if self.testid.startswith("input-select-"):
            prop = self.testid[len("input-select-"):]
            return f'Choose a new value for "{humanize_path(prop)}"'
        if self.testid.startswith("expand-"):
            prop = self.testid[len("expand-"):]
            return f'Open "{humanize_path(prop)}" to see what\'s inside'
        if self.testid.startswith("collapse-"):
            prop = self.testid[len("collapse-"):]
            return f'Close "{humanize_path(prop)}"'
        if self.testid.startswith("add-item-"):
            prop = self.testid[len("add-item-"):]
            return f'Add a new item to the "{humanize_path(prop)}" list'
        if self.testid.startswith("add-property-"):
            suffix = self.testid[len("add-property-"):]
            return f'Add a new field to "{humanize_path(suffix)}"' if suffix else \
                   'Add a new field here'
        if self.testid.startswith("rename-property-"):
            prop = self.testid[len("rename-property-"):]
            return f'Rename "{humanize_path(prop)}"'
        if self.testid.startswith("remove-property-"):
            prop = self.testid[len("remove-property-"):]
            return f'Delete "{humanize_path(prop)}"'
        if self.testid == "mode-active-false":
            return "Switch between Data and Schema"
        if self.testid == "format-selector":
            return "Switch file format (JSON / YAML / XML)"
        if self.testid == "undo":
            return "Undo the last change (in Data)"
        if self.testid == "redo":
            return "Redo the last undone change (in Data)"
        if self.testid == "schema_undo":
            return "Undo the last change (in Schema)"
        if self.testid == "schema_redo":
            return "Redo the last undone change (in Schema)"
        if self.testid.startswith("edit-text-"):
            mode_and_path = self.testid[len("edit-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Change "{humanize_path(path_str)}" {mode_label} - it\'s currently {self.label}'
        if self.testid.startswith("add-child-text-"):
            mode_and_path = self.testid[len("add-child-text-"):]
            mode_str, _, rest = mode_and_path.partition(":")
            path_str, _, prop_name = rest.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            if prop_name == "*":
                return (f'Add a new field (not in the schema) to "{humanize_path(path_str)}" '
                        f'{mode_label} - it already has {self.label}')
            if prop_name:
                return (f'Add "{prop_name}" to "{humanize_path(path_str)}" '
                        f'{mode_label} - it already has {self.label}')
            return f'Add a new field to "{humanize_path(path_str)}" {mode_label} - it already has {self.label}'
        if self.testid.startswith("change-type-text-"):
            mode_and_path = self.testid[len("change-type-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Change what kind of thing "{humanize_path(path_str)}" is {mode_label} - it\'s currently {self.label}'
        if self.testid.startswith("remove-child-text-"):
            mode_and_path = self.testid[len("remove-child-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Delete "{humanize_path(path_str)}" {mode_label} - it\'s currently {self.label}'
        if self.testid.startswith("rename-child-text-"):
            mode_and_path = self.testid[len("rename-child-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Rename "{humanize_path(path_str)}" {mode_label}'
        if self.testid.startswith("add-array-item-text-"):
            mode_and_path = self.testid[len("add-array-item-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            target_desc = humanize_path(path_str)
            return f'Add a new item to the "{target_desc}" list {mode_label} - it already has {self.label}'
        if self.testid.startswith("remove-array-item-text-"):
            mode_and_path = self.testid[len("remove-array-item-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Delete "{humanize_path(path_str)}" {mode_label} - it\'s currently {self.label}'
        if self.testid.startswith("clear-text-"):
            # No path: this targets the whole document.
            mode_str = self.testid[len("clear-text-"):]
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Select everything and delete it {mode_label} - clears the whole document'
        return self.testid


_SKIP_PREFIXES = (
    "current-path",
    "submit-",
    "current-selected",
    "test-component",
    "data-input",
    "schema-input",
    "current-path-input",
    "current-selected-element-input",
    "property-metadata-",
    "required-star",
    "validation-error-icon",
    "mode-settings-button",
    # Root-level schema meta-fields are excluded: mutating them corrupts the schema for the rest of the run.
    "input-select-$ref",
    "input-select-$schema",
    "input-select-$id",
    "remove-property-$defs",
    "remove-property-$schema",
    "rename-property-$defs",
)

_CLICK_PATTERNS = (
    "mode-active-",
    "mode-settings-button",
    "add-item-",
    "add-property-",
    "remove-property-",
    "rename-property-",
    "expand-",
    "collapse-",
    "undo",
    "redo",
    "schema_undo",
    "schema_redo",
    "edit-text-",
    "add-child-text-",
    "change-type-text-",
    "remove-child-text-",
    "rename-child-text-",
    "add-array-item-text-",
    "remove-array-item-text-",
    "clear-text-",
)

_FILL_PATTERNS = (
    "input-string-",
    "input-number-",
    "input-boolean-",
)

_SELECT_PATTERNS = (
    "format-selector",
    "input-select-",
)


def path_to_testid_string(path: list) -> str:
    """Path as MetaConfigurator writes it in testids: [N] for array indexes, .key for object keys ('required[0]').
    utils.path_to_string does not match this.
    """
    result = ""
    for p in path:
        if isinstance(p, int):
            result += f"[{p}]"
        else:
            result += f".{p}"
    return result[1:] if result.startswith(".") else result


def _collect_leaf_paths(value: Any, path: list | None = None) -> list[tuple[list, Any]]:
    """(path, value) for every leaf (non-dict, non-list) of a data structure."""
    path = path or []
    leaves = []
    if isinstance(value, dict):
        for k, v in value.items():
            leaves.extend(_collect_leaf_paths(v, path + [k]))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            leaves.extend(_collect_leaf_paths(v, path + [i]))
    else:
        leaves.append((path, value))
    return leaves


def testid_path_string_to_list(path_str: str) -> list:
    """Inverse of path_to_testid_string: 'required[0]' becomes ['required', 0]."""
    tokens = re.findall(r'\.([^.\[]+)|\[(\d+)\]', "." + path_str)
    path = []
    for key_token, index_token in tokens:
        path.append(int(index_token) if index_token else key_token)
    return path


def _humanize_segment(segment: Any) -> str:
    """One path segment in plain English: 'DrivingSpeed' becomes 'Driving Speed', index N becomes 'item N+1'."""
    if isinstance(segment, int):
        return f"item {segment + 1}"
    # Space before a capital that follows a lowercase letter or digit, and between a letter and a digit run.
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', str(segment))
    s = re.sub(r'(?<=[a-zA-Z])(?=[0-9])', ' ', s)
    s = s.replace('-', ' ')
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s


def humanize_value(value: Any) -> str:
    """Plain display of a value instead of its Python repr()."""
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str):
        return f'"{value}"' if value else "an empty piece of text"
    if isinstance(value, dict):
        n = len(value)
        return "an empty group" if n == 0 else f"a group with {n} field{'s' if n != 1 else ''} inside"
    if isinstance(value, list):
        n = len(value)
        return "an empty list" if n == 0 else f"a list with {n} item{'' if n == 1 else 's'} inside"
    return str(value)


def humanize_path(path_str: str) -> str:
    """Readable breadcrumb for a testid path: 'Vehicles.car1.Speed' becomes 'Vehicles → car 1 → Speed'."""
    if not path_str:
        return "the top level"
    path = testid_path_string_to_list(path_str)
    return " → ".join(_humanize_segment(p) for p in path)


def _value_at_path(data: Any, path: list) -> Any:
    current = data
    for p in path:
        current = current[p]
    return current


def _find_matching_close_bracket(text: str, open_index: int, open_char: str, close_char: str) -> int | None:
    """Index of the bracket that closes the one at the given index, ignoring brackets inside strings. None if unmatched."""
    if text[open_index] != open_char:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(open_index, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return i
    return None


def _find_array_item_starts(text: str, bracket_start: int, bracket_end: int) -> list[int]:
    """Start index of each top-level item in a [ ... ] span. Commas inside nested values or strings do not count."""
    starts = []
    i = bracket_start + 1
    depth = 0
    in_string = False
    escape = False
    expect_item_start = True  # True right after '[' or a top-level comma
    while i < bracket_end:
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if expect_item_start and not c.isspace():
            starts.append(i)
            expect_item_start = False
        if c == '"':
            in_string = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        elif c == "," and depth == 0:
            expect_item_start = True
        i += 1
    return starts


def _generate_typed_value(allowed_kinds: list[str]) -> tuple[str, Any]:
    """Pick a random kind from allowed_kinds and generate a value of it."""
    kind = random.choice(allowed_kinds)
    if kind == "object":
        return kind, {}
    elif kind == "array":
        return kind, []
    elif kind == "boolean":
        return kind, random.choice([True, False])
    elif kind in ("number", "integer"):
        # 'integer' (from infer_base_type) uses the same branch as 'number'.
        return kind, random.randint(1, 1000)
    elif kind == "string":
        return kind, "".join(random.choices(string.ascii_letters, k=8))
    else:
        return kind, None


def _collect_container_paths(value: Any, path: list | None = None, want_type: type = dict) -> list[tuple[list, Any]]:
    """Paths to dict and list nodes, including the root."""
    path = path or []
    results = []
    if isinstance(value, want_type):
        results.append((path, value))
    if isinstance(value, dict):
        for k, v in value.items():
            results.extend(_collect_container_paths(v, path + [k], want_type))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            results.extend(_collect_container_paths(v, path + [i], want_type))
    return results


def js_click_for_testid(testid: str) -> str | None:
    """JS click snippet for testids that get_by_test_id() cannot click reliably: tree expand/collapse togglers
    and undo/redo (plain ids). None for everything else. Used by discovery and by replay.
    """
    if testid.startswith("expand-") or testid.startswith("collapse-"):
        path = testid.split("-", 1)[1]
        return f"""(function() {{
            const label = document.getElementById('_label_' + '{path}');
            if (!label) return;
            const row = label.closest('tr');
            if (!row) return;
            const btn = row.querySelector('button.p-treetable-node-toggle-button');
            if (btn) btn.click();
        }})()"""
    if testid in ("undo", "redo", "schema_undo", "schema_redo"):
        return f"document.getElementById('{testid}').click()"
    return None


def _get_current_active_mode(page: Page) -> str:
    """'dataEditor' or 'schemaEditor' for the active mode tab. GUI View testids carry no mode, so the active tab decides."""
    try:
        label = page.get_by_test_id("mode-active-true").first.inner_text(timeout=1000).strip().lower()
    except Exception:
        label = ""
    return "schemaEditor" if "schema" in label else "dataEditor"


def _infer_element_mode(testid: str, current_mode: str) -> str:
    """Mode of one discovered testid. Text View and undo/redo testids name it; GUI View testids use current_mode."""
    if testid in ("undo", "redo"):
        return "dataEditor"
    if testid in ("schema_undo", "schema_redo"):
        return "schemaEditor"
    if testid.startswith("clear-text-"):
        # no ':' suffix here; the mode is everything after the prefix
        return testid[len("clear-text-"):]
    _mode_prefixed = (
        "edit-text-", "add-child-text-", "change-type-text-",
        "remove-child-text-", "rename-child-text-",
        "add-array-item-text-", "remove-array-item-text-",
    )
    for p in _mode_prefixed:
        if testid.startswith(p):
            rest = testid[len(p):]
            mode_str, _, _ = rest.partition(":")
            return mode_str
    return current_mode


def _classify(testid: str) -> str | None:
    for p in _SKIP_PREFIXES:
        if testid.startswith(p) or testid == p.rstrip("-"):
            return None
    for p in _FILL_PATTERNS:
        if testid.startswith(p):
            return "fill"
    for p in _CLICK_PATTERNS:
        if testid.startswith(p):
            return "click"
    for p in _SELECT_PATTERNS:
        if testid == p or testid.startswith(p):
            return "select"
    return None


class DOMDiscoverer:

    def __init__(self, page: Page):
        self.page = page

    def discover(self) -> list[ElementDescriptor]:
        # Text View operations only work in JSON; in YAML they would be discovered and then silently do nothing.
        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        is_json_format = "json" in current_format

        raw: list[dict] = self.page.evaluate("""
            () => {
                const skip = [
                    'current-path', 'submit-', 'current-selected', 'test-component',
                    'data-input', 'schema-input', 'current-path-input',
                    'current-selected-element-input', 'property-metadata-',
                    'required-star', 'validation-error-icon',
                ];

                function resolveAddPropertyLabel(el) {
                    const row = el.closest('tr');
                    if (!row) return '';
                    const currentLevel = parseInt(row.getAttribute('aria-level') || '1');
                    const parentLevel = currentLevel - 1;
                    let prev = row.previousElementSibling;
                    while (prev) {
                        const prevLevel = parseInt(prev.getAttribute('aria-level') || '1');
                        const label = prev.querySelector('[id^="_label_"]');
                        if (prevLevel === parentLevel && label) {
                            return label.id.replace('_label_', '');
                        }
                        prev = prev.previousElementSibling;
                    }
                    return '';
                }

                return [...document.querySelectorAll('[data-testid]')]
                    .filter(el => {
                        if (el.offsetParent === null) return false;
                        const testid = el.getAttribute('data-testid') || '';
                        for (const s of skip) {
                            if (testid.startsWith(s) || testid === s.replace(/-$/, '')) {
                                return false;
                            }
                        }
                        if (testid.startsWith('input-select-')) {
                            if (testid.startsWith('input-select-guiEditor.')) return false;
                            if (testid.startsWith('input-select-textEditor.')) return false;
                            if (testid.startsWith('input-select-schemaDiagram.')) return false;
                            const isInputItself = el.tagName === 'INPUT' && (el.type === 'text' || !el.type);
                            return isInputItself || el.querySelector('[role="combobox"], [role="spinbutton"], .p-selectbutton, [role="textbox"], input[type="text"], input:not([type])') !== null;
                        }
                        // Skip rename/remove/add with empty path suffix
                        if ((testid === 'rename-property-') ||
                            (testid === 'remove-property-') ||
                            (testid === 'add-property-')) return false;
                        return true;
                    })
                    .map(el => {
                        let testid = el.getAttribute('data-testid');
                        if (testid === 'add-property-') {
                            const parentPath = resolveAddPropertyLabel(el);
                            testid = 'add-property-' + parentPath;
                        }
                        return {
                            testid: el.getAttribute('data-testid'),  // ← use getAttribute directly, not the variable
                            tag:    el.tagName.toLowerCase(),
                            role:   el.getAttribute('role') || '',
                            value:  el.value || el.innerText?.slice(0, 40) || '',
                        };
                    });
            }
        """)

        expand_raw: list[dict] = self.page.evaluate("""
            () => {
                return [...document.querySelectorAll('button.p-treetable-node-toggle-button')]
                    .filter(btn => btn.style.visibility !== 'hidden' && btn.offsetParent !== null)
                    .map(btn => {
                        const row = btn.closest('tr');
                        const label = row?.querySelector('[id^="_label_"]');
                        const path = label?.id?.replace('_label_', '');
                        if (!path || path.includes('..') || path.endsWith('.')) return null;

                        const nextRow = row?.nextElementSibling;
                        const nextToggler = nextRow?.querySelector('button.p-treetable-node-toggle-button');
                        const currentMargin = parseFloat(btn.style.marginLeft) || 0;
                        const nextMargin = nextToggler ? parseFloat(nextToggler.style.marginLeft) || 0 : -1;
                        const isExpanded = nextMargin > currentMargin;

                        return {
                            testid: (isExpanded ? 'collapse-' : 'expand-') + path,
                            tag:    'button',
                            role:   '',
                            value:  '',
                        };
                    })
                    .filter(x => x !== null);
            }
        """)

        # undo/redo have plain ids (schema_undo and schema_redo in the Schema Editor), not data-testid,
        # so they are found by id and clicked with js_click. Settings undo/redo are out of scope.
        undo_redo_raw: list[dict] = self.page.evaluate("""
            () => {
                return ['undo', 'redo', 'schema_undo', 'schema_redo']
                    .map(id => document.getElementById(id))
                    .filter(el => el && !el.disabled && el.offsetParent !== null)
                    .map(el => ({
                        testid: el.id,
                        tag:    el.tagName.toLowerCase(),
                        role:   el.getAttribute('role') || '',
                        value:  '',
                    }));
            }
        """)

        # Text View discovery, one pass per mode. Every editable leaf, object node and array node becomes its own action
        # so it appears in the available-actions list.
        edit_text_raw: list[dict] = []
        add_child_text_raw: list[dict] = []
        change_type_text_raw: list[dict] = []
        remove_child_text_raw: list[dict] = []
        rename_child_text_raw: list[dict] = []
        add_array_item_text_raw: list[dict] = []
        remove_array_item_text_raw: list[dict] = []
        clear_text_raw: list[dict] = []

        for mode_str, session_mode in (
            ("dataEditor", SessionMode.DataEditor),
            ("schemaEditor", SessionMode.SchemaEditor),
        ):
            has_editor = self.page.evaluate(
                f'() => document.querySelector(\'[id^="code-editor-{mode_str}"]\') !== null'
            )
            if not has_editor or not is_json_format:
                continue
            try:
                tp = TestPanel(self.page, session_mode)
                target = tp.get_data()  # the document being edited; the 'data' slot on the Schema tab as well (see governing_schema below)
            except Exception:
                continue

            try:
                leaves = _collect_leaf_paths(target)
            except Exception:
                leaves = []
            for leaf_path, leaf_value in leaves:
                path_str = path_to_testid_string(leaf_path)
                if not path_str:
                    continue  # root-level primitive, no path to click
                edit_text_raw.append({
                    "testid": f"edit-text-{mode_str}:{path_str}",
                    "tag": "div",
                    "role": "",
                    "value": humanize_value(leaf_value),
                })

            try:
                objects = _collect_container_paths(target, want_type=dict)
            except Exception:
                objects = []

            # Governing schema for the targets: the user's schema in the Data Editor, the vendored meta-schema in the Schema Editor.
            # The walked document is read with get_data() on both tabs; get_schema() on the Schema tab returns the meta-schema.
            if mode_str == "dataEditor":
                try:
                    governing_schema = TestPanel(self.page, SessionMode.DataEditor).get_schema()
                except Exception:
                    governing_schema = None
            else:
                governing_schema = META_SCHEMA_SIMPLIFIED

            for obj_path, obj_value in objects:
                path_str = path_to_testid_string(obj_path)

                node_schema = None
                if governing_schema is not None:
                    try:
                        node_schema = resolve_subschema_at_path(governing_schema, obj_path, target)
                    except Exception:
                        node_schema = None

                if node_schema is not None:
                    try:
                        addable = enumerate_addable_properties(governing_schema, node_schema, obj_value)
                        allow_generic = additional_properties_allowed(governing_schema, node_schema, obj_value)
                    except Exception:
                        addable, allow_generic = [], True
                    for prop_name, _child_schema in addable:
                        add_child_text_raw.append({
                            "testid": f"add-child-text-{mode_str}:{path_str}:{prop_name}",
                            "tag": "div",
                            "role": "",
                            "value": f"{len(obj_value)} existing propert{'y' if len(obj_value) == 1 else 'ies'}",
                        })
                    if allow_generic:
                        add_child_text_raw.append({
                            "testid": f"add-child-text-{mode_str}:{path_str}:*",
                            "tag": "div",
                            "role": "",
                            "value": f"{len(obj_value)} existing propert{'y' if len(obj_value) == 1 else 'ies'}",
                        })
                else:
                    # No schema, or the node is not covered by it: arbitrary name and type, decided at execution time.
                    add_child_text_raw.append({
                        "testid": f"add-child-text-{mode_str}:{path_str}",
                        "tag": "div",
                        "role": "",
                        "value": f"{len(obj_value)} existing propert{'y' if len(obj_value) == 1 else 'ies'}",
                    })

                # Root-level change-type on the Schema tab is skipped: it can turn the schema into a non-object,
                # which breaks everything that reads it. On the Data tab it is a normal, checkable violation.
                is_schema_root = (mode_str == "schemaEditor" and not obj_path)
                if not is_schema_root:
                    change_type_text_raw.append({
                        "testid": f"change-type-text-{mode_str}:{path_str}",
                        "tag": "div",
                        "role": "",
                        "value": f"currently an object with {len(obj_value)} propert"
                                 f"{'y' if len(obj_value) == 1 else 'ies'}",
                    })
                for key in obj_value.keys():
                    child_path_str = path_to_testid_string(obj_path + [key])
                    remove_child_text_raw.append({
                        "testid": f"remove-child-text-{mode_str}:{child_path_str}",
                        "tag": "div",
                        "role": "",
                        "value": humanize_value(obj_value[key]),
                    })
                    rename_child_text_raw.append({
                        "testid": f"rename-child-text-{mode_str}:{child_path_str}",
                        "tag": "div",
                        "role": "",
                        "value": f'"{key}"',
                    })

            try:
                arrays = _collect_container_paths(target, want_type=list)
            except Exception:
                arrays = []
            for arr_path, arr_value in arrays:
                path_str = path_to_testid_string(arr_path)
                add_array_item_text_raw.append({
                    "testid": f"add-array-item-text-{mode_str}:{path_str}",
                    "tag": "div",
                    "role": "",
                    "value": f"{len(arr_value)} existing item{'' if len(arr_value) == 1 else 's'}",
                })
                for i, item_value in enumerate(arr_value):
                    item_path_str = path_to_testid_string(arr_path + [i])
                    remove_array_item_text_raw.append({
                        "testid": f"remove-array-item-text-{mode_str}:{item_path_str}",
                        "tag": "div",
                        "role": "",
                        "value": humanize_value(item_value),
                    })

            # Whole-document clear: one action per mode, only if there is something to clear.
            has_content = bool(target) if isinstance(target, (dict, list)) else target is not None
            if has_content:
                clear_text_raw.append({
                    "testid": f"clear-text-{mode_str}",
                    "tag": "div",
                    "role": "",
                    "value": f"{len(target)} top-level key(s)" if isinstance(target, dict) else "content",
                })

        current_mode = _get_current_active_mode(self.page)

        result: list[ElementDescriptor] = []
        for r in (raw + expand_raw + undo_redo_raw + edit_text_raw + add_child_text_raw
                  + change_type_text_raw + remove_child_text_raw + rename_child_text_raw
                  + add_array_item_text_raw + remove_array_item_text_raw + clear_text_raw):
            interaction = _classify(r["testid"])
            if interaction is None:
                continue
            result.append(ElementDescriptor(
                testid=r["testid"],
                tag=r["tag"],
                role=r["role"],
                interaction=interaction,
                label=r["value"] if r["testid"].startswith("edit-text-") else r["testid"],
                js_click=js_click_for_testid(r["testid"]),
                mode=_infer_element_mode(r["testid"], current_mode),
            ))

        result = [el for el in result if el.testid != "mode-active-true"]

        seen = set()
        deduped = []
        for el in result:
            if el.testid.startswith("mode-active-"):
                if el.testid in seen:
                    continue
                seen.add(el.testid)
            deduped.append(el)
        return deduped

    def print_discovered(self, elements: list[ElementDescriptor] | None = None):
        els = elements if elements is not None else self.discover()
        sorted_els = sorted(els, key=lambda e: e.category)
        print(f"\nDiscovered {len(els)} interactable elements:")
        current_category = None
        for el in sorted_els:
            if el.category != current_category:
                print(f"\n  ── {CATEGORY_LABELS.get(el.category, 'Other')}")
                current_category = el.category
            print(f"  [{el.interaction:6}] {el.human_label}")
            print(f"             → {el.testid}  (tag={el.tag})")
        return els


_FAKE_STRINGS = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo",
    "Test_123", "hello world", "foo-bar", "value_42",
    "", "x" * 50,
]

_INVALID_TYPE_STRINGS = [
    "NOT_A_NUMBER", "null", "undefined", "true",
    "<script>alert(1)</script>", "12345678901234567890",
    "  ",
]

# Readable word pool for generated strings, not tied to the vocabulary of any one schema.
_WORD_BANK = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Falcon", "Garnet",
    "Harbor", "Indigo", "Juniper", "Kestrel", "Lumen", "Meadow",
    "Nimbus", "Orchid", "Pioneer", "Quartz", "Raven", "Summit",
    "Terra", "Umber", "Vertex", "Willow", "Xenon", "Yonder", "Zephyr",
]


def _meaningful_name(min_len: int, max_len: int) -> str:
    """Readable Word_Word style name, length clamped to [min_len, max_len]."""
    name = random.choice(_WORD_BANK)
    if random.random() < 0.5:
        name += "_" + random.choice(_WORD_BANK)
    if len(name) < min_len:
        name += "_" + str(random.randint(1, 999))
    return name[:max_len] if max_len else name


class ValueGenerator:

    def __init__(self, schema: dict | None = None, instance: Any = None):
        """schema: the governing schema (the user's schema in the Data Editor, the meta-schema in the Schema Editor).
        instance: the current document, needed to resolve if/then/else.
        """
        self.governing_schema = schema or {}
        self.instance = instance if instance is not None else {}

    def _prop_schema(self, property_name: str) -> dict:
        if not property_name or not self.governing_schema:
            return {}
        path = testid_path_string_to_list(property_name)
        return _resolve_prop_schema_dict(self.governing_schema, path, self.instance)

    def value_for_type(self, prop_schema: dict, declared_type: str | None, valid: bool) -> Any:
        """Value of a known type for an already-resolved prop_schema.
        Returns a Python value for json.dumps(), not a button label.
        """
        if declared_type == "string":
            return self._string_value(prop_schema, valid)
        if declared_type in ("number", "integer"):
            return self._number_value({**prop_schema, "type": declared_type}, valid)
        if declared_type == "boolean":
            return self._boolean_value_typed(prop_schema, valid)
        return None

    def for_element(self, el: ElementDescriptor, force_valid: bool | None = None) -> Any:
        if el.interaction == "select" and el.testid.startswith("input-select-"):
            prop_name = el.testid[len("input-select-"):]
            prop_schema = self._prop_schema(prop_name)
            if prop_schema.get("type") == "string":
                use_valid = force_valid if force_valid is not None else (random.random() < 0.5)
                return self._string_value(prop_schema, use_valid)
            # meta-schema string fields without a schema definition (e.g. $id, description, title)
            if el.tag == "input" and not prop_schema:
                use_valid = force_valid if force_valid is not None else (random.random() < 0.5)
                return self._string_value({}, use_valid)
        if el.interaction != "fill":
            return None

        use_valid = force_valid if force_valid is not None else (random.random() < 0.5)
        prop_name   = el.property_name
        prop_schema = self._prop_schema(prop_name) if prop_name else {}

        if el.input_type == "string":
            return self._string_value(prop_schema, use_valid)
        if el.input_type == "number":
            return self._number_value(prop_schema, use_valid)
        if el.input_type == "boolean":
            return self._boolean_value(prop_schema, use_valid)

        return random.choice(_FAKE_STRINGS) if use_valid else random.choice(_INVALID_TYPE_STRINGS)

    def _string_value(self, prop_schema: dict, valid: bool) -> str:
        enum_vals    = prop_schema.get("enum")
        pattern      = prop_schema.get("pattern")
        real_min_len = prop_schema.get("minLength")  # None if the schema does not constrain this
        real_max_len = prop_schema.get("maxLength")
        gen_min_len  = real_min_len if real_min_len is not None else 1
        gen_max_len  = real_max_len if real_max_len is not None else 30

        if valid:
            if enum_vals:
                return random.choice(enum_vals)
            if pattern:
                candidate = self._satisfy_pattern(pattern)
                if candidate is not None:
                    return candidate
                print(f"Could not build a value guaranteed to satisfy pattern {pattern!r} "
                      f"- using a plain name instead; NOT verified schema-valid")
            return _meaningful_name(gen_min_len, gen_max_len)

        # Only produce an invalid value where there is a constraint to break.
        if enum_vals:
            candidates = [v for v in _INVALID_TYPE_STRINGS if v not in enum_vals]
            return random.choice(candidates) if candidates else "NOT_IN_ENUM_" + str(random.randint(1, 999))

        if pattern:
            for candidate in _INVALID_TYPE_STRINGS:
                if not re.search(pattern, candidate):
                    return candidate
            return "###PATTERN_VIOLATION###"

        if real_min_len:
            return "x" * max(real_min_len - 1, 0)
        if real_max_len is not None:
            return "x" * (real_max_len + 1)

        # No enum, pattern or length bound: nothing can make this string invalid.
        return _meaningful_name(gen_min_len, gen_max_len)

    def _satisfy_pattern(self, pattern: str) -> str | None:
        """String matching a regex, generated with rstr. None if rstr cannot handle the pattern or the result does not match."""
        try:
            candidate = rstr.xeger(pattern)
        except Exception:
            return None
        return candidate if re.search(pattern, candidate) else None

    def _number_value(self, prop_schema: dict, valid: bool) -> float | int | str:
        is_integer   = prop_schema.get("type") == "integer"
        real_minimum = prop_schema.get("minimum")
        real_maximum = prop_schema.get("maximum")
        minimum = real_minimum if real_minimum is not None else 0
        maximum = real_maximum if real_maximum is not None else 1000

        if not valid:
            # Only produce an invalid number where a real bound exists; a bound from the schema is certain to be violated.
            if real_minimum is not None:
                return real_minimum - (1 if is_integer else 0.01)
            if real_maximum is not None:
                return real_maximum + (1 if is_integer else 0.01)
            return random.choice(["NOT_A_NUMBER", "null", ""])

        if is_integer:
            return random.randint(int(minimum), int(maximum))
        val = minimum + random.random() * (maximum - minimum)
        return round(val, 2)

    def _boolean_value(self, prop_schema: dict, valid: bool) -> str:
        """Button label ('true' or 'false') for the GUI toggle. An invalid pick is only possible when the schema allows a single value (const or a one-item enum)."""
        const_val = prop_schema.get("const")
        enum_vals = prop_schema.get("enum")
        restricted = None
        if const_val is not None and isinstance(const_val, bool):
            restricted = [const_val]
        elif enum_vals and set(enum_vals) <= {True, False} and len(enum_vals) == 1:
            restricted = list(enum_vals)

        if restricted:
            disallowed = [b for b in (True, False) if b not in restricted]
            if not valid and disallowed:
                return str(random.choice(disallowed)).lower()
            return str(random.choice(restricted)).lower()

        return random.choice(["true", "false"])

    def _boolean_value_typed(self, prop_schema: dict, valid: bool) -> Any:
        """Text View version of _boolean_value: a Python value for json.dumps(). A type violation is possible here."""
        const_val = prop_schema.get("const")
        enum_vals = prop_schema.get("enum")
        allowed = [const_val] if const_val is not None else (enum_vals or [True, False])

        if valid:
            return random.choice(allowed)

        disallowed_bool = [b for b in (True, False) if b not in allowed]
        if disallowed_bool:
            return random.choice(disallowed_bool)
        return random.choice(["notabool", 1, 0, None])

    def select_option(self) -> str:
        return random.choices(["json", "yaml"], weights=[5, 3])[0]


_FORMAT_OPTIONS = ["json", "yaml"]  # formatRegistry only registers json and yaml


def _resolve_governing_schema(page: Page, mode: str) -> dict | None:
    """Schema governing the tab: the user's schema in the Data Editor, the meta-schema in the Schema Editor. Read fresh at execution time."""
    if mode == "dataEditor":
        try:
            schema = TestPanel(page, SessionMode.DataEditor).get_schema()
        except Exception:
            return None
        return schema if schema else None
    return META_SCHEMA_SIMPLIFIED


class InteractionExecutor:

    def __init__(self, page: Page):
        self.page = page
        self._last_rename   = ""
        self._current_schema: dict | None = None
        self._silent_no_toggle_count = 0
        # (parent_path, new_name) of the property just created by _click_add_property, used by create-then-fill.
        # Reset at the start of every call.
        self._last_added_property_parent_path = ""
        self._last_added_property_name = ""

    def execute(self, el: ElementDescriptor, value: Any = None,
                schema: dict | None = None) -> None:
        self._current_schema = schema

        if el.interaction == "fill":
            self._fill(el, value)
        elif el.interaction == "click":
            self._click(el, value)
        elif el.interaction == "select":
            self._select(el, value)
        else:
            raise ValueError(f"Unknown interaction type '{el.interaction}' for {el.testid}")

        self.page.wait_for_timeout(400)

    def _get_prop_schema(self, testid: str) -> dict:
        """Schema of a property from its full-path testid, e.g. 'input-select-Vehicles.bus1.DrivingSpeed'."""
        schema = self._current_schema or {}
        if not schema:
            return {}

        for prefix in ("input-select-", "input-string-", "input-number-"):
            if testid.startswith(prefix):
                path = testid[len(prefix):]
                break
        else:
            return {}

        parts = path.replace("[", ".").replace("]", "").split(".")
        current = schema
        for part in parts:
            if not part:
                continue
            if "properties" in current and part in current["properties"]:
                current = current["properties"][part]
            elif "additionalProperties" in current and isinstance(current["additionalProperties"], dict):
                current = current["additionalProperties"]
                if "properties" in current and part in current["properties"]:
                    current = current["properties"][part]
            elif "items" in current and isinstance(current["items"], dict):
                current = current["items"]
                if "properties" in current and part in current["properties"]:
                    current = current["properties"][part]
            else:
                return {}
        return current if isinstance(current, dict) else {}

    def _generate_property_name(self, parent_path: str) -> str:
        """Property name shaped by the parent's propertyNames pattern, using words from _WORD_BANK.
        Casing is guessed from the pattern text, so the name is not guaranteed to match the pattern or exist in the schema.
        """
        schema = self._current_schema or {}
        if not parent_path:
            prop_names_schema = schema.get("propertyNames", {})
            pattern = prop_names_schema.get("pattern", "")
        else:
            parts = parent_path.replace("[", ".").replace("]", "").split(".")
            parent_schema = schema
            for part in parts:
                if not part:
                    continue
                parent_schema = (
                        parent_schema.get("properties", {}).get(part) or
                        parent_schema.get("additionalProperties") or
                        {}
                )
            prop_names_schema = parent_schema.get("propertyNames", {})
            pattern = prop_names_schema.get("pattern", "")

        word = random.choice(_WORD_BANK)

        if not pattern:
            return word.lower()

        has_upper = "A-Z" in pattern
        has_lower = "a-z" in pattern

        if has_upper and has_lower:
            upper_pos = pattern.index("A-Z")
            lower_pos = pattern.index("a-z")
            if upper_pos < lower_pos:
                # capital first, e.g. ^[A-Z][a-z]+[0-9]*$
                return word[0].upper() + word[1:].lower()
        if has_lower:
            # all lowercase, e.g. ^[a-z]+[0-9]*$
            return word.lower()

        return word.lower()

    def _fill(self, el: ElementDescriptor, value: Any) -> None:
        if el.input_type == "boolean":
            btn_label = str(value).lower() if value is not None else "true"
            container = self.page.get_by_test_id(el.testid)
            btn = container.get_by_role("button", name=btn_label)
            btn.click()
            return

        locator = self.page.get_by_test_id(el.testid)

        # numeric testids (array items) need .first to avoid a strict-mode violation
        if el.property_name and el.property_name.isdigit():
            locator = locator.first

        try:
            current = locator.input_value(timeout=2000)
            print(f"   field '{el.testid}': '{current}' → '{value}'")
        except Exception:
            print(f"   field '{el.testid}': (unreadable) → '{value}'")

        locator.click()
        locator.fill(str(value) if value is not None else "")
        locator.press("Enter")

    def _rename_property(self, el: ElementDescriptor, forced_name: str | None = None) -> None:
        path = el.testid[len("rename-property-"):]
        label_id = f"_label_{path}"
        parts = path.split(".")
        parent_path = ".".join(parts[:-1]) if len(parts) > 1 else ""
        new_name = forced_name if forced_name else self._generate_property_name(parent_path)

        pencil = self.page.get_by_test_id(el.testid)
        pencil.scroll_into_view_if_needed()
        pencil.hover()
        self.page.wait_for_timeout(100)
        pencil.click(timeout=5000)
        self.page.wait_for_timeout(200)

        try:
            self.page.wait_for_selector(
                f'[id="{label_id}"][contenteditable="true"]', timeout=2000
            )
            label = self.page.locator(f'[id="{label_id}"][contenteditable="true"]')
        except Exception:
            label = self.page.locator(f'[id="{label_id}"]').first

        label.click()
        self.page.keyboard.press("Control+a")
        self.page.keyboard.type(new_name)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(300)

        self._last_rename = new_name
        print(f"   renamed '{path}' → '{new_name}'")

    def _click_add_item_required(self) -> None:
        self.page.evaluate("""
            () => {
                const label = document.getElementById('_label_required');
                if (!label) return;
                const row = label.closest('tr');
                if (!row) return;
                const nextRow = row.nextElementSibling;
                const toggle = row.querySelector('button.p-treetable-node-toggle-button');
                if (!toggle) return;
                const currentMargin = parseFloat(toggle.style.marginLeft) || 0;
                const nextToggle = nextRow?.querySelector('button.p-treetable-node-toggle-button');
                const nextMargin = nextToggle ? (parseFloat(nextToggle.style.marginLeft) || 0) : -1;
                const isExpanded = nextMargin > currentMargin;
                if (!isExpanded) toggle.click();
            }
        """)
        self.page.wait_for_timeout(300)

        before_testids = set(self.page.evaluate("""
            () => [...document.querySelectorAll('[data-testid*="required["]')]
                .map(el => el.getAttribute('data-testid'))
        """))

        self.page.get_by_test_id("add-item-required").first.click()
        self.page.wait_for_timeout(600)

        after: list[dict] = self.page.evaluate("""
            () => [...document.querySelectorAll('[data-testid*="required["]')]
                .map(el => ({testid: el.getAttribute('data-testid'), value: el.value}))
        """)
        print(f"   [add-item-required] inputs found: {after}")

        new_entries = [i for i in after if i["testid"] not in before_testids]
        target = new_entries[0] if new_entries else next(
            (i for i in after if i["value"] == ""), None
        )

        if target:
            testid = target["testid"]
            if testid.startswith("property-data-") or testid.startswith("property-metadata-"):
                data_testid = testid.replace("property-metadata-", "property-data-")
                inp = self.page.get_by_test_id(data_testid).locator("input, textarea").first
            else:
                inp = self.page.get_by_test_id(testid)

            name = self._generate_property_name("")
            print(f"   [add-item-required] filling '{testid}' (real input located) with '{name}'")
            inp.click()
            inp.fill(name)
            inp.press("Tab")
            self.page.wait_for_timeout(400)
        else:
            print("   [add-item-required] no new required[N] input found - "
                  "click may have silently failed")

    # Composition keywords are structural wrappers, never collapsible tree nodes; _ensure_path_visible skips them.
    _COMPOSITION_KEYWORDS = {"allOf", "anyOf", "oneOf", "not", "if", "then", "else"}
    _DEFS_PREFIX_RE = re.compile(r"^(\$defs|definitions)\.(.+)$")

    def _split_defs_prefix(self, path: list) -> list:
        """Split '$defs.name' into '$defs' and 'name': the tree shows $defs as its own row, which _ensure_path_visible has to expand."""
        if path and isinstance(path[0], str):
            m = self._DEFS_PREFIX_RE.match(path[0])
            if m:
                return [m.group(1), m.group(2), *path[1:]]
        return path

    def _ensure_path_visible(self, path: list) -> None:
        path = self._split_defs_prefix(path)
        for i in range(1, len(path)):
            segment = path[i - 1]
            if isinstance(segment, str) and segment in self._COMPOSITION_KEYWORDS:
                continue

            ancestor_str = path_to_testid_string(path[:i])
            if not ancestor_str:
                continue

            state = self.page.evaluate(f"""
                () => {{
                    const label = document.getElementById('_label_' + {json.dumps(ancestor_str)});
                    if (!label) return 'missing';
                    const row = label.closest('tr');
                    if (!row) return 'missing';
                    const btn = row.querySelector('button.p-treetable-node-toggle-button');
                    if (!btn || btn.style.visibility === 'hidden' || btn.offsetParent === null) return 'no-toggle';
                    const nextRow = row.nextElementSibling;
                    const nextToggler = nextRow?.querySelector('button.p-treetable-node-toggle-button');
                    const currentMargin = parseFloat(btn.style.marginLeft) || 0;
                    const nextMargin = nextToggler ? (parseFloat(nextToggler.style.marginLeft) || 0) : -1;
                    return (nextMargin > currentMargin) ? 'expanded' : 'collapsed';
                }}
            """)

            if state == "collapsed":
                self.page.evaluate(f"""
                    () => {{
                        const label = document.getElementById('_label_' + {json.dumps(ancestor_str)});
                        const row = label?.closest('tr');
                        const btn = row?.querySelector('button.p-treetable-node-toggle-button');
                        if (btn) btn.click();
                    }}
                """)
                # Wait for the child row to mount; a fixed sleep was too short for some $defs subtrees.
                next_str = path_to_testid_string(path[:i + 1])
                try:
                    self.page.wait_for_function(
                        f"document.getElementById('_label_' + {json.dumps(next_str)}) !== null",
                        timeout=3000,
                    )
                except Exception:
                    self.page.wait_for_timeout(300)
            elif state == "expanded":
                pass
            elif state == "no-toggle":
                # Not a collapsible node: tallied instead of printed, so a deep path does not print one line per ancestor.
                self._silent_no_toggle_count += 1
            else:
                print(f"   [ensure-visible] ancestor '{ancestor_str}' row not found in DOM "
                      f"- parent likely hadn't finished expanding, or this path isn't "
                      f"rendered as its own row")

    def report_ensure_visible_summary(self) -> None:
        """Print the tally of skipped toggles as one line at the end of a run."""
        if self._silent_no_toggle_count:
            print(f"\n[ensure-visible] {self._silent_no_toggle_count} ancestor(s) had no "
                  f"toggle button this run (composition keywords already excluded above)")

    def _click_edit_text(self, mode: str, path_str: str, forced_valid: bool | None = None) -> None:
        from tests.shared.python.utils_code_editor import read_text_editor_raw

        session_mode = SessionMode.DataEditor if mode == "dataEditor" else SessionMode.SchemaEditor

        force_editor_mode(self.page, session_mode)
        self.page.wait_for_timeout(400)
        self.page.get_by_text("Text View", exact=True).click(timeout=5000)
        self.page.wait_for_timeout(300)

        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        if "json" not in current_format:
            print(f"   [edit-text] current format is '{current_format}', not json - "
                  "skipping (first version only handles JSON serialization/search)")
            return

        path = testid_path_string_to_list(path_str)
        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()
        try:
            current_value = _value_at_path(target, path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [edit-text] path '{path_str}' no longer resolves ({e}) - "
                  "state likely changed since discovery, skipping")
            return

        use_valid = forced_valid if isinstance(forced_valid, bool) else (random.random() < 0.8)

        # Use the schema constraint for this leaf, not the Python type of the current value: an earlier invalid write can leave
        # a wrong type there and generation would keep following it. The Python type is only a fallback when no schema applies.
        governing_schema = _resolve_governing_schema(self.page, mode)
        prop_schema = _resolve_prop_schema_dict(governing_schema, path, target) if governing_schema else {}
        declared_type = prop_schema.get("type") or (
            "boolean" if isinstance(current_value, bool) else
            "integer" if isinstance(current_value, int) else
            "number"  if isinstance(current_value, float) else
            "string"  if isinstance(current_value, str) else None
        )

        if declared_type in ("string", "number", "integer", "boolean"):
            gen = ValueGenerator(schema=governing_schema, instance=target)
            new_value = gen.value_for_type(prop_schema, declared_type, use_valid)
        else:
            # object, array or null leaf, or unknown type: no schema-driven generator, use a safe fallback.
            if use_valid:
                new_value = current_value
            else:
                invalid_pool = [v for v in ["NOT_A_VALID_TYPE", -999999, None, True] if v != current_value]
                new_value = random.choice(invalid_pool or ["NOT_A_VALID_TYPE"])

        print(f"   [edit-text] target path='{path_str}' current={current_value!r} "
              f"new={new_value!r} (valid={use_valid}, type={declared_type})")

        self._ensure_path_visible(path)
        selector_testid = f"property-data-{path_str}"
        try:
            self.page.get_by_test_id(selector_testid).first.click(timeout=5000)
        except Exception as e:
            print(f"   [edit-text] could not click '{selector_testid}' to sync cursor: {e}")
            return
        self.page.wait_for_timeout(300)

        raw_text = read_text_editor_raw(self.page, session_mode)
        if raw_text is None:
            print("   [edit-text] could not read raw editor text")
            return

        cursor_index = self.page.evaluate(f"""
            () => {{
                const editorEl = [...document.querySelectorAll('[id^="code-editor-{mode}"]')][0];
                if (!editorEl) return null;
                const editor = window.ace?.edit(editorEl.id);
                if (!editor) return null;
                return editor.session.doc.positionToIndex(editor.getCursorPosition(), 0);
            }}
        """)
        if cursor_index is None:
            print("   [edit-text] could not determine cursor position")
            return

        value_literal = json.dumps(current_value)
        search_window = raw_text[cursor_index:cursor_index + 300]
        offset = search_window.find(value_literal)
        if offset == -1:
            print(f"   [edit-text] could not locate value literal {value_literal!r} "
                  f"near cursor (index {cursor_index}) - skipping this edit")
            return

        start_index = cursor_index + offset
        end_index = start_index + len(value_literal)

        new_literal = json.dumps(new_value)
        if not self._replace_range(mode, start_index, end_index, new_literal):
            print("   [edit-text] could not replace target text span")
            return

    def _get_ace_cursor_index(self, mode: str) -> int | None:
        return self.page.evaluate(f"""
            () => {{
                const editorEl = [...document.querySelectorAll('[id^="code-editor-{mode}"]')][0];
                if (!editorEl) return null;
                const editor = window.ace?.edit(editorEl.id);
                if (!editor) return null;
                return editor.session.doc.positionToIndex(editor.getCursorPosition(), 0);
            }}
        """)

    def _replace_range(self, mode: str, start_index: int, end_index: int, new_text: str) -> bool:
        """Replace [start_index, end_index) through Ace's session.replace(), not keystrokes.

        Typing a bracket pair goes through Ace's auto-closing and leaves a stray bracket ('"Vehicles": []]').
        session.replace() edits the document model directly and is a single undo entry.
        Use it (or _insert_at) for every Text View edit that writes JSON.
        """
        result = self.page.evaluate(f"""
            () => {{
                const editorEl = [...document.querySelectorAll('[id^="code-editor-{mode}"]')][0];
                if (!editorEl) return false;
                const editor = window.ace?.edit(editorEl.id);
                if (!editor) return false;
                const startPos = editor.session.doc.indexToPosition({start_index}, 0);
                const endPos = editor.session.doc.indexToPosition({end_index}, 0);
                editor.session.replace({{start: startPos, end: endPos}}, {json.dumps(new_text)});
                editor.focus();
                return true;
            }}
        """)
        if not result:
            return False
        self.page.wait_for_timeout(400)
        return True

    def _insert_at(self, mode: str, index: int, text: str) -> bool:
        """Insert text at one position via Ace's session.insert(), for the same reason as _replace_range."""
        result = self.page.evaluate(f"""
            () => {{
                const editorEl = [...document.querySelectorAll('[id^="code-editor-{mode}"]')][0];
                if (!editorEl) return false;
                const editor = window.ace?.edit(editorEl.id);
                if (!editor) return false;
                const pos = editor.session.doc.indexToPosition({index}, 0);
                editor.session.insert(pos, {json.dumps(text)});
                editor.focus();
                return true;
            }}
        """)
        if not result:
            return False
        self.page.wait_for_timeout(400)
        return True

    def _click_add_child_text(self, mode: str, parent_path_str: str,
                               target_property_name: str | None = None) -> None:
        """Add a child property to an object in the Text View.

        With target_property_name (a schema-known property still missing, or "*" for out-of-schema) that name is used.
        Otherwise the name and type are generated. The root has no property-data element, so the first '{' is used
        instead of a cursor-sync click.
        """
        from tests.shared.python.utils_code_editor import read_text_editor_raw

        session_mode = SessionMode.DataEditor if mode == "dataEditor" else SessionMode.SchemaEditor

        force_editor_mode(self.page, session_mode)
        self.page.wait_for_timeout(400)
        self.page.get_by_text("Text View", exact=True).click(timeout=5000)
        self.page.wait_for_timeout(300)

        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        if "json" not in current_format:
            print(f"   [add-child-text] current format is '{current_format}', not json - skipping")
            return

        path = testid_path_string_to_list(parent_path_str)
        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()
        try:
            parent_value = _value_at_path(target, path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [add-child-text] path '{parent_path_str}' no longer resolves ({e}) - skipping")
            return
        if not isinstance(parent_value, dict):
            print(f"   [add-child-text] '{parent_path_str}' is no longer an object - skipping")
            return

        governing_schema = _resolve_governing_schema(self.page, mode)

        new_name: str | None = None
        child_type: str | None = None
        new_value: Any = None

        if governing_schema is not None and target_property_name not in (None, "*"):
            node_schema = resolve_subschema_at_path(governing_schema, path, target)
            addable_now = (dict(enumerate_addable_properties(governing_schema, node_schema, parent_value))
                           if node_schema is not None else {})
            if target_property_name not in addable_now:
                print(f"   [add-child-text] '{target_property_name}' is no longer a valid/addable "
                      f"schema property at '{parent_path_str}' - state may have shifted since "
                      f"discovery, skipping")
                return
            new_name = target_property_name
            base_type = infer_base_type(governing_schema, addable_now[target_property_name], sample_instance={})
            child_type, new_value = _generate_typed_value(
                [base_type] if base_type else ["object", "array", "boolean", "number", "string", "null"])

        elif target_property_name == "*" and governing_schema is not None:
            node_schema = resolve_subschema_at_path(governing_schema, path, target)
            if node_schema is not None and not additional_properties_allowed(governing_schema, node_schema, parent_value):
                print(f"   [add-child-text] out-of-schema properties no longer allowed at "
                      f"'{parent_path_str}' - state may have shifted since discovery, skipping")
                return
            new_name = self._generate_property_name(parent_path_str)
            child_type, new_value = _generate_typed_value(
                ["object", "array", "boolean", "number", "string", "null"])

        else:
            new_name = self._generate_property_name(parent_path_str)
            child_type, new_value = _generate_typed_value(
                ["object", "array", "boolean", "number", "string", "null"])

        print(f"   [add-child-text] parent='{parent_path_str}' new_name='{new_name}' "
              f"type={child_type} value={new_value!r}"
              f"{' (schema-driven)' if target_property_name not in (None, '*') else ''}")

        raw_text = read_text_editor_raw(self.page, session_mode)
        if raw_text is None:
            print("   [add-child-text] could not read raw editor text")
            return

        if parent_path_str:
            self._ensure_path_visible(path)
            selector_testid = f"property-data-{parent_path_str}"
            try:
                self.page.get_by_test_id(selector_testid).first.click(timeout=5000)
            except Exception as e:
                print(f"   [add-child-text] could not click '{selector_testid}' to sync cursor: {e}")
                return
            self.page.wait_for_timeout(300)
            cursor_index = self._get_ace_cursor_index(mode)
            if cursor_index is None:
                print("   [add-child-text] could not determine cursor position")
                return
            brace_start = raw_text.find("{", cursor_index)
            if brace_start == -1 or brace_start > cursor_index + 50:
                print(f"   [add-child-text] could not locate opening brace near cursor "
                      f"(index {cursor_index}) - skipping")
                return
        else:
            brace_start = raw_text.find("{")
            if brace_start == -1:
                print("   [add-child-text] document root is not an object - skipping")
                return

        brace_end = _find_matching_close_bracket(raw_text, brace_start, "{", "}")
        if brace_end is None:
            print("   [add-child-text] could not find matching closing brace - "
                  "malformed/truncated text, skipping")
            return

        inner = raw_text[brace_start + 1:brace_end].strip()
        is_empty = inner == ""
        prefix = "" if is_empty else ", "
        insertion_text = f'{prefix}"{new_name}": {json.dumps(new_value)}'

        if not self._insert_at(mode, brace_end, insertion_text):
            print("   [add-child-text] could not insert new property")

    def _click_change_type_text(self, mode: str, path_str: str) -> None:
        from tests.shared.python.utils_code_editor import read_text_editor_raw

        session_mode = SessionMode.DataEditor if mode == "dataEditor" else SessionMode.SchemaEditor

        force_editor_mode(self.page, session_mode)
        self.page.wait_for_timeout(400)
        self.page.get_by_text("Text View", exact=True).click(timeout=5000)
        self.page.wait_for_timeout(300)

        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        if "json" not in current_format:
            print(f"   [change-type-text] current format is '{current_format}', not json - skipping")
            return

        path = testid_path_string_to_list(path_str)
        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()
        try:
            current_obj = _value_at_path(target, path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [change-type-text] path '{path_str}' no longer resolves ({e}) - skipping")
            return
        if not isinstance(current_obj, dict):
            print(f"   [change-type-text] '{path_str}' is no longer an object - skipping")
            return

        new_kind = random.choice(["array", "primitive"])
        if new_kind == "array":
            new_value = []
        else:
            _, new_value = _generate_typed_value(["boolean", "number", "string", "null"])

        print(f"   [change-type-text] target='{path_str}' was object with "
              f"{len(current_obj)} propert{'y' if len(current_obj) == 1 else 'ies'} "
              f"-> {new_kind} ({new_value!r})")

        raw_text = read_text_editor_raw(self.page, session_mode)
        if raw_text is None:
            print("   [change-type-text] could not read raw editor text")
            return

        if path_str:
            self._ensure_path_visible(path)
            selector_testid = f"property-data-{path_str}"
            try:
                self.page.get_by_test_id(selector_testid).first.click(timeout=5000)
            except Exception as e:
                print(f"   [change-type-text] could not click '{selector_testid}' to sync cursor: {e}")
                return
            self.page.wait_for_timeout(300)
            cursor_index = self._get_ace_cursor_index(mode)
            if cursor_index is None:
                print("   [change-type-text] could not determine cursor position")
                return
            brace_start = raw_text.find("{", cursor_index)
            if brace_start == -1 or brace_start > cursor_index + 50:
                print(f"   [change-type-text] could not locate opening brace near cursor "
                      f"(index {cursor_index}) - skipping")
                return
        else:
            brace_start = raw_text.find("{")
            if brace_start == -1:
                print("   [change-type-text] document root is not an object - skipping")
                return

        brace_end = _find_matching_close_bracket(raw_text, brace_start, "{", "}")
        if brace_end is None:
            print("   [change-type-text] could not find matching closing brace - "
                  "malformed/truncated text, skipping")
            return

        if not self._replace_range(mode, brace_start, brace_end + 1, json.dumps(new_value)):
            print("   [change-type-text] could not replace target span")
            return

    def _click_add_array_item_text(self, mode: str, path_str: str) -> None:
        """Insert a new item into an array at a random position. The type comes from the array's items schema
        when it resolves to a concrete type, otherwise it is arbitrary.
        """
        from tests.shared.python.utils_code_editor import read_text_editor_raw

        session_mode = SessionMode.DataEditor if mode == "dataEditor" else SessionMode.SchemaEditor

        force_editor_mode(self.page, session_mode)
        self.page.wait_for_timeout(400)
        self.page.get_by_text("Text View", exact=True).click(timeout=5000)
        self.page.wait_for_timeout(300)

        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        if "json" not in current_format:
            print(f"   [add-array-item-text] current format is '{current_format}', not json - skipping")
            return

        path = testid_path_string_to_list(path_str)
        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()
        try:
            current_array = _value_at_path(target, path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [add-array-item-text] path '{path_str}' no longer resolves ({e}) - skipping")
            return
        if not isinstance(current_array, list):
            print(f"   [add-array-item-text] '{path_str}' is no longer an array - skipping")
            return

        n = len(current_array)
        position = random.randint(0, n)

        item_type = None
        governing_schema = _resolve_governing_schema(self.page, mode)
        if governing_schema is not None:
            array_schema = resolve_subschema_at_path(governing_schema, path, target)
            items_schema = (resolve_items_schema(governing_schema, array_schema, current_array)
                             if array_schema is not None else None)
            base_type = (infer_base_type(governing_schema, items_schema, sample_instance={})
                         if items_schema is not None else None)
            if base_type:
                item_type, new_value = _generate_typed_value([base_type])

        if item_type is None:
            item_type, new_value = _generate_typed_value(
                ["object", "array", "boolean", "number", "string", "null"]
            )

        print(f"   [add-array-item-text] target='{path_str}' has {n} item(s), "
              f"inserting {item_type} ({new_value!r}) at position {position}")

        raw_text = read_text_editor_raw(self.page, session_mode)
        if raw_text is None:
            print("   [add-array-item-text] could not read raw editor text")
            return

        if path_str:
            self._ensure_path_visible(path)
            selector_testid = f"property-data-{path_str}"
            try:
                self.page.get_by_test_id(selector_testid).first.click(timeout=5000)
            except Exception as e:
                print(f"   [add-array-item-text] could not click '{selector_testid}' to sync cursor: {e}")
                return
            self.page.wait_for_timeout(300)
            cursor_index = self._get_ace_cursor_index(mode)
            if cursor_index is None:
                print("   [add-array-item-text] could not determine cursor position")
                return
            array_start = raw_text.find("[", cursor_index)
            if array_start == -1 or array_start > cursor_index + 50:
                print(f"   [add-array-item-text] could not locate opening bracket near cursor "
                      f"(index {cursor_index}) - skipping")
                return
        else:
            array_start = raw_text.find("[")
            if array_start == -1:
                print("   [add-array-item-text] document root is not an array - skipping")
                return

        array_end = _find_matching_close_bracket(raw_text, array_start, "[", "]")
        if array_end is None:
            print("   [add-array-item-text] could not find matching closing bracket - "
                  "malformed/truncated text, skipping")
            return

        item_starts = _find_array_item_starts(raw_text, array_start, array_end)
        actual_n = len(item_starts)
        if actual_n != n:
            print(f"   [add-array-item-text] item count mismatch (expected {n}, "
                  f"text shows {actual_n}) - state may have shifted, skipping")
            return

        if actual_n == 0:
            insert_at = array_start + 1
            insert_text = json.dumps(new_value)
        elif position < actual_n:
            insert_at = item_starts[position]
            insert_text = f"{json.dumps(new_value)}, "
        else:
            insert_at = array_end
            insert_text = f", {json.dumps(new_value)}"

        if not self._insert_at(mode, insert_at, insert_text):
            print("   [add-array-item-text] could not insert new item")

    def _click_remove_array_item_text(self, mode: str, path_str: str) -> None:
        from tests.shared.python.utils_code_editor import read_text_editor_raw

        session_mode = SessionMode.DataEditor if mode == "dataEditor" else SessionMode.SchemaEditor

        force_editor_mode(self.page, session_mode)
        self.page.wait_for_timeout(400)
        self.page.get_by_text("Text View", exact=True).click(timeout=5000)
        self.page.wait_for_timeout(300)

        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        if "json" not in current_format:
            print(f"   [remove-array-item-text] current format is '{current_format}', not json - skipping")
            return

        path = testid_path_string_to_list(path_str)
        if not path or not isinstance(path[-1], int):
            print(f"   [remove-array-item-text] path '{path_str}' doesn't end in an array index - skipping")
            return
        parent_path, idx = path[:-1], path[-1]

        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()
        try:
            parent_array = _value_at_path(target, parent_path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [remove-array-item-text] parent path no longer resolves ({e}) - skipping")
            return
        if not isinstance(parent_array, list) or not (0 <= idx < len(parent_array)):
            print(f"   [remove-array-item-text] '{path_str}' no longer exists - skipping")
            return

        n = len(parent_array)
        current_value = parent_array[idx]

        print(f"   [remove-array-item-text] target='{path_str}' current={current_value!r} "
              f"(item {idx+1}/{n})")

        self._ensure_path_visible(path)
        selector_testid = f"property-data-{path_str}"
        try:
            self.page.get_by_test_id(selector_testid).first.click(timeout=5000)
        except Exception as e:
            print(f"   [remove-array-item-text] could not click '{selector_testid}' to sync cursor: {e}")
            return
        self.page.wait_for_timeout(300)

        raw_text = read_text_editor_raw(self.page, session_mode)
        if raw_text is None:
            print("   [remove-array-item-text] could not read raw editor text")
            return

        cursor_index = self._get_ace_cursor_index(mode)
        if cursor_index is None:
            print("   [remove-array-item-text] could not determine cursor position")
            return

        span = self._locate_value_span(raw_text, current_value, cursor_index)
        if span is None:
            print(f"   [remove-array-item-text] could not locate item near cursor "
                  f"(index {cursor_index}) - skipping")
            return
        item_start, item_end = span

        if n == 1:
            delete_start, delete_end = item_start, item_end
        elif idx == n - 1:
            comma_idx = raw_text.rfind(",", 0, item_start)
            if comma_idx == -1:
                print("   [remove-array-item-text] could not find preceding comma - skipping")
                return
            delete_start, delete_end = comma_idx, item_end
        else:
            comma_idx = raw_text.find(",", item_end)
            if comma_idx == -1:
                print("   [remove-array-item-text] could not find following comma - skipping")
                return
            delete_start, delete_end = item_start, comma_idx + 1

        if not self._delete_span(mode, delete_start, delete_end):
            print("   [remove-array-item-text] could not select/delete target span")

    def _click_clear_text(self, mode: str) -> None:
        """Replace the whole document with "{}" through _replace_range.

        setValue() would reset Ace's undo history and break the walk-back repair. A replace is one normal undo entry,
        which the loop's immediate auto-undo relies on. The end state is valid JSON, so the app state should update to {}
        and the GUI View should clear its rows (INV-D4).
        """
        from tests.shared.python.utils_code_editor import read_text_editor_raw

        session_mode = SessionMode.DataEditor if mode == "dataEditor" else SessionMode.SchemaEditor

        force_editor_mode(self.page, session_mode)
        self.page.wait_for_timeout(400)
        self.page.get_by_text("Text View", exact=True).click(timeout=5000)
        self.page.wait_for_timeout(300)

        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        if "json" not in current_format:
            print(f"   [clear-text] current format is '{current_format}', not json - skipping")
            return

        raw_text = read_text_editor_raw(self.page, session_mode)
        if raw_text is None:
            print("   [clear-text] could not read raw editor text")
            return
        if not raw_text.strip():
            print("   [clear-text] document is already empty - nothing to clear, skipping")
            return

        print(f"   [clear-text] clearing entire {mode} document ({len(raw_text)} chars)")

        if not self._replace_range(mode, 0, len(raw_text), "{}"):
            print("   [clear-text] could not replace document content")
            return


    def _delete_span(self, mode: str, start_index: int, end_index: int) -> bool:
        selected = self.page.evaluate(f"""
            () => {{
                const editorEl = [...document.querySelectorAll('[id^="code-editor-{mode}"]')][0];
                if (!editorEl) return false;
                const editor = window.ace?.edit(editorEl.id);
                if (!editor) return false;
                const startPos = editor.session.doc.indexToPosition({start_index}, 0);
                const endPos = editor.session.doc.indexToPosition({end_index}, 0);
                editor.getSelection().setSelectionRange({{start: startPos, end: endPos}});
                editor.focus();
                return true;
            }}
        """)
        if not selected:
            return False
        self.page.wait_for_timeout(200)
        self.page.keyboard.press("Delete")
        self.page.wait_for_timeout(400)
        return True

    def _locate_value_span(self, raw_text: str, value: Any, search_start: int) -> tuple[int, int] | None:
        if isinstance(value, dict):
            start = raw_text.find("{", search_start)
            if start == -1 or start > search_start + 50:
                return None
            end = _find_matching_close_bracket(raw_text, start, "{", "}")
            return (start, end + 1) if end is not None else None
        elif isinstance(value, list):
            start = raw_text.find("[", search_start)
            if start == -1 or start > search_start + 50:
                return None
            end = _find_matching_close_bracket(raw_text, start, "[", "]")
            return (start, end + 1) if end is not None else None
        else:
            literal = json.dumps(value)
            window = raw_text[search_start:search_start + 300]
            offset = window.find(literal)
            if offset == -1:
                return None
            start = search_start + offset
            return (start, start + len(literal))

    def _click_remove_child_text(self, mode: str, path_str: str) -> None:
        from tests.shared.python.utils_code_editor import read_text_editor_raw

        session_mode = SessionMode.DataEditor if mode == "dataEditor" else SessionMode.SchemaEditor

        force_editor_mode(self.page, session_mode)
        self.page.wait_for_timeout(400)
        self.page.get_by_text("Text View", exact=True).click(timeout=5000)
        self.page.wait_for_timeout(300)

        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        if "json" not in current_format:
            print(f"   [remove-child-text] current format is '{current_format}', not json - skipping")
            return

        path = testid_path_string_to_list(path_str)
        if not path or not isinstance(path[-1], str):
            print(f"   [remove-child-text] path '{path_str}' doesn't end in a string key - skipping")
            return
        parent_path, key = path[:-1], path[-1]

        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()
        try:
            parent_value = _value_at_path(target, parent_path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [remove-child-text] parent path no longer resolves ({e}) - skipping")
            return
        if not isinstance(parent_value, dict) or key not in parent_value:
            print(f"   [remove-child-text] '{path_str}' no longer exists - skipping")
            return

        sibling_keys = list(parent_value.keys())
        idx = sibling_keys.index(key)
        current_value = parent_value[key]

        print(f"   [remove-child-text] target='{path_str}' current={current_value!r} "
              f"(sibling {idx+1}/{len(sibling_keys)})")

        selector_testid = f"property-data-{path_str}"
        self._ensure_path_visible(path)

        try:
            self.page.get_by_test_id(selector_testid).first.click(timeout=5000)
        except Exception as e:
            print(f"   [remove-child-text] could not click '{selector_testid}' to sync cursor: {e}")
            return
        self.page.wait_for_timeout(300)

        raw_text = read_text_editor_raw(self.page, session_mode)
        if raw_text is None:
            print("   [remove-child-text] could not read raw editor text")
            return

        cursor_index = self._get_ace_cursor_index(mode)
        if cursor_index is None:
            print("   [remove-child-text] could not determine cursor position")
            return

        span = self._locate_value_span(raw_text, current_value, cursor_index)
        if span is None:
            print(f"   [remove-child-text] could not locate value near cursor "
                  f"(index {cursor_index}) - skipping")
            return
        value_start, value_end = span

        key_pattern = re.compile(r'"' + re.escape(key) + r'"\s*:\s*')
        window_start = max(0, value_start - 200)
        window = raw_text[window_start:value_start]
        matches = list(key_pattern.finditer(window))
        if not matches:
            print(f"   [remove-child-text] could not locate key '{key}' before its value - skipping")
            return
        key_start = window_start + matches[-1].start()

        if len(sibling_keys) == 1:
            delete_start, delete_end = key_start, value_end
        elif idx == len(sibling_keys) - 1:
            comma_idx = raw_text.rfind(",", 0, key_start)
            if comma_idx == -1:
                print("   [remove-child-text] could not find preceding comma - skipping")
                return
            delete_start, delete_end = comma_idx, value_end
        else:
            comma_idx = raw_text.find(",", value_end)
            if comma_idx == -1:
                print("   [remove-child-text] could not find following comma - skipping")
                return
            delete_start, delete_end = key_start, comma_idx + 1

        if not self._delete_span(mode, delete_start, delete_end):
            print("   [remove-child-text] could not select/delete target span")

    def _click_rename_child_text(self, mode: str, path_str: str) -> None:
        from tests.shared.python.utils_code_editor import read_text_editor_raw

        session_mode = SessionMode.DataEditor if mode == "dataEditor" else SessionMode.SchemaEditor

        force_editor_mode(self.page, session_mode)
        self.page.wait_for_timeout(400)
        self.page.get_by_text("Text View", exact=True).click(timeout=5000)
        self.page.wait_for_timeout(300)

        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        if "json" not in current_format:
            print(f"   [rename-child-text] current format is '{current_format}', not json - skipping")
            return

        path = testid_path_string_to_list(path_str)
        if not path or not isinstance(path[-1], str):
            print(f"   [rename-child-text] path '{path_str}' doesn't end in a string key - skipping")
            return
        parent_path, old_key = path[:-1], path[-1]

        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()
        try:
            parent_value = _value_at_path(target, parent_path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [rename-child-text] parent path no longer resolves ({e}) - skipping")
            return
        if not isinstance(parent_value, dict) or old_key not in parent_value:
            print(f"   [rename-child-text] '{path_str}' no longer exists - skipping")
            return

        sibling_keys = set(parent_value.keys())
        current_value = parent_value[old_key]

        parent_path_str = path_to_testid_string(parent_path)
        new_key = self._generate_property_name(parent_path_str)
        attempts = 0
        while new_key in sibling_keys and attempts < 10:
            new_key = self._generate_property_name(parent_path_str)
            attempts += 1
        if new_key in sibling_keys:
            print("   [rename-child-text] could not generate a name distinct from "
                  "existing siblings after 10 attempts - skipping")
            return

        print(f"   [rename-child-text] target='{path_str}' old_key='{old_key}' "
              f"new_key='{new_key}' value={current_value!r}")

        selector_testid = f"property-data-{path_str}"
        self._ensure_path_visible(path)

        try:
            self.page.get_by_test_id(selector_testid).first.click(timeout=5000)
        except Exception as e:
            print(f"   [rename-child-text] could not click '{selector_testid}' to sync cursor: {e}")
            return
        self.page.wait_for_timeout(300)

        raw_text = read_text_editor_raw(self.page, session_mode)
        if raw_text is None:
            print("   [rename-child-text] could not read raw editor text")
            return

        cursor_index = self._get_ace_cursor_index(mode)
        if cursor_index is None:
            print("   [rename-child-text] could not determine cursor position")
            return

        span = self._locate_value_span(raw_text, current_value, cursor_index)
        if span is None:
            print(f"   [rename-child-text] could not locate value near cursor "
                  f"(index {cursor_index}) - skipping")
            return
        value_start, _ = span

        key_pattern = re.compile(r'"' + re.escape(old_key) + r'"\s*:\s*')
        window_start = max(0, value_start - 200)
        window = raw_text[window_start:value_start]
        matches = list(key_pattern.finditer(window))
        if not matches:
            print(f"   [rename-child-text] could not locate key '{old_key}' before its value - skipping")
            return
        key_start = window_start + matches[-1].start()
        key_end = key_start + len(old_key) + 2  # +2 for the surrounding quotes

        if not self._replace_range(mode, key_start, key_end, json.dumps(new_key)):
            print("   [rename-child-text] could not replace target key span")
            return

    def _click(self, el: ElementDescriptor, value: Any = None) -> None:
        if el.js_click:
            self.page.evaluate(el.js_click)
            return

        if el.testid.startswith("add-property-") and el.testid != "add-property-":
            self._click_add_property(el, forced_name=value)
            return
        if el.testid.startswith("rename-property-"):
            self._rename_property(el, forced_name=value)
            return

        if el.testid == "add-item-required":
            self._click_add_item_required()
            return

        if el.testid.startswith("edit-text-"):
            mode_str, _, path_str = el.testid[len("edit-text-"):].partition(":")
            self._click_edit_text(mode_str, path_str, forced_valid=value)
            return

        if el.testid.startswith("add-child-text-"):
            mode_and_path = el.testid[len("add-child-text-"):]
            mode_str, _, rest = mode_and_path.partition(":")
            path_str, _, prop_name = rest.partition(":")
            self._click_add_child_text(mode_str, path_str, target_property_name=(prop_name or None))
            return

        if el.testid.startswith("change-type-text-"):
            mode_str, _, path_str = el.testid[len("change-type-text-"):].partition(":")
            self._click_change_type_text(mode_str, path_str)
            return

        if el.testid.startswith("remove-child-text-"):
            mode_str, _, path_str = el.testid[len("remove-child-text-"):].partition(":")
            self._click_remove_child_text(mode_str, path_str)
            return

        if el.testid.startswith("rename-child-text-"):
            mode_str, _, path_str = el.testid[len("rename-child-text-"):].partition(":")
            self._click_rename_child_text(mode_str, path_str)
            return

        if el.testid.startswith("add-array-item-text-"):
            mode_str, _, path_str = el.testid[len("add-array-item-text-"):].partition(":")
            self._click_add_array_item_text(mode_str, path_str)
            return

        if el.testid.startswith("remove-array-item-text-"):
            mode_str, _, path_str = el.testid[len("remove-array-item-text-"):].partition(":")
            self._click_remove_array_item_text(mode_str, path_str)
            return

        if el.testid.startswith("clear-text-"):
            mode_str = el.testid[len("clear-text-"):]
            self._click_clear_text(mode_str)
            return

        self.page.get_by_test_id(el.testid).first.click()

    def _click_add_property(self, el: ElementDescriptor, forced_name: str | None = None) -> None:
        print(f"   [add-property-debug] testid={el.testid} forced_name={forced_name!r}")
        parent_path = el.testid[len("add-property-"):]
        self._last_added_property_parent_path = ""
        self._last_added_property_name = ""

        self.page.evaluate(f"""
            () => {{
                const spans = [...document.querySelectorAll('[data-testid="add-property-"]')];
                for (const span of spans) {{
                    const row = span.closest('tr');
                    if (!row) continue;
                    let contextDepth = -1;
                    let p = row.previousElementSibling;
                    while (p) {{
                        const tog = p.querySelector('button.p-treetable-node-toggle-button');
                        if (tog) {{ contextDepth = parseFloat(tog.style.marginLeft) || 0; break; }}
                        p = p.previousElementSibling;
                    }}
                    let prev = row.previousElementSibling;
                    while (prev) {{
                        const toggler = prev.querySelector('button.p-treetable-node-toggle-button');
                        const label   = prev.querySelector('[id^="_label_"]');
                        if (toggler && label) {{
                            const prevMargin = parseFloat(toggler.style.marginLeft) || 0;
                            if (prevMargin < contextDepth) {{
                                const resolvedPath = label.id.replace('_label_', '');
                                if (resolvedPath === '{parent_path}') {{
                                    span.click();
                                    return;
                                }}
                                break;
                            }}
                        }}
                        prev = prev.previousElementSibling;
                    }}
                }}
            }}
        """)

        self.page.wait_for_timeout(400)

        new_name = forced_name if forced_name else self._generate_property_name(parent_path)
        print(f"   [name-gen] path='{parent_path}' → '{new_name}'")

        try:
            self.page.wait_for_selector('[contenteditable="true"]', timeout=3000)
        except Exception:
            print("   [name-gen] contenteditable never appeared - name may not be set")

        self.page.evaluate("""
            () => {
                const editables = [...document.querySelectorAll('[contenteditable="true"]')];
                const active = editables[editables.length - 1];
                if (active) {
                    active.focus();
                    document.execCommand('selectAll', false, null);
                }
            }
        """)
        self.page.wait_for_timeout(200)
        self.page.keyboard.type(new_name)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(300)
        print(f"   new property named '{new_name}'")
        self._last_added_property_parent_path = parent_path
        self._last_added_property_name = new_name

    def _select(self, el: ElementDescriptor, value: Any = None) -> None:
        if el.testid == "format-selector":
            self._select_format(value)
        elif el.testid.startswith("input-select-"):
            self._select_dropdown(el, value)
        else:
            self.page.get_by_test_id(el.testid).first.click()

    def _select_dropdown(self, el: ElementDescriptor, value: Any = None) -> None:
        container   = self.page.get_by_test_id(el.testid).first
        spinbutton  = container.get_by_role("spinbutton")
        selectbutton= container.locator(".p-selectbutton")
        combobox    = container.get_by_role("combobox")

        if spinbutton.count() > 0:
            prop_schema = self._get_prop_schema(el.testid)
            self._fill_number(el, spinbutton, prop_schema)
        elif selectbutton.count() > 0:
            self._fill_boolean(selectbutton)
        elif combobox.count() > 0:
            self._fill_enum(combobox)
        else:
            tag = self.page.get_by_test_id(el.testid).first.evaluate("el => el.tagName.toLowerCase()")
            if tag == "input":
                self._fill_string(el, self.page.get_by_test_id(el.testid).first, value)
            else:
                textbox = container.get_by_role("textbox")
                if textbox.count() > 0:
                    self._fill_string(el, textbox, value)
                else:
                    self.page.keyboard.press("Escape")

    def _fill_number(self, el: ElementDescriptor, spinbutton, prop_schema: dict = None) -> None:
        prop_schema = prop_schema or {}
        minimum = prop_schema.get("minimum", 0)
        maximum = prop_schema.get("maximum", 100)
        multiple_of = prop_schema.get("multipleOf")
        is_integer = prop_schema.get("type") == "integer"
        use_valid = random.random() < 0.5

        if not use_valid:
            invalid_choices = [
                maximum + 100,
                minimum - 100,
                round(random.uniform(maximum, maximum * 2), 2),
            ]
            if multiple_of:
                invalid_choices.append(multiple_of * random.randint(1, 10) + 0.5)
            value = random.choice(invalid_choices)
        elif multiple_of:
            min_mult = int(minimum / multiple_of) + (1 if minimum % multiple_of else 0)
            max_mult = int(maximum / multiple_of)
            value = random.randint(min_mult, max_mult) * multiple_of if min_mult <= max_mult else multiple_of
        elif is_integer:
            value = random.randint(int(minimum), int(maximum))
        else:
            value = round(random.uniform(minimum, maximum), 2)

        try:
            current = spinbutton.input_value(timeout=2000)
            print(f"   field '{el.testid}': '{current}' → '{value}'")
        except Exception:
            print(f"   field '{el.testid}': (unreadable) → '{value}'")
        spinbutton.click()
        spinbutton.fill(str(value))
        spinbutton.press("Enter")

    def _fill_boolean(self, selectbutton) -> None:
        choice = random.choice(["true", "false"])
        print(f"   boolean → '{choice}'")
        btn = selectbutton.get_by_role("button", name=choice)
        if btn.count() > 0:
            btn.first.click()

    def _fill_enum(self, combobox) -> None:
        combobox.first.click()
        self.page.wait_for_timeout(300)
        options = self.page.locator(".p-select-list .p-select-option")
        count = options.count()
        if count > 0:
            options.nth(random.randint(0, count - 1)).click()
        else:
            self.page.keyboard.press("Escape")

    def _fill_string(self, el: ElementDescriptor, textbox, value: Any) -> None:
        try:
            current = textbox.input_value(timeout=2000)
            print(f"   field '{el.testid}': '{current}' → '{value}'")
        except Exception:
            print(f"   field '{el.testid}': (unreadable) → '{value}'")

        textbox.click()
        textbox.press("Control+a")
        textbox.fill(str(value) if value is not None else "")
        textbox.press("Tab")
        self.page.wait_for_timeout(300)

        try:
            after = textbox.input_value(timeout=1000)
            if after != str(value):
                print("   [fill-retry] Tab didn't commit - trying Enter")
                textbox.click()
                textbox.press("Control+a")
                textbox.fill(str(value) if value is not None else "")
                textbox.press("Enter")
                self.page.wait_for_timeout(300)
        except Exception:
            pass

    def _select_format(self, format_value: Any = None) -> None:
        selector = self.page.get_by_test_id("format-selector")
        try:
            current = selector.inner_text().lower().strip()
            for fmt in _FORMAT_OPTIONS:
                if fmt in current:
                    current = fmt
                    break
            else:
                current = "json"
        except Exception:
            current = "json"

        if format_value is None:
            choices = [f for f in _FORMAT_OPTIONS if f != current]
            format_value = random.choice(choices)

        if format_value == current:
            return

        selector.get_by_role("combobox").click()
        self.page.get_by_role("option", name=format_value).click()
        self.page.wait_for_timeout(300)