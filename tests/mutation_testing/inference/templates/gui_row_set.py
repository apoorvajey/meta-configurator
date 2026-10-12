"""Template gui_row_set: every row of the Data Editor's GUI belongs to the data or to the schema (no stale rows)."""
from __future__ import annotations

from tests.mutation_testing.inference.common import (
    Observation, _MISSING, path_tokens, required_names, schema_has_path, value_at, walk_schema,
)
from tests.mutation_testing.inference.facets import SETTINGS_ROW_PREFIXES, _safe_dict


def _is_required_name(schema: dict, tokens: list[str]) -> bool:
    """True if the last path step is listed in `required` (or dependentRequired) of the schema(s) that apply to its parent."""
    _, parents, _ = walk_schema(schema, tokens[:-1])
    always, conditional = required_names(parents)
    return tokens[-1] in always or tokens[-1] in conditional


def _stale_rows(snapshot: dict) -> list[str] | None:
    """Rows that are neither a path in the data nor a path the schema declares, or None if the snapshot has no GUI rows to judge."""
    gui = snapshot.get("de_gui")
    schema = snapshot.get("de_schema")
    if not isinstance(gui, dict) or not isinstance(schema, dict):
        return None
    rows = [k for k in gui if k and not k.startswith(SETTINGS_ROW_PREFIXES)]
    if not rows:
        return None
    data = snapshot.get("de_data")
    stale = []
    for key in rows:
        tokens = path_tokens(key)
        if (tokens and value_at(data, tokens) is _MISSING
                and not schema_has_path(schema, tokens)
                and not _is_required_name(schema, tokens)):
            stale.append(key)
    return stale


def rule_gui_row_set(before: dict, after: dict, transition: bool = False) -> list[Observation]:
    """One observation per step. transition=True also requires the GUI to have had no stale rows before the step,
    which blames the action that introduced them."""
    now = _stale_rows(after)
    if now is None:
        return []
    if transition:
        prev = _stale_rows(before)
        if prev is None or prev:
            return []
    return [(not now, {} if not now else {"stale_rows": len(now), "rows": ", ".join(sorted(now))})]


NAME = "gui_row_set"
RULE = rule_gui_row_set
