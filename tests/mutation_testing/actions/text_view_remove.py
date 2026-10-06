"""Text View actions that remove content."""
from __future__ import annotations

import re
from tests.shared.python.utils import SessionMode, force_editor_mode
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.discovery.paths import _value_at_path, testid_path_string_to_list


class TextViewRemoveMixin:
    """Remove a child property or an array item in the Text View."""

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
            print(f"   [remove-array-item-text] current format is '{current_format}', not json — skipping")
            return

        path = testid_path_string_to_list(path_str)
        if not path or not isinstance(path[-1], int):
            print(f"   [remove-array-item-text] path '{path_str}' doesn't end in an array index — skipping")
            return
        parent_path, idx = path[:-1], path[-1]

        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()  # the document being edited (the "data" slot on both tabs)
        try:
            parent_array = _value_at_path(target, parent_path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [remove-array-item-text] parent path no longer resolves ({e}) — skipping")
            return
        if not isinstance(parent_array, list) or not (0 <= idx < len(parent_array)):
            print(f"   [remove-array-item-text] '{path_str}' no longer exists — skipping")
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
                  f"(index {cursor_index}) — skipping")
            return
        item_start, item_end = span

        if n == 1:
            delete_start, delete_end = item_start, item_end
        elif idx == n - 1:
            comma_idx = raw_text.rfind(",", 0, item_start)
            if comma_idx == -1:
                print("   [remove-array-item-text] could not find preceding comma — skipping")
                return
            delete_start, delete_end = comma_idx, item_end
        else:
            comma_idx = raw_text.find(",", item_end)
            if comma_idx == -1:
                print("   [remove-array-item-text] could not find following comma — skipping")
                return
            delete_start, delete_end = item_start, comma_idx + 1

        if not self._delete_span(mode, delete_start, delete_end):
            print("   [remove-array-item-text] could not select/delete target span")

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
            print(f"   [remove-child-text] current format is '{current_format}', not json — skipping")
            return

        path = testid_path_string_to_list(path_str)
        if not path or not isinstance(path[-1], str):
            print(f"   [remove-child-text] path '{path_str}' doesn't end in a string key — skipping")
            return
        parent_path, key = path[:-1], path[-1]

        tp = TestPanel(self.page, session_mode)
        target = tp.get_data()  # the document being edited (the "data" slot on both tabs)
        try:
            parent_value = _value_at_path(target, parent_path)
        except (KeyError, IndexError, TypeError) as e:
            print(f"   [remove-child-text] parent path no longer resolves ({e}) — skipping")
            return
        if not isinstance(parent_value, dict) or key not in parent_value:
            print(f"   [remove-child-text] '{path_str}' no longer exists — skipping")
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
                  f"(index {cursor_index}) — skipping")
            return
        value_start, value_end = span

        key_pattern = re.compile(r'"' + re.escape(key) + r'"\s*:\s*')
        window_start = max(0, value_start - 200)
        window = raw_text[window_start:value_start]
        matches = list(key_pattern.finditer(window))
        if not matches:
            print(f"   [remove-child-text] could not locate key '{key}' before its value — skipping")
            return
        key_start = window_start + matches[-1].start()

        if len(sibling_keys) == 1:
            delete_start, delete_end = key_start, value_end
        elif idx == len(sibling_keys) - 1:
            comma_idx = raw_text.rfind(",", 0, key_start)
            if comma_idx == -1:
                print("   [remove-child-text] could not find preceding comma — skipping")
                return
            delete_start, delete_end = comma_idx, value_end
        else:
            comma_idx = raw_text.find(",", value_end)
            if comma_idx == -1:
                print("   [remove-child-text] could not find following comma — skipping")
                return
            delete_start, delete_end = key_start, comma_idx + 1

        if not self._delete_span(mode, delete_start, delete_end):
            print("   [remove-child-text] could not select/delete target span")
