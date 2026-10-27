"""Template gui_rows_complete: every top-level key of an editor's store has a row in that editor's GUI View."""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation

# view -> (GUI rows field, store field). The Schema Editor's store is se_data (the schema being edited).
VIEWS = {"data": ("de_gui", "de_data"), "schema": ("se_gui", "se_data")}
# The views the engine mines.
MINED_VIEWS = ("data", "schema")


def _row_keys(store: dict) -> list[str]:
    """Top-level keys that can be matched to a GUI row key. A key with '.', '[' or ']' cannot (those separate path steps in a row key)."""
    return [k for k in store if isinstance(k, str) and k and not any(c in k for c in ".[]")]


def rule_gui_rows_complete(before: dict, after: dict, view: str) -> list[Observation]:
    """One observation per step, when that editor's GUI rows and its store (an object with a matchable key) are recorded."""
    gui_field, store_field = VIEWS[view]
    gui, store = after.get(gui_field), after.get(store_field)
    if not isinstance(gui, dict) or not isinstance(store, dict):
        return []
    keys = _row_keys(store)
    if not keys:
        return []
    missing = sorted(k for k in keys if k not in gui)
    return [(not missing, {} if not missing else {"keys_without_row": len(missing), "keys": ", ".join(missing[:5])})]


NAME = "gui_rows_complete"
RULE = rule_gui_rows_complete
