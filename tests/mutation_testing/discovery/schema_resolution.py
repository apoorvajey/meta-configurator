"""Find the schema that governs a node: the user's schema in the Data Editor, the meta-schema in the Schema Editor."""
from __future__ import annotations

import json
from pathlib import Path

from typing import Any
from playwright.sync_api import Page
from tests.shared.python.utils import SessionMode
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.schema_path_enumerator import resolve_subschema_at_path, resolve_effective_schema
from tests.mutation_testing.discovery.paths import _value_at_path


# MetaConfigurator's meta-schema, vendored in config/.
_META_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "meta_schema.json"
with open(_META_SCHEMA_PATH, "r", encoding="utf-8") as _f:
    META_SCHEMA_SIMPLIFIED: dict = json.load(_f)


def _effective_schema_to_prop_dict(eff) -> dict:
    """Flatten an EffectiveSchema into the plain dict ValueGenerator expects, so GUI fill and Text View edits resolve constraints alike."""
    out: dict = {}
    if eff.types:
        out["type"] = next(iter(eff.types))
    if eff.pattern is not None:
        out["pattern"] = eff.pattern
    if eff.enum is not None:
        out["enum"] = eff.enum
    if eff.min_length is not None:
        out["minLength"] = eff.min_length
    if eff.max_length is not None:
        out["maxLength"] = eff.max_length
    if eff.minimum is not None:
        out["minimum"] = eff.minimum
    if eff.maximum is not None:
        out["maximum"] = eff.maximum
    return out


def _resolve_prop_schema_dict(governing_schema: dict, path: list, instance: Any) -> dict:
    """The constraints (pattern, enum, type, length, range) governing the node at `path` within `instance`."""
    if not governing_schema:
        return {}
    node_schema = resolve_subschema_at_path(governing_schema, path, instance)
    if node_schema is None:
        return {}
    try:
        leaf_value = _value_at_path(instance, path)
    except (KeyError, IndexError, TypeError):
        leaf_value = None
    eff = resolve_effective_schema(governing_schema, node_schema, leaf_value)
    return _effective_schema_to_prop_dict(eff)


def _resolve_governing_schema(page: Page, mode: str) -> dict | None:
    """Governing schema of a mode: the user's schema (if any) in the Data Editor, the meta-schema in the Schema Editor. Read at execution time."""
    if mode == "dataEditor":
        try:
            schema = TestPanel(page, SessionMode.DataEditor).get_schema()
        except Exception:
            return None
        return schema if schema else None
    return META_SCHEMA_SIMPLIFIED
