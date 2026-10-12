"""Template validity_agreement: the meta-schema verdict on the edited schema and the app's own verdict agree.

The two are recorded apart (schema_valid_meta, app_logged_invalid) so that a disagreement stays visible. The app writes its
message only when it reloads the schema, so the two directions are separate candidates and the data decides which one holds.
"""
from __future__ import annotations

from tests.mutation_testing.inference.common import Observation

VARIANTS = ("app_flags_imply_meta_invalid", "meta_invalid_implies_app_flag")


def rule_validity_agreement(before: dict, after: dict, variant: str) -> list[Observation]:
    if variant not in VARIANTS:
        raise ValueError(f"validity_agreement: unknown variant {variant!r}")
    meta, app = after.get("schema_valid_meta"), after.get("app_logged_invalid")
    if not isinstance(meta, bool) or not isinstance(app, bool):
        return []
    detail = {"meta_schema_valid": meta, "app_logged_invalid": app, "violations": after.get("schema_violation_count")}
    if variant == "app_flags_imply_meta_invalid":
        return [(not meta, detail)] if app else []
    return [(app, detail)] if not meta else []


NAME = "validity_agreement"
RULE = rule_validity_agreement
