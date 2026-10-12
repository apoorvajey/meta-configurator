"""Template gui_control_by_type: a property declared with a given schema type is shown with a given kind of GUI control.

A candidate is a pair (declared, control), for example ("boolean", "input:checkbox"). The engine takes the pairs that occur in the
traces (observed_rows over every snapshot), so the same candidates are tested in every dataset. One observation per GUI row whose
declared type is `declared`; it holds when the row's control is `control`.

declared: the row's schema type through $ref, allOf, anyOf, oneOf, then/else; 'enum' when the schema lists an enum; rows whose
          schema gives no single type, or runs into a $ref that cannot be followed, are skipped.
control:  snapshot['de_gui'][row]['control'] ('input:text', 'input:number', 'input:checkbox', 'textarea', 'select', ...), read
          from the input, textarea or select directly inside the row. A row without such an element has no control and is skipped.
"""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation, path_tokens, walk_schema
from tests.mutation_testing.inference.facets import SETTINGS_ROW_PREFIXES
from tests.mutation_testing.inference.memo import memoized

_LAST: tuple[dict, list[tuple[str, str, str]]] | None = None  # (snapshot, its rows): the snapshot is kept so its id cannot be reused


def declared_type(schema, tokens: list[str]) -> str | None:
    """'enum' or the single schema type declared for the path, else None."""
    declared, expanded, opaque = walk_schema(schema, tokens)
    if opaque or not declared:
        return None
    types: set[str] = set()
    for node, _conditional in expanded:
        if isinstance(node.get("enum"), list):
            return "enum"
        if isinstance(node.get("type"), str):
            types.add(node["type"])
    return next(iter(types)) if len(types) == 1 else None


def observed_rows(snapshot: dict) -> list[tuple[str, str, str]]:
    """(row, declared type, control) for every GUI row that has both. Every candidate reads the same rows, so they are kept
    for the whole inference run (inference/memo.py) and, one step at a time, for the last snapshot."""
    return memoized(("control_rows", id(snapshot)), lambda: _last_snapshot_rows(snapshot))


def _last_snapshot_rows(snapshot: dict) -> list[tuple[str, str, str]]:
    global _LAST
    if _LAST is not None and _LAST[0] is snapshot:
        return _LAST[1]
    gui, schema = snapshot.get("de_gui"), snapshot.get("de_schema")
    rows: list[tuple[str, str, str]] = []
    if isinstance(gui, dict) and isinstance(schema, dict):
        for key, facts in gui.items():
            control = facts.get("control") if isinstance(facts, dict) else None
            if not key or key.startswith(SETTINGS_ROW_PREFIXES) or not isinstance(control, str):
                continue
            tokens = path_tokens(key)
            declared = declared_type(schema, tokens) if tokens else None
            if declared:
                rows.append((key, declared, control))
    _LAST = (snapshot, rows)
    return rows


def rule_gui_control_by_type(before: dict, after: dict, declared: str, control: str) -> list[Observation]:
    return [(c == control, {} if c == control else {"row": key, "control": c})
            for key, d, c in observed_rows(after) if d == declared]


NAME = "gui_control_by_type"
RULE = rule_gui_control_by_type
