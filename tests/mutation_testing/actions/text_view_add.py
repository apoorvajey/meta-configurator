"""Text View actions that add content."""
from __future__ import annotations

import json
import random
from typing import Any
from tests.shared.python.utils import SessionMode, force_editor_mode
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.schema_path_enumerator import (
    enumerate_addable_properties,
    additional_properties_allowed,
    resolve_items_schema,
    resolve_subschema_at_path,
    infer_base_type,
)
from tests.mutation_testing.discovery.paths import (
    _find_array_item_starts,
    _find_matching_close_bracket,
    _value_at_path,
    testid_path_string_to_list,
)
from tests.mutation_testing.discovery.values import _generate_typed_value
from tests.mutation_testing.discovery.schema_resolution import _resolve_governing_schema


class TextViewAddMixin:
    """Add a child property or an array item in the Text View."""

    def _click_add_child_text(self, mode: str, parent_path_str: str,
                               target_property_name: str | None = None) -> None:
        """Add a new child property to an object by editing the Text View document.

        If `target_property_name` is given (a schema property still missing here, or "*" for one outside the schema), it
        is re-checked and used. If it is None (no schema, or the node is not covered by it), name and type are arbitrary.
        The root ("") is a valid target; it has no property-data-<path> element, so the document's first '{' is used.
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
            print(f"   [add-child-text] current format is '{current_format}', not json — skipping")
            return

        path = testid_path_string_to_list(parent_path_str)
        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()  # the document being edited (the "data" slot on both tabs)
        try:
            parent_value = _value_at_path(target, path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [add-child-text] path '{parent_path_str}' no longer resolves ({e}) — skipping")
            return
        if not isinstance(parent_value, dict):
            print(f"   [add-child-text] '{parent_path_str}' is no longer an object — skipping")
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
                      f"schema property at '{parent_path_str}' — state may have shifted since "
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
                      f"'{parent_path_str}' — state may have shifted since discovery, skipping")
                return
            new_name = self._generate_property_name(parent_path_str)
            child_type, new_value = _generate_typed_value(
                ["object", "array", "boolean", "number", "string", "null"])

        else:
            # No schema loaded, or this node is not covered by it
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
                      f"(index {cursor_index}) — skipping")
                return
        else:
            brace_start = raw_text.find("{")
            if brace_start == -1:
                print("   [add-child-text] document root is not an object — skipping")
                return

        brace_end = _find_matching_close_bracket(raw_text, brace_start, "{", "}")
        if brace_end is None:
            print("   [add-child-text] could not find matching closing brace — "
                  "malformed/truncated text, skipping")
            return

        inner = raw_text[brace_start + 1:brace_end].strip()
        is_empty = inner == ""
        prefix = "" if is_empty else ", "
        insertion_text = f'{prefix}"{new_name}": {json.dumps(new_value)}'

        if not self._insert_at(mode, brace_end, insertion_text):
            print("   [add-child-text] could not insert new property")

    def _click_add_array_item_text(self, mode: str, path_str: str) -> None:
        """Insert a new item at a random position of an array in the Text View document.

        The item type comes from the array's 'items' subschema if it resolves to a concrete type, else it is arbitrary.
        There is one discovered action per array node.
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
            print(f"   [add-array-item-text] current format is '{current_format}', not json — skipping")
            return

        path = testid_path_string_to_list(path_str)
        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()  # the document being edited (the "data" slot on both tabs)
        try:
            current_array = _value_at_path(target, path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [add-array-item-text] path '{path_str}' no longer resolves ({e}) — skipping")
            return
        if not isinstance(current_array, list):
            print(f"   [add-array-item-text] '{path_str}' is no longer an array — skipping")
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
                      f"(index {cursor_index}) — skipping")
                return
        else:
            array_start = raw_text.find("[")
            if array_start == -1:
                print("   [add-array-item-text] document root is not an array — skipping")
                return

        array_end = _find_matching_close_bracket(raw_text, array_start, "[", "]")
        if array_end is None:
            print("   [add-array-item-text] could not find matching closing bracket — "
                  "malformed/truncated text, skipping")
            return

        item_starts = _find_array_item_starts(raw_text, array_start, array_end)
        actual_n = len(item_starts)
        if actual_n != n:
            print(f"   [add-array-item-text] item count mismatch (expected {n}, "
                  f"text shows {actual_n}) — state may have shifted, skipping")
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
