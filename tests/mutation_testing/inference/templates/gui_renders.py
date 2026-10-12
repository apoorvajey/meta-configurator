"""Template gui_renders: with data in the Data Editor, the GUI View shows rows for it and no loading spinner."""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation, path_tokens
from tests.mutation_testing.inference.facets import SETTINGS_ROW_PREFIXES


def _renders(snapshot: dict) -> tuple[bool, dict] | None:
    """(rendered, detail), or None if there is nothing to judge: the Data Editor's GUI is not showing, the data is empty
    or not an object, or the spinner was not read (a trace from before the field existed).

    Blank means no data key has a row, the same reading as INV-D4's "GUI is blank"; one row for one key is not blank.
    """
    gui, data, spinner = snapshot.get("de_gui"), snapshot.get("de_data"), snapshot.get("spinner_present")
    if not isinstance(gui, dict) or not isinstance(data, dict) or not data or not isinstance(spinner, bool):
        return None
    rows = {t[0] for t in (path_tokens(k) for k in gui if k and not k.startswith(SETTINGS_ROW_PREFIXES)) if t}
    without_row = [k for k in data if str(k) not in rows]
    blank = len(without_row) == len(data)
    if not blank and not spinner:
        return True, {}
    return False, {"blank_gui": blank, "spinner": spinner, "data_keys_without_row": len(without_row)}


def rule_gui_renders(before: dict, after: dict, transition: bool = False) -> list[Observation]:
    """One observation per step. transition=True also requires the GUI to have rendered before the step, which blames
    the action that broke it instead of every later step."""
    now = _renders(after)
    if now is None:
        return []
    if transition:
        prev = _renders(before)
        if prev is None or not prev[0]:
            return []
    return [now]


NAME = "gui_renders"
RULE = rule_gui_renders
