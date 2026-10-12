"""Template app_health: the app writes no console error and throws no uncaught exception during the step."""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation


def rule_app_health(before: dict, after: dict) -> list[Observation]:
    """One observation per step that recorded the console (a trace from before the field existed, or a step whose
    console was not being recorded, gives none). A message can land on the snapshot after the edit that caused it:
    the app reloads the schema from a debounced callback."""
    count = after.get("console_errors_since_last_step")
    if isinstance(count, bool) or not isinstance(count, int):
        return []
    if count == 0:
        return [(True, {})]
    return [(False, {"console_errors": count, "first_error": after.get("first_error_message")})]


NAME = "app_health"
RULE = rule_app_health
