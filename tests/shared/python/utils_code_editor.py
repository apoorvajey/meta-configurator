"""Python mirror of utilsCodeEditor.ts: reading and writing the Ace text editor panel."""

from __future__ import annotations
from typing import Any
from playwright.sync_api import Page

from tests.shared.python.utils import SessionMode, select_all
import yaml


class _StringTimestampLoader(yaml.SafeLoader):
    """SafeLoader that leaves ISO timestamps as strings, matching the app (YAML 1.2 core behaviour)."""


_StringTimestampLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

def get_code_editor(page: Page, mode: SessionMode | str = ""):
    """Locate the code editor element for a mode (id prefix code-editor-{mode}, e.g. code-editor-dataEditor)."""
    mode_val = mode.value if isinstance(mode, SessionMode) else mode
    return page.locator(f'[id^="code-editor-{mode_val}"]')


def read_code_editor_text(page: Page, mode: SessionMode | str = "") -> str:
    """Read the raw text content of the code editor."""
    return get_code_editor(page, mode).inner_text()


def check_code_editor_for_text(page: Page, text: str, mode: SessionMode | str = ""):
    """Assert that the code editor contains the given text."""
    editor = get_code_editor(page, mode)
    content = editor.inner_text()
    # Normalise whitespace for comparison (matching TS toContainText behaviour)
    assert text.replace(" ", "") in content.replace(" ", "").replace("\n", ""), \
        f"Code editor did not contain '{text}'.\nActual content: {content[:200]}"


def read_text_editor_raw(page: Page, mode: SessionMode | str = "") -> str | None:
    """The Text View's raw string with no parsing, for diagnostics when read_text_editor_content() fails to parse it
    under the selected format.
    """
    mode_val = mode.value if isinstance(mode, SessionMode) else mode
    try:
        return page.evaluate(f"""
            () => {{
                const editorEl = document.querySelector('[id^="code-editor-{mode_val}"]');
                if (!editorEl) return null;
                return window.ace?.edit(editorEl.id)?.getValue() ?? null;
            }}
        """)
    except Exception:
        return None


def read_text_editor_content(page: Page, mode: SessionMode | str = "") -> Any | None:
    """Read the text editor content parsed according to the selected format (JSON or YAML); None if parsing fails."""
    import json
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

    try:
        fmt = page.get_by_test_id("format-selector").inner_text().lower().strip()
    except Exception:
        fmt = "json"

    if "yaml" in fmt:
        try:
            return yaml.load(content, Loader=_StringTimestampLoader)
        except Exception:
            return None
    else:
        try:
            return json.loads(content)
        except Exception:
            return None

def force_code_editor_text(page: Page, text: str, mode: SessionMode | str = ""):
    """Overwrite the entire content of the code editor as the TS original does: click to focus, select all and
    delete, type character by character, click away to trigger the blur/change event.
    """
    editor = get_code_editor(page, mode)
    editor.click()
    select_all(page)
    editor.press("Backspace")

    for char in text:
        page.keyboard.press(char)

    editor.press("Enter")

    vp = page.viewport_size() or {"width": 1280, "height": 720}
    page.mouse.click(vp["width"] - 10, vp["height"] - 10)
    page.wait_for_timeout(800)