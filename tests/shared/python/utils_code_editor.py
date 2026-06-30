"""
utils_code_editor.py
====================
Mirrors: tests/shared/utilsCodeEditor.ts

Helpers for reading and writing the text/code editor panel (Ace editor).
"""

from __future__ import annotations
import json
from typing import Any
from playwright.sync_api import Page

from tests.shared.python.utils import SessionMode, select_all


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def get_code_editor(page: Page, mode: SessionMode | str = ""):
    """
    Locate the code editor element for a given mode.
    Mirrors: getCodeEditor() in utilsCodeEditor.ts

    The id prefix pattern is: code-editor-{mode}
    e.g. code-editor-dataEditor, code-editor-schemaEditor
    """
    mode_val = mode.value if isinstance(mode, SessionMode) else mode
    return page.locator(f'[id^="code-editor-{mode_val}"]')


def read_code_editor_text(page: Page, mode: SessionMode | str = "") -> str:
    """
    Read the raw text content of the code editor.
    Mirrors: readCodeEditorText() in utilsCodeEditor.ts
    """
    return get_code_editor(page, mode).inner_text()


def check_code_editor_for_text(page: Page, text: str, mode: SessionMode | str = ""):
    """
    Assert that the code editor contains the given text.
    Mirrors: checkCodeEditorForText() in utilsCodeEditor.ts
    """
    editor = get_code_editor(page, mode)
    content = editor.inner_text()
    # Normalise whitespace for comparison (matching TS toContainText behaviour)
    assert text.replace(" ", "") in content.replace(" ", "").replace("\n", ""), \
        f"Code editor did not contain '{text}'.\nActual content: {content[:200]}"


def read_text_editor_content(page: Page, mode: SessionMode | str = "") -> Any | None:
    """
    Read the text editor content and parse it according to the
    currently selected format (JSON, YAML, or XML).
    Returns a Python object, or None if parsing fails.
    """
    import yaml

    # Use JavaScript to read Ace editor value directly
    # inner_text() only returns line numbers, not content
    mode_val = mode.value if isinstance(mode, SessionMode) else mode
    try:
        content = page.evaluate(f"""
            () => {{
                const editorEl = document.querySelector('[id^="code-editor-{mode_val}"]');
                if (!editorEl) return null;
                return window.ace?.edit(editorEl.id)?.getValue() ?? null;
            }}
        """)
        if content is None:
            return None
    except Exception:
        return None

    # Detect current format
    try:
        fmt = page.get_by_test_id("format-selector").inner_text().lower().strip()
    except Exception:
        fmt = "json"

    # Parse according to format
    if "yaml" in fmt:
        try:
            return yaml.safe_load(content)
        except Exception:
            return None
    elif "xml" in fmt:
        return None
    else:
        try:
            return json.loads(content)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def force_code_editor_text(page: Page, text: str, mode: SessionMode | str = ""):
    """
    Overwrite the entire content of the code editor.
    Mirrors: forceCodeEditorText() in utilsCodeEditor.ts

    Uses the same approach as the TS original:
    1. Click to focus
    2. Select all + delete
    3. Type character by character
    4. Click away to trigger the change/blur event
    """
    editor = get_code_editor(page, mode)
    editor.click()
    select_all(page)
    editor.press("Backspace")

    for char in text:
        page.keyboard.press(char)

    editor.press("Enter")

    # Click away from the editor to trigger the blur/change event
    vp = page.viewport_size() or {"width": 1280, "height": 720}
    page.mouse.click(vp["width"] - 10, vp["height"] - 10)
    page.wait_for_timeout(800)
