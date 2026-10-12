"""Template gui_required_agreement: a property's GUI required-star is shown exactly when the schema requires it."""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation, path_tokens, required_names, walk_schema
from tests.mutation_testing.inference.facets import _safe_dict


def _reading(snapshot: dict, key: str):
    """(star_shown, always_required, conditionally_required) for one row, or None if the row cannot be judged.
    The parent's required list is read through $ref, allOf, anyOf, oneOf, then/else and dependentRequired;
    names required only under a condition count as 'conditional'. Array elements have no star and are skipped.
    """
    facts = _safe_dict(_safe_dict(snapshot.get("de_gui")).get(key))
    tokens = path_tokens(key)
    schema = snapshot.get("de_schema")
    if "required_star_shown" not in facts or not tokens or tokens[-1].isdigit() or not isinstance(schema, dict):
        return None
    _, parent, opaque = walk_schema(schema, tokens[:-1])
    declared = any(isinstance(n.get("properties"), dict) and tokens[-1] in n["properties"] for n, _ in parent)
    if opaque or not declared:
        return None
    always, conditional = required_names(parent)
    return bool(facts["required_star_shown"]), tokens[-1] in always, tokens[-1] in conditional


def _agrees(reading) -> bool:
    shown, always, conditional = reading
    return (always or conditional) if shown else not always


def rule_gui_required_agreement(before: dict, after: dict, transition: bool = False) -> list[Observation]:
    """Every property row at any depth. A shown star needs the name to be required (always or under a condition);
    a hidden star needs it not to be always required. transition=True also requires agreement before the step."""
    obs: list[Observation] = []
    for key in _safe_dict(after.get("de_gui")):
        now = _reading(after, key)
        if now is None:
            continue
        if transition:
            prev = _reading(before, key)
            if prev is None or not _agrees(prev):
                continue
        agree = _agrees(now)
        obs.append((agree, {} if agree else
                    {"key": key, "star_shown": now[0], "always_required": now[1], "conditionally_required": now[2]}))
    return obs


NAME = "gui_required_agreement"
RULE = rule_gui_required_agreement