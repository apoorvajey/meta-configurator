"""Temporal invariants: each compares two consecutive state snapshots (before and after an action) to check
that the right things changed and the right things did not.

TemporalInvariantChecker takes prev_snapshot, curr_snapshot and the testid of the action and returns an
InvariantReport like InvariantChecker. Each invariant is one file in invariants/temporal/<action>/, loaded
at the bottom of this file (list_temporal_invariants() lists them). Inferred rules from inferred_rules.json
are checked as well (evaluate_inferred_rules).
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.invariants.invariants_core import InvariantResult, InvariantReport


def _is_edit_field(action: str) -> bool:
    return any(action.startswith(p) for p in (
        "input-string-", "input-number-", "input-boolean-", "input-select-"
    ))

def _is_edit_field_data_editor(action: str) -> bool:
    """Edit-field actions in the Data Editor only; schema meta-fields are excluded."""
    if not _is_edit_field(action):
        return False
    # schema editor fields that edit schema content, excluded
    schema_editor_fields = (
        "input-select-title", "input-select-type", "input-select-$ref",
        "input-select-$id", "input-select-$schema", "input-select-description",
        "input-select-properties", "input-select-required",
        "input-select-additionalProperties", "input-select-definitions",
    )
    return not any(action.startswith(f) for f in schema_editor_fields)

def _is_expand(action: str) -> bool:
    return action.startswith("expand-")

def _is_collapse(action: str) -> bool:
    return action.startswith("collapse-")

def _is_rename(action: str) -> bool:
    return action.startswith("rename-")

def _is_add_item(action: str) -> bool:
    return action.startswith("add-item-")

def _is_add_item_required(action: str) -> bool:
    return action == "add-item-required"

def _is_mode_switch(action: str) -> bool:
    return action.startswith("mode-active-")


class TemporalInvariantChecker:
    """Checks the temporal invariants between two consecutive snapshots.

    Each snapshot is a dict with de_data and de_schema (Data Editor data and schema), se_schema (the
    meta-schema), se_data (the user schema when the Schema Editor is active), action (testid) and step.

        report = TemporalInvariantChecker(prev=prev_snap, curr=curr_snap, action=el.testid).run_all()
    """

    def __init__(self, prev: dict, curr: dict, action: str, pre_edit: dict | None = None):
        self.prev   = prev or {}
        self.curr   = curr or {}
        self.action = action
        # Snapshot from before the edit that prev records; only the undo_inverse rules use it (see evaluate_inferred_rules)
        self.pre_edit = pre_edit

        self.prev_data = self.prev.get("de_data", {})
        self.curr_data = self.curr.get("de_data", {})
        if self.prev_data is None:
            self.prev_data = {}
        if self.curr_data is None:
            self.curr_data = {}
        self.prev_schema = self.prev.get("de_schema", {}) or {}
        self.curr_schema = self.curr.get("de_schema", {}) or {}
        if not isinstance(self.prev_schema, dict):
            self.prev_schema = {}
        if not isinstance(self.curr_schema, dict):
            self.curr_schema = {}

        self.prev_data_keys = set(self.prev_data.keys()) if isinstance(self.prev_data, dict) else set()
        self.curr_data_keys = set(self.curr_data.keys()) if isinstance(self.curr_data, dict) else set()
        # "properties" can be something else than an object while the schema is being edited (e.g. a list)
        prev_props, curr_props = self.prev_schema.get("properties"), self.curr_schema.get("properties")
        self.prev_prop_keys = set(prev_props.keys()) if isinstance(prev_props, dict) else set()
        self.curr_prop_keys = set(curr_props.keys()) if isinstance(curr_props, dict) else set()

        self.prev_step = self.prev.get("step", 0)

    def run_all(self) -> InvariantReport:
        report = InvariantReport(mode="temporal")
        # every plugin in tests/invariants/temporal/, in the order of the invariant ids
        checks = [getattr(self, plugin.name) for plugin in list_temporal_invariants()]
        for check in checks:
            try:
                result = check()
                if result is not None:
                    report.results.append(result)
            except Exception as e:
                report.results.append(InvariantResult(
                    name=check.__name__,
                    passed=False,
                    message=f"Exception during check: {e}",
                ))
        # inferred_rules.json, if present
        try:
            from tests.mutation_testing.inference.inferred_rules import evaluate_inferred_rules
            if self.prev:
                report.results.extend(evaluate_inferred_rules(self.prev, self.curr, self.action, pre_edit=self.pre_edit))
        except Exception as e:
            report.results.append(InvariantResult(
                name="INFERRED rules",
                passed=False,
                message=f"Exception while evaluating inferred rules: {e}",
            ))
        return report


# Temporal invariant plugins: one file per invariant in tests/invariants/temporal/<action>/
@dataclass(frozen=True)
class TemporalInvariantPlugin:
    id: str        # e.g. "TEMP-ADD-03", as shown in reports
    name: str      # method name, e.g. "_inv_temp_add03_add_item_required_propagates_to_data"
    check: object  # callable(ctx) -> InvariantResult | None, ctx is the TemporalInvariantChecker
    module: str    # path below tests/invariants/temporal, e.g. "add/required_key_appears_in_data"


TEMPORAL_REGISTRY: dict[str, TemporalInvariantPlugin] = {}


def _bound_method(name: str, check):
    """Wrap a plugin's check(ctx) as a method. __name__ is kept because run_all() reports a crashed check
    under it."""
    def method(self):
        return check(self)
    method.__name__ = name
    method.__doc__ = check.__doc__
    return method


def load_temporal_plugins() -> dict[str, TemporalInvariantPlugin]:
    """Import every module below invariants/temporal and attach its check() to TemporalInvariantChecker.
    Safe to call more than once. A broken plugin raises: a silently missing invariant would make a run look
    healthier than it is.

    A plugin defines ID (e.g. "TEMP-ADD-02"), NAME (starting with "_inv_"; a crashed check is reported under
    it, so keep it stable) and check(ctx), which returns an InvariantResult, or None when the rule does not
    apply to this action.
    """
    import importlib
    import pkgutil
    from tests.invariants import temporal as package

    depth = len(package.__name__.split("."))
    for info in sorted(pkgutil.walk_packages(package.__path__, f"{package.__name__}."), key=lambda i: i.name):
        parts = info.name.split(".")[depth:]
        if info.ispkg or any(p.startswith("_") for p in parts):
            continue
        label = "/".join(parts)
        module = importlib.import_module(info.name)
        inv_id, name, check = (getattr(module, a, None) for a in ("ID", "NAME", "check"))
        if not (isinstance(inv_id, str) and isinstance(name, str) and name.startswith("_inv_") and callable(check)):
            raise ImportError(f"{label}: needs ID (str), NAME starting with '_inv_' and a callable check(ctx)")
        for other in TEMPORAL_REGISTRY.values():
            if other.module != label and (inv_id == other.id or name == other.name):
                raise ImportError(f"{label}: ID {inv_id} or NAME {name} is already defined in {other.module}")
        TEMPORAL_REGISTRY[name] = TemporalInvariantPlugin(inv_id, name, check, label)
        setattr(TemporalInvariantChecker, name, _bound_method(name, check))
    return TEMPORAL_REGISTRY


def list_temporal_invariants() -> list[TemporalInvariantPlugin]:
    """All loaded temporal invariants, ordered by id. run_all() runs them in this order."""
    return sorted(load_temporal_plugins().values(), key=lambda p: p.id)


load_temporal_plugins()   # must stay the last statement: the plugins import names defined above