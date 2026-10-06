"""INV-D4. Loaded by invariants_core.load_invariant_plugins()."""
from __future__ import annotations
from tests.shared.python.utils_gui_editor import read_top_level_property_keys
from tests.invariants.invariants_core import InvariantResult

ID = "INV-D4"
NAME = "_inv_d4_gui_rows_exist_for_data_keys"


def check(ctx) -> InvariantResult:
    """GUI rows must be complete and free of stale values relative to data. Row existence need not match
    data keys exactly, because GUI View is a schema-driven form.

    1. Every data key must have a visible GUI row.
    2. Every GUI row that shows a value (populated input, checked checkbox, populated array items) must
       correspond to a data key. A row with no value is fine: MetaConfigurator pre-renders placeholders
       for object-typed and required properties.

    Check 2 ignores empty placeholder rows. It catches a stale row that keeps its content after the data
    moved on, e.g. when undo removes a key from data.

    See checkPropertyExistence() in panelGuiEditor.spec.ts. Selector: property-data-{key}.
    """

    name = "INV-D4 GUI rows complete, no stale values"
    data = ctx.tp.get_data()

    if not isinstance(data, dict):
        return InvariantResult(name, True,
                               "Data is not an object - skip")

    data_keys = set(data.keys())
    gui_keys = set(read_top_level_property_keys(ctx.page))

    missing_rows = sorted(data_keys - gui_keys)
    orphan_candidates = gui_keys - data_keys
    orphan_rows = sorted(k for k in orphan_candidates if ctx._gui_row_has_value(k))
    passed = not missing_rows and not orphan_rows

    evidence = [f"{key}: row visible ✓" for key in sorted(data_keys - set(missing_rows))]
    evidence += [f"{key}: MISSING from GUI ✗" for key in missing_rows]
    evidence += [f"{key}: stale row - shows a value but no matching data key ✗" for key in orphan_rows]

    details = {}
    if missing_rows:
        details["missing_in_gui"] = missing_rows
    if orphan_rows:
        details["stale_gui_rows"] = orphan_rows

    message = "GUI rows complete, no stale values ✓"
    if not passed:
        parts = []
        if missing_rows:
            parts.append(f"missing rows for {missing_rows}")
        if orphan_rows:
            parts.append(f"stale rows for {orphan_rows}")
        message = "; ".join(parts)

    return InvariantResult(name, passed, message, details, evidence=evidence)
