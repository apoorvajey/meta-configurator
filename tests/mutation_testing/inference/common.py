"""Shared pieces of the templates: observations and access to schema keyword paths."""
from __future__ import annotations

from typing import Any


SCHEMA_KEYWORDS = {
    "$schema", "$defs", "$id", "$ref", "title", "description",
    "type", "properties", "required", "additionalProperties",
    "definitions", "items", "allOf", "anyOf", "oneOf", "not",
    "if", "then", "else", "format", "enum", "const", "minimum",
    "maximum", "minLength", "maxLength", "pattern", "propertyNames",
    "multipleOf", "exclusiveMinimum", "exclusiveMaximum",
}


# Keywords whose children are names, not keywords; not descended into, so a property called 'type' is not taken for a keyword.
NAME_MAP_KEYWORDS = {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}


_MISSING = object()


def _is_schema_key(key: str) -> bool:
    return key in SCHEMA_KEYWORDS or key.startswith("$")


def schema_path_value(snapshot: dict, keys: list[str]) -> Any:
    """de_schema[k1][k2]... of a snapshot, or _MISSING if a step is absent."""
    node = snapshot.get("de_schema")
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return _MISSING
        node = node[k]
    return node


def _enumerate_schema_keyword_paths(schema: Any) -> set[tuple[str, ...]]:
    """Every keyword-only dict path of depth 1 or 2 in a schema."""
    paths: set[tuple[str, ...]] = set()
    if not isinstance(schema, dict):
        return paths
    for k1, v1 in schema.items():
        if not _is_schema_key(k1):
            continue
        paths.add((k1,))
        if isinstance(v1, dict) and k1 not in NAME_MAP_KEYWORDS:
            for k2 in v1:
                if _is_schema_key(k2):
                    paths.add((k1, k2))
    return paths


Observation = tuple[bool, dict]


def _short(value: Any, n: int = 100) -> str:
    return "<missing>" if value is _MISSING else str(value)[:n]


def _keys(obj: Any) -> set:
    return set(obj.keys()) if isinstance(obj, dict) else set()
