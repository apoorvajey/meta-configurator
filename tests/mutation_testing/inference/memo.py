"""Optional per-run memo for template readings that are expensive and shared by many candidates.

The engine runs many candidates over the same snapshots (edit_distance with two modes, gui_control_by_type with one candidate
per control kind). Inside `with memoizing():` a template can ask `memoized(key, compute)` and the reading is computed once. Outside
the block (the mutation loop checking one step at a time) nothing is stored, so nothing accumulates over a long run.

Keys use id() of snapshot dicts, which is safe only while those dicts stay alive; the engine holds every snapshot for the whole run.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Callable

_ACTIVE: dict | None = None


@contextmanager
def memoizing():
    global _ACTIVE
    previous, _ACTIVE = _ACTIVE, {}
    try:
        yield
    finally:
        _ACTIVE = previous


def memoized(key, compute: Callable):
    """compute() once per key while memoizing() is active, otherwise on every call."""
    if _ACTIVE is None:
        return compute()
    if key not in _ACTIVE:
        _ACTIVE[key] = compute()
    return _ACTIVE[key]
