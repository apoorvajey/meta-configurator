"""Template gui_store_value: the value shown in a GUI input of an editor equals the value in that editor's store."""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation, _MISSING, path_tokens, value_at
from tests.mutation_testing.inference.facets import _safe_dict
from tests.mutation_testing.inference.templates.gui_value_agreement import _same

# view -> (GUI rows field, store field). The Data Editor is covered by gui_value_agreement, so the engine mines the Schema Editor only.
VIEWS = {"data": ("de_gui", "de_data"), "schema": ("se_gui", "se_data")}
MINED_VIEWS = ("schema",)


def rule_gui_store_value(before: dict, after: dict, view: str) -> list[Observation]:
    """One observation per GUI row (any depth) that has an input and whose store value is a primitive.
    Rows without a store path (settings rows) or with an object / array value are skipped."""
    gui_field, store_field = VIEWS[view]
    obs: list[Observation] = []
    for key, facts in _safe_dict(after.get(gui_field)).items():
        tokens = path_tokens(key)
        if not tokens or not isinstance(facts, dict) or "value" not in facts:
            continue
        value = value_at(after.get(store_field), tokens)
        if value is _MISSING or not isinstance(value, (str, int, float, bool)):
            continue
        agree = _same(facts["value"], value)
        obs.append((agree, {} if agree else {"key": key, "gui_value": str(facts["value"])[:60], "store_value": str(value)[:60]}))
    return obs


NAME = "gui_store_value"
RULE = rule_gui_store_value
