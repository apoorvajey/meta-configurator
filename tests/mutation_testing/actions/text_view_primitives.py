"""Low-level edits of the Text View document through Ace's API."""
from __future__ import annotations

import json
from typing import Any
from tests.mutation_testing.discovery.paths import _find_matching_close_bracket


class TextViewPrimitivesMixin:
    """Cursor, replace, insert and delete operations on the Ace editor."""

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
        """Replace [start_index, end_index) via Ace's session.replace(), as one undo entry.

        Typing instead would go through Ace's auto-closing brackets (typing "[]" over a selection leaves a stray "]"),
        so use this or _insert_at for every Text View edit that writes JSON text; _delete_span is fine for pure removals.
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
        """Insert text via Ace's session.insert(). Like _replace_range, this avoids typing, whose auto-closing would
        corrupt inserted brackets and quotes.
        """
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
