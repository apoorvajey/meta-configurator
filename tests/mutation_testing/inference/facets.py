"""Facets of an app state snapshot that the templates compare."""
from __future__ import annotations

from typing import Callable


def _safe_dict(value) -> dict:
    """Non-dicts become {}: a root-level change-type on the schema tab can collapse the schema to a scalar."""
    return value if isinstance(value, dict) else {}


def _safe_list(value) -> list:
    return value if isinstance(value, list) else []


def _safe_defs(schema: dict) -> dict:
    """$defs and definitions merged into one key-set facet, since schemas use either keyword interchangeably."""
    merged: dict = {}
    merged.update(_safe_dict(schema.get("$defs")))
    merged.update(_safe_dict(schema.get("definitions")))
    return merged


FACETS: dict[str, Callable[[dict], dict]] = {
    "schema":            lambda s: _safe_dict(s.get("de_schema")),
    "schema.properties": lambda s: _safe_dict(_safe_dict(s.get("de_schema")).get("properties")),
    "schema.required":   lambda s: {str(k): None for k in _safe_list(_safe_dict(s.get("de_schema")).get("required"))},
    "schema.defs":       lambda s: _safe_defs(_safe_dict(s.get("de_schema"))),
    "data":              lambda s: _safe_dict(s.get("de_data")),
}


# What each facet reads; copied into every rule card ('schema.properties' does not include nested properties or $defs).
FACET_DEFINITIONS = {
    "schema":            "snapshot['de_schema'] - the Data Editor's copy of the schema, whole document",
    "schema.properties": "snapshot['de_schema']['properties'] - ROOT level only; properties under $defs/definitions or nested objects are not included",
    "schema.required":   "the names listed in snapshot['de_schema']['required'] - ROOT level only",
    "schema.defs":       "snapshot['de_schema']['$defs'] merged with snapshot['de_schema']['definitions'] - ROOT level only, whichever keyword (or both) the schema uses",
    "data":              "snapshot['de_data'] - the Data Editor's data; key-based rules use its TOP-LEVEL keys only",
}


CORRELATION_PAIRS = [
    ("schema.properties", "data"),
    ("schema.required",   "data"),
    ("schema.required",   "schema.properties"),
    ("data",              "schema.properties"),
    ("data",              "schema.required"),
    ("schema.defs",       "schema.properties"),
    ("schema.defs",       "data"),
]


# Containment is directional. ('data', 'schema.required') is left out because data nearly always has more keys,
# and schema.defs because a def name is not expected to be a property or data key.
CONTAINMENT_PAIRS = [
    ("schema.properties", "data"),
    ("schema.required",   "data"),
    ("schema.required",   "schema.properties"),
    ("data",              "schema.properties"),
]


# view -> (text field, store field). The Schema Editor's store is se_data (the schema being edited), not se_schema (the meta-schema).
TEXT_VIEWS = {
    "data":   ("de_text", "de_data"),
    "schema": ("se_text", "se_data"),
}


# Facets that are lists where order matters (FACETS are key-sets and discard order)
ORDERED_FACETS: dict[str, Callable[[dict], list]] = {
    "schema.required": lambda s: _safe_list(_safe_dict(s.get("de_schema")).get("required")),
}


def _facet_has_signal(get_facet, pairs: list) -> bool:
    """False if the facet is empty in every snapshot; 'always unchanged' would then be true about nothing."""
    for p, c in pairs:
        if get_facet(p) or get_facet(c):
            return True
    return False
