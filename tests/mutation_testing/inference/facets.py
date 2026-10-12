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


def _schema_valid_facet(snapshot: dict) -> dict | None:
    """{'valid': bool}, or None when the snapshot has no verdict (a trace from before the field existed, or validity
    could not be determined). None means "not recorded": stability and change_count skip such a step."""
    value = snapshot.get("schema_valid_meta")
    return {"valid": value} if isinstance(value, bool) else None


# A reader returns None when the facet is not recorded in the snapshot. Only the schema.valid facet does; the others always return a dict.
FACETS: dict[str, Callable[[dict], dict | None]] = {
    "schema":            lambda s: _safe_dict(s.get("de_schema")),
    "schema.properties": lambda s: _safe_dict(_safe_dict(s.get("de_schema")).get("properties")),
    "schema.required":   lambda s: {str(k): None for k in _safe_list(_safe_dict(s.get("de_schema")).get("required"))},
    "schema.defs":       lambda s: _safe_defs(_safe_dict(s.get("de_schema"))),
    "data":              lambda s: _safe_dict(s.get("de_data")),
    "schema.valid":      _schema_valid_facet,
}


# What each facet reads; copied into every rule card ('schema.properties' does not include nested properties or $defs).
FACET_DEFINITIONS = {
    "schema":            "snapshot['de_schema'] - the Data Editor's copy of the schema, whole document",
    "schema.properties": "snapshot['de_schema']['properties'] - ROOT level only; properties under $defs/definitions or nested objects are not included",
    "schema.required":   "the names listed in snapshot['de_schema']['required'] - ROOT level only",
    "schema.defs":       "snapshot['de_schema']['$defs'] merged with snapshot['de_schema']['definitions'] - ROOT level only, whichever keyword (or both) the schema uses",
    "data":              "snapshot['de_data'] - the Data Editor's data; key-based rules use its TOP-LEVEL keys only",
    "schema.valid":      "{'valid': snapshot['schema_valid_meta']} - whether the edited schema passes the meta-schema (tests/invariants/schema_validity.py); steps without that reading are skipped",
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


# Rows in de_gui that belong to the settings panel, not to the data ('textEditor.fontSize', 'guiEditor.maximumDepth', ...).
SETTINGS_ROW_PREFIXES = ("textEditor.", "guiEditor.")


def _parsed_text(snapshot: dict, text_key: str):
    """The parsed content of a Text View, or None when the view was not showing (nothing to compare then)."""
    text = snapshot.get(text_key)
    return text.get("parsed") if isinstance(text, dict) else None


# What an undo must bring back (undo_inverse template). A reader returns None when the store is not available in the snapshot.
RESTORE_STORES: dict[str, Callable[[dict], object]] = {
    "data":             lambda s: s.get("de_data"),
    "schema":           lambda s: s.get("de_schema"),
    "data Text View":   lambda s: _parsed_text(s, "de_text"),
    "schema Text View": lambda s: _parsed_text(s, "se_text"),
}


RESTORE_STORE_DEFINITIONS = {
    "data":             "snapshot['de_data']",
    "schema":           "snapshot['de_schema']",
    "data Text View":   "snapshot['de_text']['parsed'] (the Data Editor's Text View, parsed)",
    "schema Text View": "snapshot['se_text']['parsed'] (the Schema Editor's Text View, parsed)",
}


def _facet_has_signal(get_facet, pairs: list) -> bool:
    """False if the facet is empty in every snapshot; 'always unchanged' would then be true about nothing."""
    for p, c in pairs:
        if get_facet(p) or get_facet(c):
            return True
    return False