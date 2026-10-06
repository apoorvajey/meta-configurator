"""Text View actions that change existing content."""
from __future__ import annotations

import json
import random
import re
from tests.shared.python.utils import SessionMode, force_editor_mode
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.discovery.values import ValueGenerator, _generate_typed_value
from tests.mutation_testing.discovery.paths import (
    _find_matching_close_bracket,
    _value_at_path,
    path_to_testid_string,
    testid_path_string_to_list,
)
from tests.mutation_testing.discovery.schema_resolution import (
    _resolve_governing_schema,
    _resolve_prop_schema_dict,
)


class TextViewModifyMixin:
    """Edit, change the type of, rename or clear content in the Text View."""

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
            print(f"   [edit-text] current format is '{current_format}', not json — "
                  "skipping (first version only handles JSON serialization/search)")
            return

        path = testid_path_string_to_list(path_str)
        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()  # the document being edited (the "data" slot on both tabs)
        try:
            current_value = _value_at_path(target, path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [edit-text] path '{path_str}' no longer resolves ({e}) — "
                  "state likely changed since discovery, skipping")
            return

        use_valid = forced_valid if isinstance(forced_valid, bool) else (random.random() < 0.8)

        # Use the type the schema declares, not the Python type of the current value: an earlier invalid write
        # can leave a wrong type in place and generation would lock onto it. Without a schema (or if the node
        # is not covered), fall back to the current value's type.
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
            # Object, array or null leaf, or unknown type: no generator, so keep or break the current value
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
                  f"near cursor (index {cursor_index}) — skipping this edit")
            return

        start_index = cursor_index + offset
        end_index = start_index + len(value_literal)

        new_literal = json.dumps(new_value)
        if not self._replace_range(mode, start_index, end_index, new_literal):
            print("   [edit-text] could not replace target text span")
            return

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
            print(f"   [change-type-text] current format is '{current_format}', not json — skipping")
            return

        path = testid_path_string_to_list(path_str)
        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()  # the document being edited (the "data" slot on both tabs)
        try:
            current_obj = _value_at_path(target, path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [change-type-text] path '{path_str}' no longer resolves ({e}) — skipping")
            return
        if not isinstance(current_obj, dict):
            print(f"   [change-type-text] '{path_str}' is no longer an object — skipping")
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
                      f"(index {cursor_index}) — skipping")
                return
        else:
            brace_start = raw_text.find("{")
            if brace_start == -1:
                print("   [change-type-text] document root is not an object — skipping")
                return

        brace_end = _find_matching_close_bracket(raw_text, brace_start, "{", "}")
        if brace_end is None:
            print("   [change-type-text] could not find matching closing brace — "
                  "malformed/truncated text, skipping")
            return

        # One atomic replace through Ace's API
        if not self._replace_range(mode, brace_start, brace_end + 1, json.dumps(new_value)):
            print("   [change-type-text] could not replace target span")
            return

    def _click_clear_text(self, mode: str) -> None:
        """Replace the whole Text View document with "{}", the way a user selects all and retypes.

        The edit is one _replace_range call, so it is a single undo step and the end state is valid JSON.
        setValue() is avoided because it resets Ace's undo manager, which the undo-based repair needs.
        It checks that the GUI View resets its rows together with the now-empty data (INV-D4) and that
        the Text View keeps matching the internal state (INV-D1, INV-S1).
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
            print(f"   [clear-text] current format is '{current_format}', not json — skipping")
            return

        raw_text = read_text_editor_raw(self.page, session_mode)
        if raw_text is None:
            print("   [clear-text] could not read raw editor text")
            return
        if not raw_text.strip():
            print("   [clear-text] document is already empty — nothing to clear, skipping")
            return

        print(f"   [clear-text] clearing entire {mode} document ({len(raw_text)} chars)")

        if not self._replace_range(mode, 0, len(raw_text), "{}"):
            print("   [clear-text] could not replace document content")
            return

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
            print(f"   [rename-child-text] current format is '{current_format}', not json — skipping")
            return

        path = testid_path_string_to_list(path_str)
        if not path or not isinstance(path[-1], str):
            print(f"   [rename-child-text] path '{path_str}' doesn't end in a string key — skipping")
            return
        parent_path, old_key = path[:-1], path[-1]

        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()  # the document being edited (the "data" slot on both tabs)
        try:
            parent_value = _value_at_path(target, parent_path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [rename-child-text] parent path no longer resolves ({e}) — skipping")
            return
        if not isinstance(parent_value, dict) or old_key not in parent_value:
            print(f"   [rename-child-text] '{path_str}' no longer exists — skipping")
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
                  "existing siblings after 10 attempts — skipping")
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
                  f"(index {cursor_index}) — skipping")
            return
        value_start, _ = span

        key_pattern = re.compile(r'"' + re.escape(old_key) + r'"\s*:\s*')
        window_start = max(0, value_start - 200)
        window = raw_text[window_start:value_start]
        matches = list(key_pattern.finditer(window))
        if not matches:
            print(f"   [rename-child-text] could not locate key '{old_key}' before its value — skipping")
            return
        key_start = window_start + matches[-1].start()
        key_end = key_start + len(old_key) + 2  # +2 for the surrounding quotes

        if not self._replace_range(mode, key_start, key_end, json.dumps(new_key)):
            print("   [rename-child-text] could not replace target key span")
            return
