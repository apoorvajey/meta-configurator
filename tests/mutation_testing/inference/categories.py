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
    if action.startswith(("expand-", "collapse-", "mode-active-")): return "ui_only"
    if action.startswith("rename-"):          return "rename"
    if action.startswith("remove-property-"): return "remove"
    if action.startswith("add-property-"):    return "add_property"
    if action == "add-item-required":         return "add_item_required"
    if action.startswith("add-item-"):        return "add_item"
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
    "rename":                 'action starts with "rename-" (Text View renames excluded)',
    "remove":                 'action starts with "remove-property-"',
    "add_property":           'action starts with "add-property-"',
    "add_item_required":      'action is exactly "add-item-required"',
    "add_item":               'action starts with "add-item-" (except "add-item-required")',
    "undo_redo":              'action is "undo", "redo", "schema_undo" or "schema_redo"',
    "format_toggle":          'action is exactly "format-selector"',
    "undo_then_redo":         'an undo immediately followed by its matching redo (same run, adjacent steps, no failed interaction between; see _round_trip_pairs)',
    "redo_then_undo":         'a redo immediately followed by its matching undo (same run, adjacent steps, no failed interaction between; see _round_trip_pairs)',
    "undo_inverse":           'an undo directly after an edit that changed the data or the schema (same run, adjacent steps, no failed interaction between), compared with the state before that edit; see _undo_pairs. Checked in the loop on such an undo (see _undo_pair)',
    "ui_only":                'action starts with "expand-", "collapse-" or "mode-active-" (view-only actions: open/close a tree node or switch tab; not meant to edit data or schema)',
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


def _base_category(category: str) -> str:
    """The category without its use suffix: 'add_item_required@schema#first' -> 'add_item_required@schema'."""
    return category.partition("#")[0]


def _use_stratum(snapshot: dict | None) -> str | None:
    """'first' for the first step of its category in a run, 'later' for every further one; None when the snapshot has
    no 'nth_use' (recorded by the loop, see RecordingMixin._build_snapshot)."""
    n = (snapshot or {}).get("nth_use")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        return None
    return "first" if n == 1 else "later"


def _is_baseline(snapshot: dict | None) -> bool:
    """True for the state a repair left behind (the loop writes it to the trace with "baseline": true). It is no step of the
    loop: it is only ever the 'before' of the next step, never the 'after' of a pair or part of an undo/redo triple's edit."""
    return bool((snapshot or {}).get("baseline"))


def _steps_adjacent(prev: dict, curr: dict, *, allow_same_step: bool = False) -> bool:
    """True if no interaction lies between two snapshots of one run (the caller has checked the run id).

    The loop numbers every interaction, but an interaction whose action failed records no snapshot (and the loop then
    skips the temporal check, see mutation_loop.py), so a step number that jumps by more than one marks a failed
    interaction between the two snapshots. Snapshots that share a step number belong to one interaction (the action and
    its auto-undo after clear-text, or the action and its repair clicks) and are not a step pair, unless the caller
    allows it (allow_same_step): the undo of an edit may be logged under the edit's own step number.
    A snapshot without a step number cannot show a gap, so it does not break the pair.
    """
    p, c = prev.get("step"), curr.get("step")
    if p is None or c is None:
        return True
    return c - p == 1 or (allow_same_step and c == p)


_UNDO_ACTIONS = {"undo", "schema_undo"}


_REDO_ACTIONS = {"redo", "schema_redo"}


def _round_trip_pairs(snapshots: list[dict]) -> list[tuple[dict, dict, str]]:
    """(before, after, tag) triples for immediate undo-then-redo or redo-then-undo pairs within one run.

    Three consecutive steps of the same run (step numbers n, n+1, n+2, so no failed interaction between them) where the
    middle one is undo/redo and the last its counterpart. 'before' is the snapshot before the pair and 'after' the snapshot after it, skipping the
    intermediate state, so the pair reads as "did we end up where we started" for the ordinary templates.
    Round trips with another action in between are not detected.
    """
    pairs: list[tuple[dict, dict, str]] = []
    for i in range(1, len(snapshots) - 1):
        a, b, c = snapshots[i - 1], snapshots[i], snapshots[i + 1]
        run = a.get("run_id")
        if run is None or b.get("run_id") != run or c.get("run_id") != run:
            continue
        if _is_baseline(b) or _is_baseline(c):
            continue
        if not (_steps_adjacent(a, b) and _steps_adjacent(b, c)):
            continue
        b_action, c_action = b.get("action", ""), c.get("action", "")
        if b_action in _UNDO_ACTIONS and c_action in _REDO_ACTIONS:
            pairs.append((a, c, "undo_then_redo"))
        elif b_action in _REDO_ACTIONS and c_action in _UNDO_ACTIONS:
            pairs.append((a, c, "redo_then_undo"))
    return pairs


# Actions that are not edits: an undo after one of them has nothing to take back.
_NOT_UNDOABLE_CATEGORIES = {"undo_redo", "ui_only", "format_toggle"}


UNDO_INVERSE_CATEGORY = "undo_inverse@any"


def _undo_pair(a: dict | None, b: dict | None, c: dict | None) -> tuple[dict, dict, str] | None:
    """The (before, after, tag) pair for three consecutive snapshots of one run: a = before an edit, b = after the edit,
    c = after an undo. None if they are not such a triple.

    'before' is a and 'after' is c, copied with the edit's action name added as 'undone_action'. The edit must have
    changed the data or the schema, otherwise the undo has nothing to restore. Mining (_undo_pairs) and the loop's
    check (inferred_rules.evaluate_inferred_rules) both use this, so a rule is checked on the pairs it was mined from.
    """
    if not (a and b and c) or _is_baseline(b) or _is_baseline(c):
        return None
    run = a.get("run_id")
    if run is None or b.get("run_id") != run or c.get("run_id") != run:
        return None
    if not (_steps_adjacent(a, b) and _steps_adjacent(b, c, allow_same_step=True)):
        return None  # the loop may log an undo under the step number of the edit it undoes, so the undo may repeat it
    if c.get("action", "") not in _UNDO_ACTIONS or _action_category(b.get("action", "")) in _NOT_UNDOABLE_CATEGORIES:
        return None
    if a.get("de_data") == b.get("de_data") and a.get("de_schema") == b.get("de_schema"):
        return None
    return a, {**c, "undone_action": b.get("action", "")}, UNDO_INVERSE_CATEGORY


def _undo_pairs(snapshots: list[dict]) -> list[tuple[dict, dict, str]]:
    """(before, after, tag) for an edit directly followed by an undo, in one run (three consecutive snapshots, with no
    failed interaction between them: the edit is one step after 'before', the undo one step after the edit or, for the
    loop's auto-undo, under the edit's own step number). See _undo_pair."""
    pairs = (_undo_pair(snapshots[i - 1], snapshots[i], snapshots[i + 1]) for i in range(1, len(snapshots) - 1))
    return [p for p in pairs if p is not None]