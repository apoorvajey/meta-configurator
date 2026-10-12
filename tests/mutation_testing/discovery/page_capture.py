"""Read-only helpers that capture state from the page (Text View, GUI facts, active mode) and click by test id."""
from __future__ import annotations

from playwright.sync_api import Page
from tests.shared.python.utils import SessionMode


def capture_text_view(page: Page, mode: SessionMode) -> dict:
    """Text View state of one mode: {"raw", "parsed" (None if unparseable), "format"}, all None if the editor is not mounted.

    The format selector testid is not qualified by mode, so the format is captured for the active mode only
    (None for the other one; switching modes just to read it would change the state).
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

    return {"raw": raw, "parsed": parsed, "format": fmt, **_capture_ace_extras(page, mode_str)}


def _capture_ace_extras(page: Page, mode_str: str) -> dict:
    """What the Ace editor of a Text View knows besides its text; {} if the editor or window.ace cannot be reached.

    annotation_errors / annotation_warnings / first_annotation   the markers Ace shows in the gutter
    cursor                                                       [row, column]
    undo_depth / redo_depth                                      length of Ace's own undo / redo stack (None if the stack is
                                                                 not found). Not verified: whether the app's undo button
                                                                 uses this history or a separate one
    line_count                                                   number of lines in the editor
    """
    try:
        extras = page.evaluate("""
            (prefix) => {
                const el = document.querySelector('[id^="code-editor-' + prefix + '"]');
                const editor = el && window.ace ? window.ace.edit(el.id) : null;
                if (!editor) return null;
                const session = editor.getSession();
                const notes = session.getAnnotations() || [];
                const count = t => notes.filter(n => n.type === t).length;
                const first = notes.find(n => n.type === 'error') || notes.find(n => n.type === 'warning');
                const um = session.getUndoManager();
                const undo = um ? (um.$undoStack || um.undostack) : null;
                const redo = um ? (um.$redoStack || um.redostack) : null;
                const pos = editor.getCursorPosition();
                return {
                    annotation_errors: count('error'),
                    annotation_warnings: count('warning'),
                    first_annotation: first ? String(first.text).slice(0, 200) : null,
                    cursor: [pos.row, pos.column],
                    undo_depth: undo ? undo.length : null,
                    redo_depth: redo ? redo.length : null,
                    line_count: session.getLength(),
                };
            }
        """, mode_str)
    except Exception:
        return {}
    return extras or {}


def capture_gui_facts(page: Page) -> dict:
    """Per-path GUI facts for every rendered row (collapsed nodes have no row, so this is what the user sees).

    icon_shown and required_star_shown come from the validation-error-icon and required-star children of
    property-metadata-{path}; value, control ('input:text', 'input:number', 'input:checkbox', 'textarea', 'select', ...)
    and disabled (disabled or read-only) come from the input, textarea or select in the row. Tooltips are not
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
                    const tag = input.tagName.toLowerCase();
                    result[path].control = tag === 'input' ? 'input:' + input.type : tag;
                    result[path].disabled = !!(input.disabled || input.readOnly);
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
    """'dataEditor' or 'schemaEditor' for the active mode tab, from the 'mode-active-true' tab label. GUI View
    testids carry no mode, unlike Text View testids, so the loop needs this to attribute their actions.
    """
    try:
        label = page.get_by_test_id("mode-active-true").first.inner_text(timeout=1000).strip().lower()
    except Exception:
        label = ""
    return "schemaEditor" if "schema" in label else "dataEditor"


# The PrimeVue TreeTable spinner; INV-G2 looks for the same element.
LOADING_SPINNER_SELECTOR = ".p-treetable-loading-icon"


def capture_spinner_present(page: Page) -> bool | None:
    """True if the GUI shows a loading spinner right now, False if not, None if the page could not be asked.
    No waiting: INV-G2 has already waited for the spinner to settle before the snapshot is built.
    """
    try:
        return page.locator(LOADING_SPINNER_SELECTOR).count() > 0
    except Exception:
        return None


def capture_app_build(page: Page) -> str | None:
    """File name(s) of the app's main script bundle (for example 'index-981d8f4a.js'), None if there is none.

    The hosted site changes under the harness; this tells a change of the site from a change of the harness.
    """
    try:
        names = page.evaluate("""
            () => [...document.scripts].map(s => s.src).filter(Boolean)
                .map(u => u.split('/').pop().split('?')[0])
                .filter(n => /^index-.+\\.js$/.test(n))
        """)
    except Exception:
        return None
    return ",".join(sorted(set(names))) or None