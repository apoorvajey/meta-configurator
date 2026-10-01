"""Temporal invariants: each compares two consecutive state snapshots (before and after an action) to check
that the right things changed and the right things did not.

TemporalInvariantChecker takes prev_snapshot, curr_snapshot and the testid of the action, and
returns an InvariantReport in the same format as InvariantChecker.

    TEMP-EDIT-01      after edit-field: schema.properties keys unchanged
    TEMP-EDIT-02      after edit-field (Data Editor): schema entirely unchanged
    TEMP-EDIT-03      after edit-field: len(schema.required) unchanged
    TEMP-EXPAND-01    after expand: data unchanged
    TEMP-EXPAND-02    after expand: schema unchanged
    TEMP-COLLAPSE-01  after collapse: schema unchanged
    TEMP-COLLAPSE-02  after collapse: data unchanged
    TEMP-RENAME-01    after rename: number of data keys unchanged
    TEMP-RENAME-02    after rename: len(schema.properties) unchanged
    TEMP-RENAME-03    after rename: schema.properties has exactly one key swapped
    TEMP-RENAME-04    after rename: data has exactly one key swapped
    TEMP-ADD-01       after add-item: schema.properties keys unchanged
    TEMP-ADD-02       after add-item-required: len(schema.required) grows by exactly 1
    TEMP-ADD-03       after add-item-required: a new required key appears in data
    TEMP-MODE-01      after mode switch: data unchanged
    TEMP-MODE-02      after mode switch: schema unchanged
    NEW-07            the schema state stays an object, after any action

Inferred rules from inferred_rules.json are checked as well (see evaluate_inferred_rules).
"""

from __future__ import annotations

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

    Each snapshot is a dict with:
        de_data:   Data Editor data
        de_schema: Data Editor schema
        se_schema: Schema Editor schema (the meta-schema)
        se_data:   Schema Editor data (the user schema when the Schema Editor is active)
        action:    testid of the action that produced the snapshot
        step:      step number

        checker = TemporalInvariantChecker(prev=prev_snap, curr=curr_snap, action=el.testid)
        report = checker.run_all()
        report.print()
    """

    def __init__(self, prev: dict, curr: dict, action: str):
        self.prev   = prev or {}
        self.curr   = curr or {}
        self.action = action

        # accessors
        self.prev_data   = self.prev.get("de_data", {}) or {}
        self.curr_data   = self.curr.get("de_data", {}) or {}
        self.prev_schema = self.prev.get("de_schema", {}) or {}
        self.curr_schema = self.curr.get("de_schema", {}) or {}
        if not isinstance(self.prev_schema, dict):
            self.prev_schema = {}
        if not isinstance(self.curr_schema, dict):
            self.curr_schema = {}

        self.prev_data_keys = set(self.prev_data.keys()) if isinstance(self.prev_data, dict) else set()
        self.curr_data_keys = set(self.curr_data.keys()) if isinstance(self.curr_data, dict) else set()
        self.prev_prop_keys = set(self.prev_schema.get("properties", {}).keys())
        self.curr_prop_keys = set(self.curr_schema.get("properties", {}).keys())

        self.prev_step = self.prev.get("step", 0)

    def run_all(self) -> InvariantReport:
        report = InvariantReport(mode="temporal")
        checks = [
            self._inv_temp01_edit_field_preserves_schema_properties,
            self._inv_temp03_edit_field_preserves_required_count,
            self._inv_temp04_expand_preserves_data,
            self._inv_temp05_expand_preserves_schema,
            self._inv_temp06_collapse_preserves_data,
            self._inv_temp07_collapse_preserves_schema,
            self._inv_temp08_rename_preserves_data_key_count,
            self._inv_temp09_rename_preserves_schema_property_count,
            self._inv_temp16_add_item_preserves_schema_properties,
            self._inv_new01_rename_swaps_schema_property_key,
            self._inv_new02_rename_swaps_data_key,
            self._inv_new03_edit_field_data_editor_preserves_schema,
            self._inv_new04_add_item_required_increases_required_count,
            self._inv_new05_mode_switch_preserves_data,
            self._inv_new06_mode_switch_preserves_schema,
            self._inv_temp_add03_add_item_required_propagates_to_data,
            self._inv_new07_schema_state_remains_an_object,
        ]
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
        # Inferred rules from inferred_rules.json, checked by the same functions that inferred them. Skipped if the file does not exist.
        try:
            from tests.mutation_testing.invariant_inference_engine import evaluate_inferred_rules
            if self.prev:
                report.results.extend(evaluate_inferred_rules(self.prev, self.curr, self.action))
        except Exception as e:
            report.results.append(InvariantResult(
                name="INFERRED rules",
                passed=False,
                message=f"Exception while evaluating inferred rules: {e}",
            ))
        return report

    def _inv_temp01_edit_field_preserves_schema_properties(self) -> InvariantResult | None:
        """TEMP-EDIT-01: editing a field value never adds or removes keys in schema.properties."""
        if not _is_edit_field(self.action):
            return None

        name = "TEMP-EDIT-01 edit-field preserves schema.properties.keys()"
        passed = self.prev_prop_keys == self.curr_prop_keys
        added   = sorted(self.curr_prop_keys - self.prev_prop_keys)
        removed = sorted(self.prev_prop_keys - self.curr_prop_keys)

        return InvariantResult(
            name, passed,
            "schema.properties.keys() unchanged after edit-field ✓" if passed
            else "schema.properties.keys() changed after edit-field",
            {} if passed else {"added": added, "removed": removed},
            evidence=[
                f"action={self.action}",
                f"→ {'unchanged ✓' if passed else f'added={added} removed={removed} ✗'}",
            ],
        )

    def _inv_temp03_edit_field_preserves_required_count(self) -> InvariantResult | None:
        """TEMP-EDIT-03: editing a field value never changes schema.required. It only changes through
        add-item-required or remove-property.
        """
        if not _is_edit_field(self.action):
            return None

        name = "TEMP-EDIT-03 edit-field preserves schema.required count"
        prev_count = len(self.prev_schema.get("required", []))
        curr_count = len(self.curr_schema.get("required", []))
        passed = prev_count == curr_count

        return InvariantResult(
            name, passed,
            f"schema.required count unchanged after edit-field ({curr_count}) ✓" if passed
            else f"schema.required count changed after edit-field: {prev_count} → {curr_count}",
            {} if passed else {"before": prev_count, "after": curr_count},
            evidence=[
                f"action={self.action}",
                f"schema.required: {prev_count} → {curr_count}",
                f"→ {'unchanged ✓' if passed else f'delta={curr_count - prev_count} ✗'}",
            ],
        )

    def _inv_temp04_expand_preserves_data(self) -> InvariantResult | None:
        """TEMP-EXPAND-01: expanding a tree node is navigation only, so the data is identical before and after."""
        if not _is_expand(self.action):
            return None

        name = "TEMP-EXPAND-01 expand preserves data"
        passed = self.prev_data == self.curr_data

        return InvariantResult(
            name, passed,
            "data unchanged after expand ✓" if passed
            else "data changed after expand - unexpected",
            {} if passed else {
                "prev_data_keys": sorted(self.prev_data_keys),
                "curr_data_keys": sorted(self.curr_data_keys),
            },
            evidence=[f"action={self.action}",
                      f"→ {'data unchanged ✓' if passed else 'data changed ✗'}"],
        )

    def _inv_temp05_expand_preserves_schema(self) -> InvariantResult | None:
        """TEMP-EXPAND-02: expanding a tree node does not modify the schema."""
        if not _is_expand(self.action):
            return None

        name = "TEMP-EXPAND-02 expand preserves schema"
        passed = self.prev_schema == self.curr_schema

        return InvariantResult(
            name, passed,
            "schema unchanged after expand ✓" if passed
            else "schema changed after expand - unexpected",
            {} if passed else {
                "prev_prop_keys": sorted(self.prev_prop_keys),
                "curr_prop_keys": sorted(self.curr_prop_keys),
            },
            evidence=[f"action={self.action}",
                      f"→ {'schema unchanged ✓' if passed else 'schema changed ✗'}"],
        )

    def _inv_temp06_collapse_preserves_data(self) -> InvariantResult | None:
        """TEMP-COLLAPSE-02: collapsing a tree node is navigation only, so the data is identical before and
        after.
        """
        if not _is_collapse(self.action):
            return None

        name = "TEMP-COLLAPSE-02 collapse preserves data"
        passed = self.prev_data == self.curr_data

        return InvariantResult(
            name, passed,
            "data unchanged after collapse ✓" if passed
            else "data changed after collapse - unexpected",
            {} if passed else {
                "prev_data_keys": sorted(self.prev_data_keys),
                "curr_data_keys": sorted(self.curr_data_keys),
            },
            evidence=[f"action={self.action}",
                      f"→ {'data unchanged ✓' if passed else 'data changed ✗'}"],
        )

    def _inv_temp07_collapse_preserves_schema(self) -> InvariantResult | None:
        """TEMP-COLLAPSE-01: collapsing a tree node does not modify the schema."""
        if not _is_collapse(self.action):
            return None

        name = "TEMP-COLLAPSE-01 collapse preserves schema"
        passed = self.prev_schema == self.curr_schema

        return InvariantResult(
            name, passed,
            "schema unchanged after collapse ✓" if passed
            else "schema changed after collapse - unexpected",
            {} if passed else {
                "prev_prop_keys": sorted(self.prev_prop_keys),
                "curr_prop_keys": sorted(self.curr_prop_keys),
            },
            evidence=[f"action={self.action}",
                      f"→ {'schema unchanged ✓' if passed else 'schema changed ✗'}"],
        )

    def _inv_temp08_rename_preserves_data_key_count(self) -> InvariantResult | None:
        """TEMP-RENAME-01: renaming a property does not add or remove top-level data keys."""
        if not _is_rename(self.action):
            return None

        name = "TEMP-RENAME-01 rename preserves data key count"
        prev_count = len(self.prev_data_keys)
        curr_count = len(self.curr_data_keys)
        passed = prev_count == curr_count

        return InvariantResult(
            name, passed,
            f"data key count unchanged after rename ({curr_count}) ✓" if passed
            else f"data key count changed after rename: {prev_count} → {curr_count}",
            {} if passed else {"before": prev_count, "after": curr_count},
            evidence=[f"action={self.action}",
                      f"→ {'count unchanged ✓' if passed else f'{prev_count}→{curr_count} ✗'}"],
        )

    def _inv_temp09_rename_preserves_schema_property_count(self) -> InvariantResult | None:
        """TEMP-RENAME-02: renaming a property does not add or remove schema.properties entries."""
        if not _is_rename(self.action):
            return None

        name = "TEMP-RENAME-02 rename preserves schema.properties count"
        prev_count = len(self.prev_prop_keys)
        curr_count = len(self.curr_prop_keys)
        passed = prev_count == curr_count

        return InvariantResult(
            name, passed,
            f"schema.properties count unchanged after rename ({curr_count}) ✓" if passed
            else f"schema.properties count changed: {prev_count} → {curr_count}",
            {} if passed else {"before": prev_count, "after": curr_count},
            evidence=[f"action={self.action}",
                      f"→ {'count unchanged ✓' if passed else f'{prev_count}→{curr_count} ✗'}"],
        )

    def _inv_temp16_add_item_preserves_schema_properties(self) -> InvariantResult | None:
        """TEMP-ADD-01: adding an array item never adds or removes schema.properties keys."""
        if not _is_add_item(self.action):
            return None

        name = "TEMP-ADD-01 add-item preserves schema.properties.keys()"
        passed = self.prev_prop_keys == self.curr_prop_keys
        added   = sorted(self.curr_prop_keys - self.prev_prop_keys)
        removed = sorted(self.prev_prop_keys - self.curr_prop_keys)

        return InvariantResult(
            name, passed,
            "schema.properties.keys() unchanged after add-item ✓" if passed
            else "schema.properties.keys() changed after add-item - unexpected",
            {} if passed else {"added": added, "removed": removed},
            evidence=[f"action={self.action}",
                      f"→ {'unchanged ✓' if passed else f'added={added} removed={removed} ✗'}"],
        )

    def _inv_new01_rename_swaps_schema_property_key(self) -> InvariantResult | None:
        """TEMP-RENAME-03: a rename removes one key from schema.properties and adds one. A nested rename
        changes no top-level key, which is also valid.
        """
        if not _is_rename(self.action) or self.prev_step == 0:
            return None

        name = "TEMP-RENAME-03 rename swaps exactly one schema.properties key"
        added   = self.curr_prop_keys - self.prev_prop_keys
        removed = self.prev_prop_keys - self.curr_prop_keys

        # valid: one swap (top-level rename) or no change (nested rename)
        passed = len(added) == len(removed) and len(added) <= 1

        return InvariantResult(
            name, passed,
            "schema.properties key swap is clean after rename ✓" if passed
            else "schema.properties had unexpected key changes after rename",
            {} if passed else {"added": sorted(added), "removed": sorted(removed)},
            evidence=[
                f"action={self.action}",
                f"added={sorted(added)} removed={sorted(removed)}",
                f"→ {'clean swap ✓' if passed else 'unexpected change ✗'}",
            ],
        )

    def _inv_new02_rename_swaps_data_key(self) -> InvariantResult | None:
        """TEMP-RENAME-04: a rename removes one key from data and adds one. A nested rename changes no
        top-level key, which is also valid.
        """
        if not _is_rename(self.action) or self.prev_step == 0:
            return None

        name = "TEMP-RENAME-04 rename swaps exactly one data key"
        added   = self.curr_data_keys - self.prev_data_keys
        removed = self.prev_data_keys - self.curr_data_keys

        # valid: one swap or no change (nested rename)
        passed = len(added) == len(removed) and len(added) <= 1

        return InvariantResult(
            name, passed,
            "data key swap is clean after rename ✓" if passed
            else "data had unexpected key changes after rename",
            {} if passed else {"added": sorted(added), "removed": sorted(removed)},
            evidence=[
                f"action={self.action}",
                f"added={sorted(added)} removed={sorted(removed)}",
                f"→ {'clean swap ✓' if passed else 'unexpected change ✗'}",
            ],
        )

    def _inv_new03_edit_field_data_editor_preserves_schema(self) -> InvariantResult | None:
        """TEMP-EDIT-02: editing a data field in Data Editor mode never modifies the schema. Schema Editor
        meta-fields (title, type, $ref, ...) are excluded because they edit the schema by design.
        """
        if not _is_edit_field_data_editor(self.action):
            return None

        name = "TEMP-EDIT-02 edit-field (DataEditor) preserves schema entirely"
        passed = self.prev_schema == self.curr_schema

        return InvariantResult(
            name, passed,
            "schema entirely unchanged after DataEditor edit-field ✓" if passed
            else "schema changed after DataEditor edit-field - unexpected",
            {} if passed else {
                "prev_prop_keys": sorted(self.prev_prop_keys),
                "curr_prop_keys": sorted(self.curr_prop_keys),
            },
            evidence=[
                f"action={self.action}",
                f"→ {'schema unchanged ✓' if passed else 'schema changed ✗'}",
            ],
        )

    def _inv_new04_add_item_required_increases_required_count(self) -> InvariantResult | None:
        """TEMP-ADD-02: adding an item to the required array increases len(schema.required) by exactly 1."""
        if not _is_add_item_required(self.action):
            return None

        name = "TEMP-ADD-02 add-item-required increases required count by 1"
        prev_count = len(self.prev_schema.get("required", []))
        curr_count = len(self.curr_schema.get("required", []))
        passed = curr_count == prev_count + 1

        return InvariantResult(
            name, passed,
            f"required count increased by 1 ({prev_count} → {curr_count}) ✓" if passed
            else f"required count unexpected change: {prev_count} → {curr_count}",
            {} if passed else {"before": prev_count, "after": curr_count},
            evidence=[
                f"action={self.action}",
                f"required: {prev_count} → {curr_count}",
                f"→ {'+1 ✓' if passed else f'delta={curr_count - prev_count} ✗'}",
            ],
        )

    def _inv_new05_mode_switch_preserves_data(self) -> InvariantResult | None:
        """TEMP-MODE-01: switching between Data Editor and Schema Editor does not modify the user data."""
        if not _is_mode_switch(self.action):
            return None

        name = "TEMP-MODE-01 mode switch preserves data"
        passed = self.prev_data == self.curr_data

        return InvariantResult(
            name, passed,
            "data unchanged after mode switch ✓" if passed
            else "data changed after mode switch - unexpected",
            {} if passed else {
                "prev_data_keys": sorted(self.prev_data_keys),
                "curr_data_keys": sorted(self.curr_data_keys),
            },
            evidence=[
                f"action={self.action}",
                f"→ {'data unchanged ✓' if passed else 'data changed ✗'}",
            ],
        )

    def _inv_new06_mode_switch_preserves_schema(self) -> InvariantResult | None:
        """TEMP-MODE-02: switching between Data Editor and Schema Editor does not modify the schema."""
        if not _is_mode_switch(self.action):
            return None

        name = "TEMP-MODE-02 mode switch preserves schema"
        passed = self.prev_schema == self.curr_schema

        return InvariantResult(
            name, passed,
            "schema unchanged after mode switch ✓" if passed
            else "schema changed after mode switch - unexpected",
            {} if passed else {
                "prev_prop_keys": sorted(self.prev_prop_keys),
                "curr_prop_keys": sorted(self.curr_prop_keys),
            },
            evidence=[
                f"action={self.action}",
                f"→ {'schema unchanged ✓' if passed else 'schema changed ✗'}",
            ],
        )

    def _inv_temp_add03_add_item_required_propagates_to_data(self) -> InvariantResult | None:
        """TEMP-ADD-03: after add-item-required, any new required key appears in data, whether or not
        it is defined in schema.properties.

        The rule first applied only when the key was in schema.properties. Two clean reproductions with
        random required names and no property definition contradicted that: the app creates the data slot
        either way. That is intended behaviour.
        """
        if not _is_add_item_required(self.action):
            return None

        name = "TEMP-ADD-03 add-item-required propagates to data"

        prev_required = set(self.prev_schema.get("required", []))
        curr_required = set(self.curr_schema.get("required", []))
        new_keys = curr_required - prev_required

        if not new_keys:
            return InvariantResult(name, True, "No new required keys - skip")

        curr_data_keys = set(self.curr_data.keys()) if isinstance(self.curr_data, dict) else set()
        curr_props = set(self.curr_schema.get("properties", {}).keys())  # informational only

        failures = []
        for key in new_keys:
            if key not in curr_data_keys:
                failures.append(f"'{key}' added to schema.required but missing from data")

        passed = not failures
        return InvariantResult(
            name, passed,
            "required key propagation to data is correct ✓" if passed
            else f"required key propagation broken: {failures}",
            {} if passed else {"failures": failures},
            evidence=[
                f"action={self.action}",
                f"new required keys={sorted(new_keys)}",
                f"defined in properties={sorted(new_keys & curr_props)}",
                f"→ {'correct ✓' if passed else 'broken ✗'}",
            ],
        )

    def _inv_new07_schema_state_remains_an_object(self) -> InvariantResult | None:
        """NEW-07: the schema state (de_schema) is always an object, never a bare scalar or array. Checked on
        every transition, since any action could corrupt it. A root-level change-type-text-schemaEditor
        action once collapsed de_schema to the int 165 and crashed this class before the isinstance guard
        in __init__.

        This reads self.curr.get("de_schema") directly and not self.curr_schema, which __init__ coerces to
        {} for non-dicts. The coerced value would make a corrupted schema look empty and hide the violation.
        """
        name = "NEW-07 schema state remains an object"
        raw_curr_schema = self.curr.get("de_schema")
        passed = isinstance(raw_curr_schema, dict)

        return InvariantResult(
            name, passed,
            "schema is an object ✓" if passed
            else f"schema is no longer an object - got "
                 f"{type(raw_curr_schema).__name__}: {str(raw_curr_schema)[:100]}",
            {} if passed else {
                "actual_type": type(raw_curr_schema).__name__,
                "actual_value": str(raw_curr_schema)[:200],
            },
            evidence=[
                f"action={self.action}",
                f"→ {'object ✓' if passed else 'NOT an object ✗'}",
            ],
        )