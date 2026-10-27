"""Template gui_icon_presence: an editor's GUI shows a validation-error icon exactly when its store is invalid."""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation

# view -> (GUI rows field, verdict field). The Schema Editor's verdict is the edited schema against the meta-schema only
# (snapshot['schema_valid_meta_only']), because the GUI icons reflect nothing else. The engine mines every view listed here.
VIEWS = {"schema": ("se_gui", "schema_valid_meta_only")}
MINED_VIEWS = ("schema",)


def rule_gui_icon_presence(before: dict, after: dict, view: str) -> list[Observation]:
    """One observation per step, when the GUI rows and the verdict (a bool) are recorded. Coarse on purpose: any icon or none."""
    gui_field, verdict_field = VIEWS[view]
    gui, valid = after.get(gui_field), after.get(verdict_field)
    if not isinstance(gui, dict) or not isinstance(valid, bool):
        return []
    icons = sum(1 for facts in gui.values() if isinstance(facts, dict) and facts.get("icon_shown") is True)
    held = (icons > 0) == (not valid)
    return [(held, {} if held else {"icons_shown": icons, "store_valid": valid})]


NAME = "gui_icon_presence"
RULE = rule_gui_icon_presence
