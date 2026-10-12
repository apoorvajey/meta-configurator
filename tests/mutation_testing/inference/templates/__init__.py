"""Invariant templates. Each module in this package is one template and defines:

    NAME  the template name used in rules and reports
    RULE  the function rule(before, after, **params) -> list of (held, detail) that evaluates the
          template; an empty list means the guard is false for this step. A rule promoted in the
          negative direction passes when held is False.

Modules are discovered automatically. To add a template, add a file here.

Every RULE is wrapped so that it also accepts guard=<name> (see inference/guards.py): the rule then runs only on steps
where the guard is True, otherwise it gives no observation. A template module does not handle guards itself.
"""
from __future__ import annotations

import functools
import importlib
import pkgutil
from typing import Callable

from tests.mutation_testing.inference.guards import GUARDS


def _with_guard(rule: Callable) -> Callable:
    @functools.wraps(rule)
    def guarded(before: dict, after: dict, guard: str | None = None, **params):
        if guard is not None and GUARDS[guard](before, after) is not True:
            return []
        return rule(before, after, **params)
    return guarded


def load_rules() -> dict[str, Callable]:
    rules: dict[str, Callable] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        name, rule = getattr(module, "NAME", None), getattr(module, "RULE", None)
        if not (isinstance(name, str) and callable(rule)):
            raise ImportError(f"{module.__name__}: needs NAME (str) and RULE (callable)")
        if name in rules:
            raise ImportError(f"{module.__name__}: template '{name}' is defined twice")
        rules[name] = _with_guard(rule)
    return rules


RULES = load_rules()