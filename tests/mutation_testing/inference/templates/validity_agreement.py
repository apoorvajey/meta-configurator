"""Template validity_agreement: the meta-schema verdict on the edited schema and the app's own verdict agree.

The two are recorded apart (schema_valid_meta, app_logged_invalid) so that a disagreement stays visible. The app writes its
message only when it reloads the schema, so the two directions are separate candidates and the data decides which one holds.

A two-stage action (add-item-required: blank entry added, then filled) has an intermediate state that the app validates and
logs about, and a final state. Each state is compared with its own pair of fields, so a message about the intermediate state
(a duplicate blank in 'required') is never held against the meta-schema verdict on the final schema:
  final state         schema_valid_meta           / app_logged_invalid
  intermediate state  schema_valid_meta_midstep   / app_logged_invalid_midstep   (None on steps without one)
"""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation

VARIANTS = ("app_flags_imply_meta_invalid", "meta_invalid_implies_app_flag")

# (state name, meta-schema field, app-log field)
_STATES = (
    ("final", "schema_valid_meta", "app_logged_invalid"),
    ("midstep", "schema_valid_meta_midstep", "app_logged_invalid_midstep"),
)


def rule_validity_agreement(before: dict, after: dict, variant: str) -> list[Observation]:
    if variant not in VARIANTS:
        raise ValueError(f"validity_agreement: unknown variant {variant!r}")
    observations: list[Observation] = []
    for state, meta_field, app_field in _STATES:
        meta, app = after.get(meta_field), after.get(app_field)
        if not isinstance(meta, bool) or not isinstance(app, bool):
            continue
        detail = {"state": state, "meta_schema_valid": meta, "app_logged_invalid": app}
        if state == "final":
            detail["violations"] = after.get("schema_violation_count")
        if variant == "app_flags_imply_meta_invalid":
            if app:
                observations.append((not meta, detail))
        elif not meta:
            observations.append((app, detail))
    return observations


NAME = "validity_agreement"
RULE = rule_validity_agreement