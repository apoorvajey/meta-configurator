"""Read-only helpers that capture state from the page (Text View, GUI facts, active mode) and click by test id."""
from __future__ import annotations

from playwright.sync_api import Page
from tests.shared.python.utils import SessionMode


def capture_text_view(page: Page, mode: SessionMode) -> dict:
    """Text View state of one mode: raw editor content, its parsed form (None if unparseable) and the format selector value.

    The format selector testid is not qualified by mode, so the format is only captured for the active mode
    (None for the other one; switching modes just to read it would change the state). Returns
    {"raw": None, "parsed": None, "format": None} if the editor is not mounted.
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
    """Per-path GUI facts for every rendered row (collapsed nodes have no row, so this is what the user sees).

    icon_shown and required_star_shown come from the validation-error-icon and required-star children of
    property-metadata-{path}; value comes from the input, textarea or select in the row. Tooltips are not
    collected because they need a hover (about 1.5 s per property). Uses one page.evaluate() call.
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


def js_click_for_testid(testid: str) -> str | None:
    """The js_click snippet for a testid that cannot be clicked via get_by_test_id(), else None.

    Applies to the TreeTable togglers (expand-/collapse-) and undo/redo (plain ids, not data-testid). Used by
    discovery and by replay, which rebuilds elements from bare testids.
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
    """'dataEditor' or 'schemaEditor' for the mode tab that is currently active.

    GUI View testids carry no mode, unlike Text View testids, so the loop needs this to attribute their actions.
    Reads the 'mode-active-true' tab label ('Data' or 'Schema').
    """
    try:
        label = page.get_by_test_id("mode-active-true").first.inner_text(timeout=1000).strip().lower()
    except Exception:
        label = ""
    return "schemaEditor" if "schema" in label else "dataEditor"
