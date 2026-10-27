"""Set-up of the app page for a run: dataset choice, page preparation and the state snapshot of one step."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import Page
from tests.invariants.app_errors import (
    attach_app_error_log,
    attach_console_log,
    attach_rename_conflict_log,
    events_since_last_snapshot,
    is_app_schema_rejection,
    take_midstep,
    take_rename_conflicts,
)
from tests.invariants.schema_validity import schema_meta_violations
from tests.shared.python.utils import (
    REMOTE_FIXTURES_REF,
    SessionMode,
    dismiss_dialog_if_present,
    setup_test_panel_via_localstorage,
)
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.discovery.page_capture import (
    _get_current_active_mode,
    capture_app_build,
    capture_gui_facts,
    capture_spinner_present,
    capture_text_view,
)


from tests.mutation_testing.common.datasets import DATA_DIR, SCHEMA_DATA_PAIRS  # noqa: F401  (re-exported)


def _dataset_slug(schema_option_name: str, data_file: str) -> str:
    """Stable identifier of a schema/data pair, stored in every snapshot so inference works per dataset."""
    def _norm(s: str) -> str:
        return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')
    return f"{_norm(schema_option_name)}__{_norm(data_file)}"


def _safe_filename_part(s: str) -> str:
    """Replace characters that are unsafe in file names (used for all screenshot names)."""
    return re.sub(r'[<>:"/\\|?*]', '_', s)[:60]


def _json_safe(value):
    """`value` if json.dumps accepts it (types are kept: 5, True, "", None), else its repr().

    Used for "input_value", the exact value passed to execute(), which replay feeds back unchanged.
    """
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


VIEWPORT = {"width": 1920, "height": 1080}


PAGE_ZOOM = "0.75"


def setup_app_for_schema(page: Page, schema_option_name: str, data_file_name: str) -> None:
    """Load an example schema by its dropdown name, then upload the data file (a name in DATA_DIR) in the Data Editor Text View."""
    page.get_by_role("button", name="Example Schemas").click()
    page.wait_for_timeout(1000)
    page.get_by_role("option", name=schema_option_name).click()
    page.wait_for_timeout(1000)
    print(f"✓ {schema_option_name} loaded")

    page.get_by_text("Data", exact=True).click()
    page.wait_for_timeout(1000)
    page.get_by_text("Text View", exact=True).click()
    page.wait_for_timeout(500)

    with page.expect_file_chooser() as fc_info:
        page.locator("[data-icon='folder-open']").click()
        try:   # newer builds show a menu first; older ones open the file picker straight away
            page.get_by_text("Open JSON/YAML Data").click(timeout=1500)
        except Exception:
            pass
    fc_info.value.set_files(str(DATA_DIR / data_file_name))
    page.wait_for_timeout(1000)
    print("✓ Data file loaded")


def prepare_app_page(page: Page, schema_option_name: str, data_file_name: str) -> None:
    """Test panel and reload, zoom, dismiss dialog, load schema and data. The caller sets the viewport first."""
    attach_console_log(page)  # before anything runs, so the first snapshot knows what the console said
    attach_rename_conflict_log(page)  # TEMP-RENAME-05: the executor dismisses the dialog, so it is recorded in the page
    attach_app_error_log(page)  # INV-G1's own list of uncaught exceptions; without this call INV-G1 can never fail
    setup_test_panel_via_localstorage(page)
    page.wait_for_timeout(2000)
    page.evaluate(f"document.body.style.zoom = '{PAGE_ZOOM}'")
    print("✓ Page loaded")

    dismiss_dialog_if_present(page)
    setup_app_for_schema(page, schema_option_name, data_file_name)


def scroll_to_validation_error(page: Page) -> None:
    """Scroll the first validation-error icon into view before a failing step's screenshot.

    This changes page state, so replay repeats it (see ReplayRunner's mirror_screenshot_scroll).
    """
    try:
        page.evaluate("""
            () => {
                const icon = document.querySelector('[data-testid="validation-error-icon"]');
                if (icon) icon.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        """)
        page.wait_for_timeout(400)
    except Exception:
        pass


def _short(value, limit: int = 80) -> str:
    """Compact one-line JSON form of a value for logging what a step changed."""
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        s = repr(value)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _full(value, cap: int = 20000) -> str:
    """Complete one-line JSON form of a value, for the recorded step history and the HTML log ("Document changes").

    Unlike _short (console lines only) this does not clip at 80 characters, so the log shows the exact text.
    `cap` only guards the history files against a huge subtree; when it is hit, the cut is marked with the full length.
    """
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        s = repr(value)
    return s if len(s) <= cap else s[:cap] + f"… [{len(s)} characters in total, cut here]"


_DOCUMENTS = (("data", SessionMode.DataEditor), ("schema", SessionMode.SchemaEditor))


def read_documents(page: Page) -> dict:
    """Both edited documents, whichever editor is active: {"data": the data (Data Editor's), "schema": the schema (Schema
    Editor's)}. A document that cannot be read is left out, so "not read" is never mistaken for "empty"."""
    out = {}
    for name, mode in _DOCUMENTS:
        try:
            out[name] = TestPanel(page, mode).get_data()
        except Exception:
            pass
    return out


def document_diffs(before: dict | None, after: dict | None, primary: str = "data"):
    """[(document, path, before_value, after_value)] for every difference in the data and the schema between two
    read_documents() results; the document the action was applied to (`primary`) comes first.

    [] if nothing changed; None if no document could be compared (nothing read before or after).
    """
    from tests.invariants.invariants_core import _diff_nested   # local: avoids an import cycle at module load
    if not before or not after:
        return None
    out, compared = [], False
    for name in [primary] + [n for n, _ in _DOCUMENTS if n != primary]:
        if name in before and name in after:
            compared = True
            out.extend((name, p, a, b) for p, a, b in _diff_nested(before[name], after[name]))
    return out if compared else None


def effect_records(diffs, limit: int = 3) -> list[dict]:
    """The recorded form of document_diffs(): {"document", "path", "before", "after"} with the exact values, the first
    `limit` changes of EACH document (so many data changes cannot hide a schema change)."""
    seen: dict[str, int] = {}
    out = []
    for name, p, a, b in diffs or []:
        seen[name] = seen.get(name, 0) + 1
        if seen[name] <= limit:
            out.append({"document": name, "path": p, "before": _full(a), "after": _full(b)})
    return out


def effect_console_lines(diffs, limit: int = 3, label: str = "changed") -> list[str]:
    """Console lines for document_diffs(): format_effect_lines per document that changed, tagged [data] / [schema]."""
    if not diffs:
        return [f"   {label}: nothing (data and schema identical before and after)"]
    lines = []
    for name, _ in _DOCUMENTS:
        mine = [(p, a, b) for d, p, a, b in diffs if d == name]
        if mine:
            lines += format_effect_lines(mine, limit=limit, label=f"{label} [{name}]")
    return lines


def _read_or_none(read):
    """read(), or None if it raises: a field that cannot be read must not stop the run (None means "not determined")."""
    try:
        return read()
    except Exception:
        return None


def format_effect_lines(diffs, limit: int = 3, label: str = "changed") -> list[str]:
    """Log lines for what a step changed in the edited document: one "path: before -> after" line per change
    (first `limit`), a count of the rest, or a line saying nothing changed. `diffs` is _diff_nested(before, after).
    `label` introduces each line ("changed"; "undo reverted" / "redo re-applied" for an undo or redo, where "before" is
    what was taken away and "after" what came back)."""
    if not diffs:
        return [f"   {label}: nothing (document identical before and after)"]
    lines = [f"   {label}: {p}: {_short(a, 50)} → {_short(b, 50)}" for p, a, b in diffs[:limit]]
    if len(diffs) > limit:
        lines.append(f"   … and {len(diffs) - limit} more change(s)")
    return lines


def undo_redo_label(testid: str) -> str:
    """The `label` for format_effect_lines of a step or repair click: what an undo or redo did, else "changed"."""
    if testid.endswith("undo"):
        return "undo reverted"
    if testid.endswith("redo"):
        return "redo re-applied"
    return "changed"


def build_snapshot(page: Page, action: str, step: int,
                   run_id: str | None, dataset: str | None) -> dict:
    """The snapshot of the app state after a step; used by the loop and by replay so both record the same fields.

    Fields for inference (a value of None means "not determined", never "no" / "none"; the templates skip such a step):
      active_mode                     'dataEditor' or 'schemaEditor'
      schema_valid_meta               the edited schema (de_schema) against the meta-schema, incl. schemas the app's Ajv
                                      refuses to compile (schema_validity.py); the loop's oracle
      schema_violation_count          number of violations behind schema_valid_meta
      schema_valid_meta_only          the edited schema against the meta-schema only, without the app's compile checks (the Schema
                                      GUI icons reflect nothing else; INV-S4's oracle); None for a schema that is not an object
      app_logged_invalid              the app itself wrote "schema is invalid" or "can't resolve reference" to the console during this step; the app's oracle,
                                      kept apart from schema_valid_meta so a disagreement stays visible. For a two-stage action
                                      (add-item-required) only what the app wrote after the second stage, i.e. about the final schema
      app_logged_invalid_midstep      the same for what the app wrote about the intermediate state of a two-stage action; None for other steps
      schema_valid_meta_midstep       the meta-schema verdict on that intermediate state; None for other steps
      console_errors_since_last_step  console errors and uncaught exceptions since the previous snapshot
      first_error_message             the first of them (first line, cut)
      spinner_present                 the GUI shows a loading spinner when the snapshot is taken
      app_build                       name of the app's main script bundle
      fixtures_ref                    the git ref the fixtures were fetched from
    Fields for checking that the views stay in sync (same None rule):
      de_current_path / se_current_path               the navigation path of each editor (a list; [] at the root)
      de_selected_element / se_selected_element       the selected element of each editor (a list; [] at the root)
      rename_conflict_dialogs         texts of the "Rename conflict" dialogs the app opened during this step (list, [] = none; None = not
                                      recorded); TEMP-RENAME-05 checks them
      console_warnings_since_last_step                console warnings since the previous snapshot
      failed_requests_since_last_step / first_failed_request   network requests that failed (count, first one)
      captured_at                                     UTC time the snapshot was taken (ISO 8601, milliseconds)
      page_url                                        the page URL
      de_text / se_text also carry annotation_errors, annotation_warnings, first_annotation, cursor, undo_depth,
      redo_depth and line_count (Ace; see page_capture._capture_ace_extras), and every GUI row also carries control and
      disabled (see page_capture.capture_gui_facts).
    The console record is cut per snapshot, and the app reloads the schema from a debounced callback, so a message can
    land on the snapshot after the edit that caused it (see INV-G1).
    """
    _tp_de = TestPanel(page, SessionMode.DataEditor)
    _tp_se = TestPanel(page, SessionMode.SchemaEditor)
    de_data = _tp_de.get_data()
    de_schema = _tp_de.get_schema()
    se_schema = _tp_se.get_schema()
    se_data = _tp_se.get_data()
    mode = _get_current_active_mode(page)
    events = events_since_last_snapshot(page)
    console_errors = None if events is None else events["error"]
    midstep = take_midstep(page)
    if console_errors is not None and midstep is not None:
        midstep_errors = console_errors[:midstep["error_count"]]
        final_errors = console_errors[midstep["error_count"]:]
    else:
        midstep_errors, final_errors = None, console_errors
    violations = schema_meta_violations(page, de_schema)
    meta_only = (schema_meta_violations(page, de_schema, include_app_compile_problems=False)
                 if isinstance(de_schema, dict) else None)
    return {
        "de_data": de_data,
        "de_schema": de_schema,
        "se_schema": se_schema,
        "se_data": se_data,
        "de_gui": capture_gui_facts(page) if mode == "dataEditor" else None,
        "se_gui": capture_gui_facts(page) if mode == "schemaEditor" else None,
        "de_text": capture_text_view(page, SessionMode.DataEditor),
        "se_text": capture_text_view(page, SessionMode.SchemaEditor),
        "active_mode": mode,
        "schema_valid_meta": None if violations is None else not violations,
        "schema_violation_count": None if violations is None else len(violations),
        "schema_valid_meta_only": None if meta_only is None else not meta_only,
        "app_logged_invalid": (None if final_errors is None
                               else any(is_app_schema_rejection(m) for m in final_errors)),
        "app_logged_invalid_midstep": (None if midstep_errors is None
                                       else any(is_app_schema_rejection(m) for m in midstep_errors)),
        "schema_valid_meta_midstep": None if midstep is None else midstep["schema_valid_meta"],
        "rename_conflict_dialogs": take_rename_conflicts(page),
        "console_errors_since_last_step": None if console_errors is None else len(console_errors),
        "first_error_message": console_errors[0] if console_errors else None,
        "de_current_path": _read_or_none(lambda: _tp_de.get_current_path(expect_non_root=False)),
        "de_selected_element": _read_or_none(lambda: _tp_de.get_current_selected_element(expect_non_root=False)),
        "se_current_path": _read_or_none(lambda: _tp_se.get_current_path(expect_non_root=False)),
        "se_selected_element": _read_or_none(lambda: _tp_se.get_current_selected_element(expect_non_root=False)),
        "console_warnings_since_last_step": None if events is None else len(events["warning"]),
        "failed_requests_since_last_step": None if events is None else len(events["failed_request"]),
        "first_failed_request": events["failed_request"][0] if events and events["failed_request"] else None,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "page_url": page.url,
        "spinner_present": capture_spinner_present(page),
        "app_build": capture_app_build(page),
        "fixtures_ref": REMOTE_FIXTURES_REF,
        "run_id": run_id,
        "dataset": dataset,
        "action": action,
        "step": step,
    }