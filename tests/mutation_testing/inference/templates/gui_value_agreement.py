"""Template gui_value_agreement: the value shown in a property's GUI input equals that property's value in the data."""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation, _MISSING, path_tokens, value_at
from tests.mutation_testing.inference.facets import _safe_dict


def _same(gui_value, data_value) -> bool:
    if isinstance(data_value, bool):
        return gui_value == data_value
    if isinstance(data_value, (int, float)):
        try:
            return float(gui_value) == float(data_value)
        except (TypeError, ValueError):
            return False
    return str(gui_value) == data_value


def _reading(snapshot: dict, key: str):
    """(gui_value, data_value) for a row whose data value is a primitive and which has a GUI input, else None."""
    facts = _safe_dict(_safe_dict(snapshot.get("de_gui")).get(key))
    tokens = path_tokens(key)
    if "value" not in facts or not tokens:
        return None
    value = value_at(snapshot.get("de_data"), tokens)
    if value is _MISSING or not isinstance(value, (str, int, float, bool)):
        return None  # no such data, or an object/array, which has no single input
    return facts["value"], value


def rule_gui_value_agreement(before: dict, after: dict, transition: bool = False) -> list[Observation]:
    """Every GUI row at any depth ('address.city', 'nickNames[0]'). transition=True also requires GUI and data to have agreed before the step."""
    obs: list[Observation] = []
    for key in _safe_dict(after.get("de_gui")):
        now = _reading(after, key)
        if now is None:
            continue
        if transition:
            prev = _reading(before, key)
            if prev is None or not _same(*prev):
                continue
        agree = _same(*now)
        obs.append((agree, {} if agree else {"key": key, "gui_value": str(now[0])[:60], "data_value": str(now[1])[:60]}))
    return obs


NAME = "gui_value_agreement"
RULE = rule_gui_value_agreement