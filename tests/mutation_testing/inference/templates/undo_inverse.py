"""Template undo_inverse: an undo brings back the state from before the edit it undoes."""
from __future__ import annotations

from deepdiff import DeepDiff

from tests.mutation_testing.inference.common import Observation
from tests.mutation_testing.inference.facets import RESTORE_STORES


def _first_diff(before, after) -> str:
    try:
        diff = DeepDiff(before, after)  # order matters: a list that comes back in another order or with a repeated entry is a difference
        return str(next((p for v in diff.values() for p in v), ""))[:120]
    except Exception:
        return "?"


def rule_undo_inverse(before: dict, after: dict, store: str) -> list[Observation]:
    """before: the snapshot before the edit. after: the snapshot after the undo, with 'undone_action' set (see _undo_pairs).
    The store is skipped when either snapshot has no reading of it (for example a Text View that was not showing).
    """
    read = RESTORE_STORES[store]
    was, now = read(before), read(after)
    if was is None or now is None:
        return []
    if was == now:
        return [(True, {})]
    return [(False, {"store": store, "undone": after.get("undone_action"), "first_diff": _first_diff(was, now)})]


NAME = "undo_inverse"
RULE = rule_undo_inverse
