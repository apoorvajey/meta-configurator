"""Grouping of actions into categories and tabs, and undo/redo round trips."""
from __future__ import annotations




# Text View prefixes are checked first: 'rename-child-text-' also starts with 'rename-'.
_TEXT_ACTION_PREFIXES = (
    ("edit-text-",              "edit_text"),
    ("add-child-text-",         "add_child_text"),
    ("change-type-text-",       "change_type_text"),
    ("add-array-item-text-",    "add_array_item_text"),
    ("remove-array-item-text-", "remove_array_item_text"),
    ("remove-child-text-",      "remove_child_text"),
    ("rename-child-text-",      "rename_child_text"),
    ("clear-text-",             "clear_text"),
)


def _action_category(action: str) -> str:
    for prefix, cat in _TEXT_ACTION_PREFIXES:
        if action.startswith(prefix):
            return cat
    if any(action.startswith(p) for p in (
        "input-string-", "input-number-", "input-boolean-", "input-select-"
    )): return "edit_field"
    if action.startswith("expand-"):          return "expand"
    if action.startswith("collapse-"):        return "collapse"
    if action.startswith("rename-"):          return "rename"
    if action.startswith("remove-property-"): return "remove"
    if action.startswith("add-property-"):    return "add_property"
    if action == "add-item-required":         return "add_item_required"
    if action.startswith("add-item-"):        return "add_item"
    if action.startswith("mode-active-"):     return "mode_switch"
    if action in ("undo", "redo", "schema_undo", "schema_redo"): return "undo_redo"
    if action == "format-selector":           return "format_toggle"
    return "other"


# Membership rule per category, shown on rule cards.
ACTION_CATEGORY_DESCRIPTIONS = {
    "edit_text":              'action starts with "edit-text-"',
    "add_child_text":         'action starts with "add-child-text-"',
    "change_type_text":       'action starts with "change-type-text-"',
    "add_array_item_text":    'action starts with "add-array-item-text-"',
    "remove_array_item_text": 'action starts with "remove-array-item-text-"',
    "remove_child_text":      'action starts with "remove-child-text-"',
    "rename_child_text":      'action starts with "rename-child-text-"',
    "clear_text":             'action starts with "clear-text-"',
    "edit_field":             'action starts with "input-string-", "input-number-", "input-boolean-" or "input-select-"',
    "expand":                 'action starts with "expand-"',
    "collapse":               'action starts with "collapse-"',
    "rename":                 'action starts with "rename-" (Text View renames excluded)',
    "remove":                 'action starts with "remove-property-"',
    "add_property":           'action starts with "add-property-"',
    "add_item_required":      'action is exactly "add-item-required"',
    "add_item":               'action starts with "add-item-" (except "add-item-required")',
    "mode_switch":            'action starts with "mode-active-" (tab switch)',
    "undo_redo":              'action is "undo", "redo", "schema_undo" or "schema_redo"',
    "format_toggle":          'action is exactly "format-selector"',
    "undo_then_redo":         'an undo immediately followed by its matching redo (same run, adjacent steps; see _round_trip_pairs)',
    "redo_then_undo":         'a redo immediately followed by its matching undo (same run, adjacent steps; see _round_trip_pairs)',
    "other":                  'any action not matched by another category',
}


# Text View actions name their editor; every other action belongs to the tab showing when it was chosen (the previous snapshot).
_TEXT_EDITOR_MARKERS = {"dataEditor": "data", "schemaEditor": "schema"}


def _active_tab(snapshot: dict | None) -> str | None:
    """Tab showing when the snapshot was taken: exactly one of de_gui and se_gui is set.
    Action names cannot tell, since every mode switch is logged as 'mode-active-false'.
    """
    snapshot = snapshot or {}
    if snapshot.get("de_gui") is not None:
        return "data"
    if snapshot.get("se_gui") is not None:
        return "schema"
    return None


def _action_tab(prev: dict | None, curr: dict) -> str:
    action = curr.get("action", "")
    for marker, tab in _TEXT_EDITOR_MARKERS.items():
        if marker in action:
            return tab
    return _active_tab(prev) or "unknown"


def _tabbed_category(prev: dict | None, curr: dict) -> str:
    """Action category plus its tab, e.g. 'add_item@schema'. Used in inference and in checking so actions are classified identically."""
    return f"{_action_category(curr.get('action', ''))}@{_action_tab(prev, curr)}"


_UNDO_ACTIONS = {"undo", "schema_undo"}


_REDO_ACTIONS = {"redo", "schema_redo"}


def _round_trip_pairs(snapshots: list[dict]) -> list[tuple[dict, dict, str]]:
    """(before, after, tag) triples for immediate undo-then-redo or redo-then-undo pairs within one run.

    Three consecutive steps of the same run (strictly increasing step) where the middle one is undo/redo and the last
    its counterpart. 'before' is the snapshot before the pair and 'after' the snapshot after it, skipping the
    intermediate state, so the pair reads as "did we end up where we started" for the ordinary templates.
    Round trips with another action in between are not detected.
    """
    pairs: list[tuple[dict, dict, str]] = []
    for i in range(1, len(snapshots) - 1):
        a, b, c = snapshots[i - 1], snapshots[i], snapshots[i + 1]
        run = a.get("run_id")
        if run is None or b.get("run_id") != run or c.get("run_id") != run:
            continue
        if not (b.get("step", 0) > a.get("step", -1) and c.get("step", 0) > b.get("step", -1)):
            continue
        b_action, c_action = b.get("action", ""), c.get("action", "")
        if b_action in _UNDO_ACTIONS and c_action in _REDO_ACTIONS:
            pairs.append((a, c, "undo_then_redo"))
        elif b_action in _REDO_ACTIONS and c_action in _UNDO_ACTIONS:
            pairs.append((a, c, "redo_then_undo"))
    return pairs
