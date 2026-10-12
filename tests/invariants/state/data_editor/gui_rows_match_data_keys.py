"""INV-D4."""
from __future__ import annotations
from tests.shared.python.utils_gui_editor import read_top_level_property_keys
from tests.invariants.invariants_core import InvariantResult

ID = "INV-D4"
NAME = "_inv_d4_gui_rows_exist_for_data_keys"

_ROW_WAIT_MS = 3000   # how long a data key may lack a GUI row before it counts as missing
_ROW_POLL_MS = 250
_BLANK_PREFIX = "GUI is blank"   # issue_report.py groups on this prefix (triage_config.json: issue_report.blank_gui_prefix)


def check(ctx) -> InvariantResult:
    """GUI rows must be complete and free of stale values relative to data. Row existence need not match data keys
    exactly, because the GUI View is a schema-driven form.

    1. Every data key has a visible GUI row.
    2. Every GUI row that shows a value (populated input, checked checkbox, populated array items) corresponds
       to a data key. Empty placeholder rows (pre-rendered for object-typed and required properties) are
       ignored. This catches a stale row that keeps its content after the data moved on, e.g. when undo
       removes a key.

    See checkPropertyExistence() in panelGuiEditor.spec.ts. Selector: property-data-{key}.
    """

    name = "INV-D4 GUI rows complete, no stale values"
    data = ctx.tp.get_data()

    if not isinstance(data, dict):
        return InvariantResult(name, True,
                               "Data is not an object - skip")

    data_keys = set(data.keys())
    # The GUI View renders after the Text View is parsed, so a just-added key may not have a row yet.
    # Wait for it before calling the row missing, and note the delay.
    waited_ms = 0
    while True:
        gui_keys = set(read_top_level_property_keys(ctx.page))
        missing_rows = sorted(data_keys - gui_keys)
        if not missing_rows or waited_ms >= _ROW_WAIT_MS:
            break
        ctx.page.wait_for_timeout(_ROW_POLL_MS)
        waited_ms += _ROW_POLL_MS
    orphan_candidates = gui_keys - data_keys
    orphan_rows = sorted(k for k in orphan_candidates if ctx._gui_row_has_value(k))
    passed = not missing_rows and not orphan_rows
    # Every data key lacks a row: the GUI shows none of the data at all.
    gui_blank = bool(data_keys) and set(missing_rows) == data_keys

    evidence = [f"{key}: row visible ✓" for key in sorted(data_keys - set(missing_rows))]
    evidence += [f"{key}: MISSING from GUI ✗" for key in missing_rows]
    evidence += [f"{key}: stale row - shows a value but no matching data key ✗" for key in orphan_rows]
    if waited_ms and not missing_rows:
        evidence.append(f"GUI rows took ~{waited_ms} ms to appear after the edit (render lag, not a missing row)")

    details = {}
    if missing_rows:
        details["missing_in_gui"] = missing_rows
    if orphan_rows:
        details["stale_gui_rows"] = orphan_rows

    message = "GUI rows complete, no stale values ✓"
    if not passed:
        parts = []
        if missing_rows:
            lead = f"{_BLANK_PREFIX} - no data key has a row: " if gui_blank else "missing rows for "
            parts.append(f"{lead}{missing_rows} (still missing after {waited_ms} ms)")
        if orphan_rows:
            parts.append(f"stale rows for {orphan_rows}")
        message = "; ".join(parts)

    return InvariantResult(name, passed, message, details, evidence=evidence)