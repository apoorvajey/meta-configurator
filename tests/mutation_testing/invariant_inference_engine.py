"""Invariant inference engine. Finds candidate invariants in recorded state snapshots with a small template
library (generate and check, in the style of Daikon) and exports the promoted ones as executable rules.

Every template is one rule function (see RULES): rule(before, after, **params) -> list of (held, detail).
An empty list means the rule does not apply to that step. The same function counts how often a candidate held
during inference and checks the promoted rule during a mutation run (evaluate_inferred_rules), so a rule is
checked exactly as it was measured.

Templates:
    stability             a facet (or schema keyword path) is identical before and after
    change_count          DeepDiff reports exactly k changes for a facet
    correlation           if the top-level keys of A change, the keys of B change (or do not)
    path_key              a key added or removed in A is added or removed in B as well (or not)
    value_to_key          each key newly added to A is a top-level key of B afterwards (or not)
    key_containment       after the step, keys(A) are a subset of keys(B) (or not); any action
    format_agreement      the Text View content is in the format the selector shows (or not);
                          the transition variant only blames the action that introduced a mismatch
    text_store_agreement  the parsed Text View equals the editor's store (or not)
    order_stability       the order of a list-valued facet is unchanged (only checked when its set of
                          elements is unchanged; see ORDERED_FACETS)
    gui_validation_agreement
                          a root-level property's GUI error icon agrees with whether its value violates
                          its declared schema; any action

Undo/redo round trips are not a template of their own. _round_trip_pairs() builds (state before an undo/redo,
state after its matching redo/undo) pairs and runs them through stability and change_count under the
categories "undo_then_redo@<tab>" and "redo_then_undo@<tab>".

Each dataset is run through the templates independently. A candidate is PROMOTABLE only if every dataset has
enough observations and at least promotion_threshold support for the same direction (inference_config.json).
A dataset with too few observations makes it INSUFFICIENT_EVIDENCE.

Limit: requiring agreement across datasets filters out coincidences of one random walk, but not a real bug in
code that all datasets share. Such a bug looks like a genuine invariant in the traces, so every promoted rule
keeps its per-dataset evidence for review.

Outputs: console summary, reports/invariant_report.md (--report), reports/invariant_dashboard.html
(--dashboard), inferred_rules.json (--rules) and full rule text (--output-descriptions).

    python -m tests.mutation_testing.invariant_inference_engine
    python -m tests.mutation_testing.invariant_inference_engine --snapshots-dir DIR --datasets a,b --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, RefResolver, validate

from tests.mutation_testing.inference_config import InferenceConfig, load_config, with_overrides

try:
    from deepdiff import DeepDiff
except ImportError:
    raise ImportError("deepdiff is required: pip install deepdiff")


# One JSONL file per dataset, written by MutationLoop._append_snapshot(). _untagged.jsonl is ignored.
SNAPSHOTS_DIR = "invariant_miner_traces"
UNTAGGED_DATASET_NAME = "_untagged"
INFERRED_RULES_PATH = "inferred_rules.json"
REPORTS_DIR = "reports"  # default folder for generated reports and dashboards

# Numeric parameters live in inference_config.json (see inference_config.py).

SCHEMA_KEYWORDS = {
    "$schema", "$defs", "$id", "$ref", "title", "description",
    "type", "properties", "required", "additionalProperties",
    "definitions", "items", "allOf", "anyOf", "oneOf", "not",
    "if", "then", "else", "format", "enum", "const", "minimum",
    "maximum", "minLength", "maxLength", "pattern", "propertyNames",
    "multipleOf", "exclusiveMinimum", "exclusiveMaximum",
}


# Text View prefixes are checked first: 'rename-child-text-' also starts with 'rename-'.
_TEXT_ACTION_PREFIXES = (
    ("edit-text-",              "edit_text"),
    ("add-child-text-",         "add_child_text"),
    ("change-type-text-",       "change_type_text"),
    ("add-array-item-text-",    "add_array_item_text"),
    ("remove-array-item-text-", "remove_array_item_text"),
    ("remove-child-text-",      "remove_child_text"),
    ("rename-child-text-",      "rename_child_text"),
    ("clear-text-",             "clear_text"),
)


def _action_category(action: str) -> str:
    for prefix, cat in _TEXT_ACTION_PREFIXES:
        if action.startswith(prefix):
            return cat
    if any(action.startswith(p) for p in (
        "input-string-", "input-number-", "input-boolean-", "input-select-"
    )): return "edit_field"
    if action.startswith("expand-"):          return "expand"
    if action.startswith("collapse-"):        return "collapse"
    if action.startswith("rename-"):          return "rename"
    if action.startswith("remove-property-"): return "remove"
    if action.startswith("add-property-"):    return "add_property"
    if action == "add-item-required":         return "add_item_required"
    if action.startswith("add-item-"):        return "add_item"
    if action.startswith("mode-active-"):     return "mode_switch"
    if action in ("undo", "redo", "schema_undo", "schema_redo"): return "undo_redo"
    if action == "format-selector":           return "format_toggle"
    return "other"


# Membership rule per category, shown on rule cards.
ACTION_CATEGORY_DESCRIPTIONS = {
    "edit_text":              'action starts with "edit-text-"',
    "add_child_text":         'action starts with "add-child-text-"',
    "change_type_text":       'action starts with "change-type-text-"',
    "add_array_item_text":    'action starts with "add-array-item-text-"',
    "remove_array_item_text": 'action starts with "remove-array-item-text-"',
    "remove_child_text":      'action starts with "remove-child-text-"',
    "rename_child_text":      'action starts with "rename-child-text-"',
    "clear_text":             'action starts with "clear-text-"',
    "edit_field":             'action starts with "input-string-", "input-number-", "input-boolean-" or "input-select-"',
    "expand":                 'action starts with "expand-"',
    "collapse":               'action starts with "collapse-"',
    "rename":                 'action starts with "rename-" (Text View renames excluded)',
    "remove":                 'action starts with "remove-property-"',
    "add_property":           'action starts with "add-property-"',
    "add_item_required":      'action is exactly "add-item-required"',
    "add_item":               'action starts with "add-item-" (except "add-item-required")',
    "mode_switch":            'action starts with "mode-active-" (tab switch)',
    "undo_redo":              'action is "undo", "redo", "schema_undo" or "schema_redo"',
    "format_toggle":          'action is exactly "format-selector"',
    "undo_then_redo":         'an undo immediately followed by its matching redo (same run, adjacent steps; see _round_trip_pairs)',
    "redo_then_undo":         'a redo immediately followed by its matching undo (same run, adjacent steps; see _round_trip_pairs)',
    "other":                  'any action not matched by another category',
}

# Text View actions name their editor; every other action belongs to the tab showing when it was chosen (the previous snapshot).
_TEXT_EDITOR_MARKERS = {"dataEditor": "data", "schemaEditor": "schema"}


def _active_tab(snapshot: dict | None) -> str | None:
    """Tab showing when the snapshot was taken: exactly one of de_gui and se_gui is set.
    Action names cannot tell, since every mode switch is logged as 'mode-active-false'.
    """
    snapshot = snapshot or {}
    if snapshot.get("de_gui") is not None:
        return "data"
    if snapshot.get("se_gui") is not None:
        return "schema"
    return None


def _action_tab(prev: dict | None, curr: dict) -> str:
    action = curr.get("action", "")
    for marker, tab in _TEXT_EDITOR_MARKERS.items():
        if marker in action:
            return tab
    return _active_tab(prev) or "unknown"


def _tabbed_category(prev: dict | None, curr: dict) -> str:
    """Action category plus its tab, e.g. 'add_item@schema'. Used in inference and in checking so actions are classified identically."""
    return f"{_action_category(curr.get('action', ''))}@{_action_tab(prev, curr)}"


_UNDO_ACTIONS = {"undo", "schema_undo"}
_REDO_ACTIONS = {"redo", "schema_redo"}


def _round_trip_pairs(snapshots: list[dict]) -> list[tuple[dict, dict, str]]:
    """Immediate undo-then-redo or redo-then-undo triples inside one run (three consecutive steps).
    Returns (state before the pair, state after it, tag); the undone or redone state in between is skipped.
    A round trip with another action in between is not detected.
    """
    pairs: list[tuple[dict, dict, str]] = []
    for i in range(1, len(snapshots) - 1):
        a, b, c = snapshots[i - 1], snapshots[i], snapshots[i + 1]
        run = a.get("run_id")
        if run is None or b.get("run_id") != run or c.get("run_id") != run:
            continue
        if not (b.get("step", 0) > a.get("step", -1) and c.get("step", 0) > b.get("step", -1)):
            continue
        b_action, c_action = b.get("action", ""), c.get("action", "")
        if b_action in _UNDO_ACTIONS and c_action in _REDO_ACTIONS:
            pairs.append((a, c, "undo_then_redo"))
        elif b_action in _REDO_ACTIONS and c_action in _UNDO_ACTIONS:
            pairs.append((a, c, "redo_then_undo"))
    return pairs


def _safe_dict(value) -> dict:
    """Non-dicts become {}: a root-level change-type on the schema tab can collapse the schema to a scalar."""
    return value if isinstance(value, dict) else {}


def _safe_list(value) -> list:
    return value if isinstance(value, list) else []


def _safe_defs(schema: dict) -> dict:
    """$defs and definitions merged into one facet; the schemas use either keyword."""
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
    "schema.defs":       "snapshot['de_schema']['$defs'] merged with snapshot['de_schema']['definitions'] - ROOT level only",
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

# Containment is directional; ('data', 'schema.required') is left out because data nearly always has more keys.
# schema.defs is left out too: a def name is not expected to match a property or data key.
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

# List-valued facets where order matters. FACETS turns everything into dicts or key sets, which drops order.
ORDERED_FACETS: dict[str, Callable[[dict], list]] = {
    "schema.required": lambda s: _safe_list(_safe_dict(s.get("de_schema")).get("required")),
}


def _value_violates_schema(value, prop_schema, root_schema=None) -> bool:
    """Copy of the function of the same name in invariants_core, so this offline script does not import the
    Playwright-facing test module. Keep the two in sync.
    """
    if not prop_schema:
        return False
    try:
        if root_schema:
            resolver = RefResolver.from_schema(root_schema)
            validator = Draft202012Validator(prop_schema, resolver=resolver)
            return not validator.is_valid(value)
        validate(instance=value, schema=prop_schema)
        return False
    except Exception:
        return False


def _facet_has_signal(get_facet, pairs: list) -> bool:
    """False if the facet is empty in every snapshot; 'always unchanged' would then be true about nothing."""
    for p, c in pairs:
        if get_facet(p) or get_facet(c):
            return True
    return False


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


# Rule signature: rule(before, after, **params) -> list of (held, detail).
# An empty list means the guard is false for this step.
# A rule promoted in the negative direction passes when held is False.

Observation = tuple[bool, dict]


def _short(value: Any, n: int = 100) -> str:
    return "<missing>" if value is _MISSING else str(value)[:n]


def _keys(obj: Any) -> set:
    return set(obj.keys()) if isinstance(obj, dict) else set()


def rule_stability(before: dict, after: dict, facet: str | None = None,
                   path: list[str] | None = None) -> list[Observation]:
    if path is not None:
        a, b = schema_path_value(before, path), schema_path_value(after, path)
        if a is _MISSING and b is _MISSING:
            return []
    else:
        a, b = FACETS[facet](before), FACETS[facet](after)
    same = a == b
    return [(same, {} if same else {"before": _short(a), "after": _short(b)})]


def rule_order_stability(before: dict, after: dict, facet: str) -> list[Observation]:
    """Compares the raw list order of a facet. Only applies when the set of elements is unchanged."""
    a, b = ORDERED_FACETS[facet](before), ORDERED_FACETS[facet](after)
    if not a or set(a) != set(b):
        return []
    same_order = a == b
    return [(same_order, {} if same_order else {"before": a[:10], "after": b[:10]})]


def change_count(before_value: Any, after_value: Any) -> int:
    """Number of DeepDiff change entries (-1 if DeepDiff fails)."""
    if before_value == after_value:
        return 0
    try:
        diff = DeepDiff(before_value, after_value, ignore_order=True)
        return sum(len(v) for v in diff.values() if isinstance(v, (dict, set, list)))
    except Exception:
        return -1


def rule_change_count(before: dict, after: dict, facet: str, expected: int) -> list[Observation]:
    d = change_count(FACETS[facet](before), FACETS[facet](after))
    if d < 0:
        return []
    return [(d == expected, {"change_count": d})]


def rule_correlation(before: dict, after: dict, a: str, b: str) -> list[Observation]:
    ka0, ka1 = _keys(FACETS[a](before)), _keys(FACETS[a](after))
    if ka0 == ka1:
        return []
    kb0, kb1 = _keys(FACETS[b](before)), _keys(FACETS[b](after))
    return [(kb0 != kb1, {
        f"{a} +": sorted(ka1 - ka0)[:5], f"{a} -": sorted(ka0 - ka1)[:5],
        f"{b} +": sorted(kb1 - kb0)[:5], f"{b} -": sorted(kb0 - kb1)[:5],
    })]


def rule_path_key(before: dict, after: dict, a: str, b: str) -> list[Observation]:
    ka0, ka1 = _keys(FACETS[a](before)), _keys(FACETS[a](after))
    a_add, a_rem = ka1 - ka0, ka0 - ka1
    if not a_add and not a_rem:
        return []
    kb0, kb1 = _keys(FACETS[b](before)), _keys(FACETS[b](after))
    b_add, b_rem = kb1 - kb0, kb0 - kb1
    return [(bool((a_add & b_add) or (a_rem & b_rem)), {
        f"{a} +": sorted(a_add)[:5], f"{a} -": sorted(a_rem)[:5],
        f"{b} +": sorted(b_add)[:5], f"{b} -": sorted(b_rem)[:5],
    })]


def rule_value_to_key(before: dict, after: dict, a: str, b: str) -> list[Observation]:
    added = _keys(FACETS[a](after)) - _keys(FACETS[a](before))
    kb1 = _keys(FACETS[b](after))
    return [(key in kb1, {"key": key}) for key in sorted(added)]


def rule_key_containment(before: dict, after: dict, a: str, b: str) -> list[Observation]:
    ka = _keys(FACETS[a](after))
    if not ka:
        return []
    missing = sorted(ka - _keys(FACETS[b](after)))
    return [(not missing, {} if not missing else {"missing": missing[:5]})]


_ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$"
)


def _normalise_timestamps(value: Any) -> Any:
    """Rewrite ISO-8601 timestamps to one UTC form. Needed for YAML text only: the capture parses YAML into datetimes,
    which serialise differently from the text (same instant, different string).
    """
    from datetime import datetime, timezone
    if isinstance(value, dict):
        return {k: _normalise_timestamps(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise_timestamps(v) for v in value]
    if isinstance(value, str) and _ISO_DATETIME.match(value):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else dt.isoformat()
    return value


def rule_text_store_agreement(before: dict, after: dict, view: str) -> list[Observation]:
    text_key, store_key = TEXT_VIEWS[view]
    text = after.get(text_key) or {}
    parsed = text.get("parsed")
    if parsed is None:
        return []
    store = after.get(store_key)
    same = parsed == store
    if not same and text.get("format") == "yaml":
        same = _normalise_timestamps(parsed) == _normalise_timestamps(store)
    if same:
        return [(True, {})]
    try:
        diff = DeepDiff(store, parsed, ignore_order=True)
        first = next((p for v in diff.values() if isinstance(v, (dict, set, list)) for p in v), "")
    except Exception:
        first = "?"
    return [(False, {"format": text.get("format"), "first_diff": str(first)[:120]})]


def detect_text_format(raw: str | None) -> str | None:
    """Format the text is actually in: 'json', 'yaml' or None if it cannot be judged.
    Text that looks like JSON but fails json.loads is a half-edited document, not YAML.
    """
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
        return "json" if isinstance(value, (dict, list)) else None
    except ValueError:
        pass
    if raw.lstrip().startswith(("{", "[")):
        return None
    try:
        import yaml
        value = yaml.safe_load(raw)
    except ImportError:
        raise ImportError("PyYAML is required for format_agreement: pip install pyyaml")
    except Exception:
        return None
    return "yaml" if isinstance(value, (dict, list)) else None


def _label_and_content(snapshot: dict, view: str) -> tuple[str | None, str | None]:
    text = snapshot.get(TEXT_VIEWS[view][0]) or {}
    return text.get("format"), detect_text_format(text.get("raw"))


def rule_format_agreement(before: dict, after: dict, view: str,
                          transition: bool = False) -> list[Observation]:
    """After the step, the detected format of the Text View content equals the format selector's label.

    With transition=True the rule only applies if label and content also agreed before the step,
    so only the action that introduced a mismatch is blamed.
    """
    label, content = _label_and_content(after, view)
    if label is None or content is None:
        return []
    if transition:
        b_label, b_content = _label_and_content(before, view)
        if b_label is None or b_content is None or b_label != b_content:
            return []
    if label == content:
        return [(True, {})]
    return [(False, {"label": label, "content": content})]


def rule_gui_validation_agreement(before: dict, after: dict, transition: bool = False) -> list[Observation]:
    """Whether the GUI error icon of a root-level property agrees with whether its value violates the declared
    schema (same check as INV-GUI-ERR-01). Nested properties are not covered.
    transition=True also requires that icon and validity agreed before the step, so the action that introduced
    the mismatch is the one blamed.
    """
    gui = _safe_dict(after.get("de_gui"))
    data = _safe_dict(after.get("de_data"))
    schema = _safe_dict(after.get("de_schema"))
    props = _safe_dict(schema.get("properties"))

    obs: list[Observation] = []
    for key, facts in gui.items():
        if "." in key or "[" in key or not isinstance(facts, dict) or "icon_shown" not in facts:
            continue  # nested path or incomplete row
        if key not in props:
            continue  # not a declared root property
        icon_shown = bool(facts["icon_shown"])
        actually_invalid = _value_violates_schema(data.get(key), props[key], schema)

        if transition:
            before_gui = _safe_dict(_safe_dict(before.get("de_gui")).get(key))
            before_data = _safe_dict(before.get("de_data"))
            before_schema = _safe_dict(before.get("de_schema"))
            before_props = _safe_dict(before_schema.get("properties"))
            if key not in before_props or "icon_shown" not in before_gui:
                continue
            before_icon = bool(before_gui["icon_shown"])
            before_invalid = _value_violates_schema(before_data.get(key), before_props[key], before_schema)
            if before_icon != before_invalid:
                continue  # already mismatched before this step

        agree = (icon_shown == actually_invalid)
        obs.append((agree, {} if agree else
                    {"key": key, "icon_shown": icon_shown, "actually_invalid": actually_invalid}))
    return obs


RULES: dict[str, Callable[..., list[Observation]]] = {
    "stability":       rule_stability,
    "change_count":    rule_change_count,
    "correlation":     rule_correlation,
    "path_key":        rule_path_key,
    "value_to_key":    rule_value_to_key,
    "key_containment": rule_key_containment,
    "text_store_agreement": rule_text_store_agreement,
    "format_agreement": rule_format_agreement,
    "order_stability":  rule_order_stability,
    "gui_validation_agreement": rule_gui_validation_agreement,
}


def _stability_target(params: dict) -> str:
    if "path" in params:
        return "schema." + ".".join(params["path"])
    return params["facet"]


def rule_statement(template: str, category: str, params: dict, positive: bool) -> str:
    """One-line statement of a rule."""
    prefix = "" if category == "*" else f"after {category}: "
    p = params
    if template == "stability":
        s = f"{_stability_target(p)} is unchanged" if positive else f"{_stability_target(p)} changes"
    elif template == "change_count":
        s = (f"{p['facet']} has exactly {p.get('expected', 'k')} DeepDiff change(s)" if positive
             else f"{p['facet']} does not have exactly {p.get('expected', 'k')} DeepDiff change(s)")
    elif template == "correlation":
        s = (f"if the top-level keys of {p['a']} change, the top-level keys of {p['b']} change too" if positive
             else f"if the top-level keys of {p['a']} change, the top-level keys of {p['b']} stay the same")
    elif template == "path_key":
        s = (f"a key added to / removed from {p['a']} is added to / removed from {p['b']} too" if positive
             else f"a key added to / removed from {p['a']} is not added to / removed from {p['b']}")
    elif template == "value_to_key":
        s = (f"a key newly added to {p['a']} is a key of {p['b']} afterwards" if positive
             else f"a key newly added to {p['a']} is not a key of {p['b']} afterwards")
    elif template == "key_containment":
        s = (f"every key of {p['a']} is also a key of {p['b']}" if positive
             else f"some key of {p['a']} is not a key of {p['b']}")
    elif template == "order_stability":
        s = (f"the order of {p['facet']} is unchanged" if positive
             else f"the order of {p['facet']} changes even though its set of elements does not")
    elif template == "gui_validation_agreement":
        tag = " (newly introduced)" if p.get("transition") else ""
        s = (f"the GUI error icon agrees with actual schema validity{tag}" if positive
             else f"the GUI error icon disagrees with actual schema validity{tag}")
    elif template == "format_agreement":
        label = f"{p['view'].capitalize()} Editor"
        if p.get("transition"):
            s = (f"if the {label}'s Text View content matched its format selector before the step, it still matches after"
                 if positive else
                 f"the {label}'s Text View content stops matching its format selector")
        else:
            s = (f"the {label}'s Text View content is in the format its format selector shows" if positive
                 else f"the {label}'s Text View content is not in the format its format selector shows")
    elif template == "text_store_agreement":
        label = f"{p['view'].capitalize()} Editor"
        s = (f"the {label}'s Text View shows the same content as its store" if positive
             else f"the {label}'s Text View does not match its store")
    else:
        s = f"{template} {p}"
    return prefix + s


def describe_rule(rule: dict) -> dict[str, str]:
    """Full description of a rule: trigger, when it applies, what it asserts and what each name refers to."""
    t, cat, p = rule["template"], rule["category"], rule["params"]
    pos = rule["direction"] == "positive"
    base, _, tab = cat.partition("@")

    if cat == "*":
        trigger = "every step, whatever the action and tab"
    else:
        tab_text = {"data": "the Data Editor", "schema": "the Schema Editor"}.get(tab, f"tab '{tab}'")
        trigger = (f"steps whose action is in category '{base}' ({ACTION_CATEGORY_DESCRIPTIONS.get(base, base)}), "
                   f"performed while {tab_text} was active - the tab showing in the snapshot BEFORE the step; "
                   f"Text View actions use the editor named in the action")

    facets: list[str] = []
    definitions = None
    if t == "stability":
        if "path" in p:
            target = "snapshot['de_schema']" + "".join(f"[{k!r}]" for k in p["path"])
            applies = f"{target} exists before or after the step"
            asserts = (f"{target} has the same value before and after the step" if pos
                       else f"{target} has a different value after the step")
        else:
            facets = [p["facet"]]
            applies = "always"
            asserts = (f"{p['facet']} is deep-equal before and after the step" if pos
                       else f"{p['facet']} differs after the step")
    elif t == "change_count":
        facets = [p["facet"]]
        applies = "always"
        k = p.get("expected", "k")
        asserts = (f"DeepDiff(before {p['facet']}, after {p['facet']}, ignore_order=True) reports exactly {k} change entries"
                   if pos else f"DeepDiff reports a number of change entries other than {k} for {p['facet']}")
    elif t == "correlation":
        facets = [p["a"], p["b"]]
        applies = f"the set of top-level keys of {p['a']} differs between before and after"
        asserts = (f"the set of top-level keys of {p['b']} also differs between before and after" if pos
                   else f"the set of top-level keys of {p['b']} is identical before and after")
    elif t == "path_key":
        facets = [p["a"], p["b"]]
        applies = f"at least one top-level key was added to or removed from {p['a']}"
        asserts = (f"at least one of those keys was added to {p['b']} (if added) or removed from {p['b']} (if removed)"
                   if pos else f"none of those keys was added to / removed from {p['b']} in the same way")
    elif t == "value_to_key":
        facets = [p["a"], p["b"]]
        applies = f"a top-level key was newly added to {p['a']} - checked once per added key"
        asserts = (f"that key is a top-level key of {p['b']} after the step" if pos
                   else f"that key is not a top-level key of {p['b']} after the step")
    elif t == "key_containment":
        facets = [p["a"], p["b"]]
        applies = f"{p['a']} is non-empty after the step"
        asserts = (f"every top-level key of {p['a']} is a top-level key of {p['b']} after the step" if pos
                   else f"at least one top-level key of {p['a']} is not a top-level key of {p['b']} after the step")
    elif t == "order_stability":
        facets = [p["facet"]]
        applies = f"the set of {p['facet']} is unchanged (its order might still differ)"
        asserts = (f"the order of {p['facet']} is also unchanged" if pos
                   else f"the ORDER of {p['facet']} differs even though its set of elements does not")
    elif t == "gui_validation_agreement":
        applies = ("a root-level property has a GUI error-icon reading and a declared schema"
                   + (" that agreed before this step" if p.get("transition") else "")
                   + ", checked after the step")
        asserts = ("the icon is shown exactly when the value violates its declared schema"
                   if pos else
                   "the icon is shown when the value is valid, or hidden when it violates its declared schema")
        definitions = ("icon_shown = a validation-error icon is rendered in the property's GUI row; "
                       "actually violates = _value_violates_schema (Draft 2020-12 validator) against the "
                       "property's declared schema, as in INV-GUI-ERR-01. Root-level properties only.")
    elif t == "text_store_agreement":
        tk, sk = TEXT_VIEWS[p["view"]]
        applies = f"snapshot['{tk}']['parsed'] is present after the step (that editor's Text View was showing and parsed)"
        asserts = (f"snapshot['{tk}']['parsed'] deep-equals snapshot['{sk}'] (YAML: ISO timestamps compared as instants)"
                   if pos else f"snapshot['{tk}']['parsed'] differs from snapshot['{sk}']")
    elif t == "format_agreement":
        tk = TEXT_VIEWS[p["view"]][0]
        judgeable = (f"snapshot['{tk}']['format'] is set and snapshot['{tk}']['raw'] has a detectable format")
        if p.get("transition"):
            applies = (f"BEFORE the step: {judgeable} and the two match; AFTER the step: {judgeable}")
        else:
            applies = f"after the step, {judgeable}"
        asserts = (f"after the step, the detected format of snapshot['{tk}']['raw'] equals snapshot['{tk}']['format']"
                   if pos else
                   f"after the step, the detected format of snapshot['{tk}']['raw'] differs from snapshot['{tk}']['format']")
        definitions = (f"snapshot['{tk}']['format'] = the label shown by the format selector ('json'/'yaml'); "
                       f"detected format = 'json' if json.loads gives an object/array, 'yaml' if yaml.safe_load gives "
                       f"a mapping/list and the text does not start with '{{' or '[', otherwise not judgeable "
                       f"(empty, scalar, or half-edited text is skipped)")
    else:
        applies, asserts = "?", "?"

    if definitions is None:
        definitions = "; ".join(f"{f} = {FACET_DEFINITIONS[f]}" for f in dict.fromkeys(facets))
    return {"trigger": trigger, "applies_when": applies, "asserts": asserts,
            "definitions": definitions or "-"}


_RULES_CACHE: dict[str, list[dict]] = {}


def load_inferred_rules(path: str = INFERRED_RULES_PATH) -> list[dict]:
    """Rules from inferred_rules.json (cached per path); [] if the file is missing."""
    if path not in _RULES_CACHE:
        p = Path(path)
        _RULES_CACHE[path] = json.loads(p.read_text(encoding="utf-8"))["rules"] if p.exists() else []
    return _RULES_CACHE[path]


def evaluate_inferred_rules(prev: dict | None, curr: dict | None, action: str,
                            path: str = INFERRED_RULES_PATH) -> list:
    """Check every promoted rule whose trigger matches this step.
    prev must be the full previous snapshot, since the tab is read from it; without it only '*' rules run.
    """
    from tests.invariants.invariants_core import InvariantResult

    prev, curr = prev or {}, dict(curr or {})
    curr.setdefault("action", action)
    category = _tabbed_category(prev, {"action": action})

    results = []
    for rule in load_inferred_rules(path):
        if rule["category"] not in ("*", category):
            continue
        observations = RULES[rule["template"]](prev, curr, **rule["params"])
        if not observations:
            continue
        expect = rule["direction"] == "positive"
        violations = [d for held, d in observations if held != expect]
        passed = not violations
        name = f"{rule['id']} {rule['statement']}"
        if passed:
            message = f"{rule['statement']} ✓"
        else:
            message = f"{rule['statement']} - violated ({len(violations)}/{len(observations)} observation(s))"
            if rule.get("min_support", 1.0) < 1.0:
                message += (f" [promoted at {rule['min_support']:.0%} minimum support: "
                            f"known exceptions exist in the traces]")
        results.append(InvariantResult(
            name, passed, message,
            {} if passed else {"violations": violations[:3], "asserts": rule["card"]["asserts"]},
            evidence=[f"rule={rule['id']}", f"action={action}", f"category={category}",
                      f"asserts: {rule['card']['asserts']}"],
        ))
    return results


@dataclass
class Finding:
    """One template's result for one candidate in one dataset."""
    template: str
    category: str
    params: dict  # identifies the candidate with template and category
    held: int  # observations where the positive statement held
    total: int  # applicable observations
    description: str
    expected: int | None = None  # change_count only: k in this dataset
    fail_examples: list[dict] = field(default_factory=list)  # where the positive statement failed
    hold_examples: list[dict] = field(default_factory=list)  # where it held (failures of a negative rule)
    dataset: str = ""

    @property
    def verdict(self) -> str:
        if self.held == self.total:
            return "CONFIRMED"
        if self.held == 0:
            return "NEVER_TRUE"
        return "INCONSISTENT"

    @property
    def confidence(self) -> str:
        return f"{self.held}/{self.total}"


@dataclass
class PromotionCandidate:
    """One candidate merged across datasets."""
    template: str
    category: str
    params: dict
    verdict: str  # PROMOTABLE | BELOW_THRESHOLD | CONTRADICTORY_ACROSS_DATASETS | INSUFFICIENT_EVIDENCE
    per_dataset: dict[str, dict]
    direction: str = "positive"
    expected: int | None = None
    nl_description: str = ""
    rule: dict = field(default_factory=dict)


def _params_label(template: str, params: dict) -> str:
    if template == "stability":
        return _stability_target(params)
    if template in ("change_count", "order_stability"):
        return params["facet"]
    if template == "text_store_agreement":
        return f"text({params['view']}) vs store({params['view']})"
    if template == "format_agreement":
        return f"format label vs content ({params['view']}{', transition' if params.get('transition') else ''})"
    if template == "gui_validation_agreement":
        return "GUI icon vs schema validity" + (" (transition)" if params.get("transition") else "")
    return f"{params['a']} | {params['b']}"


class InvariantInferenceEngine:

    def __init__(
        self,
        snapshots_dir: str = SNAPSHOTS_DIR,
        min_observations: int | None = None,
        config: InferenceConfig | None = None,
    ):
        # min_observations stays as an optional per-call override.
        self.cfg = with_overrides(config if config is not None else load_config(),
                                  min_observations=min_observations)
        self.dir = Path(snapshots_dir)
        self.snapshots_by_dataset: dict[str, list[dict]] = {}
        self.findings: list[Finding] = []
        self.promotions: list[PromotionCandidate] = []

    @property
    def min_observations(self) -> int:
        return self.cfg.min_observations

    def discover_datasets(self) -> list[str]:
        """Every {dataset}.jsonl in the snapshots directory except _untagged.jsonl, which cannot take part in a per-dataset check."""
        if not self.dir.exists():
            raise FileNotFoundError(f"Snapshots directory not found: {self.dir}")
        return sorted(
            p.stem for p in self.dir.glob("*.jsonl")
            if p.stem != UNTAGGED_DATASET_NAME
        )

    def load_dataset(self, dataset: str) -> list[dict]:
        """Read one dataset's JSONL file. A bad line (only the last can be cut off by a killed run) is skipped with a warning."""
        path = self.dir / f"{dataset}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"No snapshot file for dataset '{dataset}': {path}")

        snapshots: list[dict] = []
        skipped = 0
        line_num = 0
        with path.open(encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    snapshots.append(json.loads(line))
                except json.JSONDecodeError as e:
                    skipped += 1
                    print(f"[Engine] {path.name}:{line_num} could not be parsed ({e}) "
                          f"- skipping (only ever expected on a killed run's last line)")

        if skipped:
            print(f"[Engine] {dataset}: skipped {skipped} malformed line(s) out of {line_num}")
        print(f"[Engine] Loaded {len(snapshots)} snapshots for dataset '{dataset}' from {path}")
        return snapshots

    def load_all(self, datasets: list[str] | None = None) -> dict[str, int]:
        """Load every discovered dataset (or a subset). Returns {dataset: snapshot count}."""
        datasets = datasets if datasets is not None else self.discover_datasets()
        if not datasets:
            raise FileNotFoundError(
                f"No dataset .jsonl files found in {self.dir} "
                f"(besides possibly {UNTAGGED_DATASET_NAME}.jsonl, which is excluded)"
            )
        counts = {}
        for ds in datasets:
            self.snapshots_by_dataset[ds] = self.load_dataset(ds)
            counts[ds] = len(self.snapshots_by_dataset[ds])
        return counts

    def run(self, on_progress=None) -> list[Finding]:
        """Run every template per dataset, deduplicate, tag findings with their dataset and compute the cross-dataset verdicts.

        on_progress(dataset, dataset_index, dataset_count, category_index, category_count) is called once per action category.
        """
        all_findings: list[Finding] = []
        dataset_count = len(self.snapshots_by_dataset)

        for dataset_index, (dataset, snapshots) in enumerate(self.snapshots_by_dataset.items(), 1):
            self.findings = []

            # Pair consecutive snapshots of the same run only.
            clean_pairs = [
                (p, c) for p, c in zip(snapshots[:-1], snapshots[1:])
                if p.get("run_id") is not None
                   and p.get("run_id") == c.get("run_id")
                   and c.get("step", 0) > p.get("step", -1)
            ]

            by_cat: dict[str, list] = defaultdict(list)
            for p, c in clean_pairs:
                by_cat[_tabbed_category(p, c)].append((p, c))

            for cat_index, (cat, cat_pairs) in enumerate(by_cat.items(), 1):
                if on_progress:
                    on_progress(dataset, dataset_index, dataset_count, cat_index, len(by_cat))
                if len(cat_pairs) < self.min_observations:
                    continue
                self._run_stability(cat, cat_pairs)
                self._run_change_count(cat, cat_pairs)
                self._run_order_stability(cat, cat_pairs)
                for a, b in CORRELATION_PAIRS:
                    for template in ("correlation", "path_key", "value_to_key"):
                        self._run_rule(template, cat, cat_pairs, {"a": a, "b": b}, self.cfg.min_rule_observations)
                for view in TEXT_VIEWS:
                    self._run_rule("text_store_agreement", cat, cat_pairs, {"view": view}, self.min_observations)
                    self._run_rule("format_agreement", cat, cat_pairs,
                                   {"view": view, "transition": True}, self.min_observations)
                self._run_rule("gui_validation_agreement", cat, cat_pairs,
                               {"transition": True}, self.min_observations)

            # undo/redo round trips: same templates on (state before the pair, state after its counterpart)
            rt_by_cat: dict[str, list] = defaultdict(list)
            for rt_before, rt_after, direction in _round_trip_pairs(snapshots):
                tab = _active_tab(rt_before) or "unknown"
                rt_by_cat[f"{direction}@{tab}"].append((rt_before, rt_after))
            for cat, cat_pairs in rt_by_cat.items():
                if len(cat_pairs) < self.min_observations:
                    continue
                self._run_stability(cat, cat_pairs)
                self._run_change_count(cat, cat_pairs)

            # State rules do not depend on the action, so they also run once over every pair.
            if len(clean_pairs) >= self.min_observations:
                for a, b in CONTAINMENT_PAIRS:
                    self._run_rule("key_containment", "*", clean_pairs, {"a": a, "b": b}, self.cfg.min_rule_observations)
                for view in TEXT_VIEWS:
                    self._run_rule("text_store_agreement", "*", clean_pairs, {"view": view}, self.min_observations)
                    self._run_rule("format_agreement", "*", clean_pairs,
                                   {"view": view, "transition": False}, self.min_observations)
                self._run_rule("gui_validation_agreement", "*", clean_pairs,
                               {"transition": False}, self.min_observations)

            self._deduplicate()
            for f in self.findings:
                f.dataset = dataset
            all_findings.extend(self.findings)

        self.findings = all_findings
        self._compute_promotions()
        return self.findings

    def _run_rule(self, template: str, cat: str, pairs: list, params: dict,
                  min_obs: int, record_params: dict | None = None,
                  expected: int | None = None) -> None:
        """Apply RULES[template] to every pair and record how often the positive statement held.
        record_params is the candidate identity when it differs from params (change_count: the facet).
        """
        rule = RULES[template]
        held = total = 0
        fail_ex: list[dict] = []
        hold_ex: list[dict] = []
        for prev, curr in pairs:
            for h, detail in rule(prev, curr, **params):
                total += 1
                ex = {"run_id": curr.get("run_id"), "step": curr.get("step"),
                      "action": curr.get("action"), **detail}
                if h:
                    held += 1
                    if len(hold_ex) < self.cfg.max_examples:
                        hold_ex.append(ex)
                elif len(fail_ex) < self.cfg.max_examples:
                    fail_ex.append(ex)

        if total < min_obs:
            return
        identity = record_params if record_params is not None else params
        self.findings.append(Finding(
            template=template,
            category=cat,
            params=identity,
            held=held,
            total=total,
            description=rule_statement(template, cat, params, positive=True),
            expected=expected,
            fail_examples=fail_ex,
            hold_examples=hold_ex,
        ))

    def _run_stability(self, cat: str, pairs: list) -> None:
        for facet, get_facet in FACETS.items():
            if _facet_has_signal(get_facet, pairs):  # skip facets that are empty everywhere
                self._run_rule("stability", cat, pairs, {"facet": facet}, self.min_observations)

        paths: set[tuple[str, ...]] = set()
        for prev, curr in pairs:
            paths |= _enumerate_schema_keyword_paths(prev.get("de_schema"))
            paths |= _enumerate_schema_keyword_paths(curr.get("de_schema"))
        for keys in sorted(paths):
            self._run_rule("stability", cat, pairs, {"path": list(keys)}, self.min_observations)

    def _run_order_stability(self, cat: str, pairs: list) -> None:
        for facet, get_facet in ORDERED_FACETS.items():
            if _facet_has_signal(get_facet, pairs):
                self._run_rule("order_stability", cat, pairs, {"facet": facet}, self.min_observations)

    def _run_change_count(self, cat: str, pairs: list) -> None:
        """k is the most common change count in the dataset (k == 0 is the stability rule, so it is skipped).
        Reported only if k covers at least cfg.change_count_min_share of the observations.
        """
        for facet, get_facet in FACETS.items():
            if not _facet_has_signal(get_facet, pairs):
                continue
            counts = [d for d in (change_count(get_facet(p), get_facet(c)) for p, c in pairs) if d >= 0]
            if len(counts) < self.min_observations:
                continue
            k, freq = Counter(counts).most_common(1)[0]
            if k == 0 or freq < self.cfg.change_count_min_share * len(counts):
                continue
            self._run_rule("change_count", cat, pairs, {"facet": facet, "expected": k},
                           self.min_observations, record_params={"facet": facet}, expected=k)

    def _deduplicate(self) -> None:
        """Drop findings implied by another confirmed finding of the same category and dataset: stability of a schema path
        when the whole schema is stable, and never-true correlation findings whose target facet never changes.
        """
        whole_stable = {
            (f.category, f.params["facet"]) for f in self.findings
            if f.template == "stability" and "facet" in f.params and f.verdict == "CONFIRMED"
        }
        keep = []
        for f in self.findings:
            p = f.params
            if f.template == "stability" and p.get("path") in (["properties"], ["required"]):
                continue
            if f.template == "stability" and (f.category, "schema") in whole_stable:
                if "path" in p or p.get("facet") in ("schema.properties", "schema.required"):
                    continue
            if (f.template in ("correlation", "path_key", "value_to_key") and f.verdict == "NEVER_TRUE"
                    and (f.category, p["b"].split(".")[0]) in whole_stable):
                continue
            keep.append(f)
        removed = len(self.findings) - len(keep)
        self.findings = keep
        if removed:
            print(f"[Engine] Deduplicated: removed {removed} redundant findings")

    @staticmethod
    def _group_key(f: Finding) -> tuple:
        """Candidate identity, independent of dataset and verdict."""
        return (f.template, f.category, json.dumps(f.params, sort_keys=True))

    @staticmethod
    def _direction_and_support(held: int, total: int) -> tuple[str, float]:
        """Majority direction of one dataset and its support (ties count as positive)."""
        if total == 0:
            return "positive", 0.0
        pct = held / total
        return ("positive", pct) if pct >= 0.5 else ("negative", 1.0 - pct)

    def _compute_promotions(self) -> None:
        """Decide a verdict per candidate:

          PROMOTABLE                     every dataset has enough observations and clears promotion_threshold in the same
                                         direction (for change_count, also the same k)
          BELOW_THRESHOLD                enough data everywhere, but not every dataset clears the threshold
          CONTRADICTORY_ACROSS_DATASETS  datasets that clear the threshold disagree on direction (or on k)
          INSUFFICIENT_EVIDENCE          a dataset is missing or has too few observations

        Only datasets that clear the threshold vote on direction: one near 50% flips on a single observation.
        """
        all_datasets = sorted(self.snapshots_by_dataset.keys())
        groups: dict[tuple, list[Finding]] = defaultdict(list)
        for f in self.findings:
            groups[self._group_key(f)].append(f)

        self.promotions = []
        for key, group in groups.items():
            per_dataset: dict[str, dict] = {}
            for f in group:
                direction, support = self._direction_and_support(f.held, f.total)
                per_dataset[f.dataset] = {
                    "n": f.held, "m": f.total, "direction": direction, "support": support,
                    "verdict": f.verdict, "expected": f.expected,
                    "fail_examples": f.fail_examples, "hold_examples": f.hold_examples,
                }

            missing = [d for d in all_datasets if d not in per_dataset]
            thin = [d for d, v in per_dataset.items() if v["m"] < self.cfg.min_observations_for_promotion]
            constants = {v["expected"] for v in per_dataset.values()}

            direction = "positive"
            if missing or thin:
                verdict = "INSUFFICIENT_EVIDENCE"
            elif len(constants) > 1:
                verdict = "CONTRADICTORY_ACROSS_DATASETS"
            else:
                strong = {v["direction"] for v in per_dataset.values() if v["support"] >= self.cfg.promotion_threshold}
                if len(strong) > 1:
                    verdict = "CONTRADICTORY_ACROSS_DATASETS"
                elif all(v["support"] >= self.cfg.promotion_threshold for v in per_dataset.values()):
                    direction = strong.pop()
                    verdict = "PROMOTABLE"
                else:
                    if strong:
                        direction = strong.pop()
                    verdict = "BELOW_THRESHOLD"

            sample = group[0]
            expected = next(iter(constants)) if len(constants) == 1 else None
            eval_params = dict(sample.params)
            if sample.template == "change_count":
                eval_params["expected"] = expected
            cand = PromotionCandidate(
                template=sample.template,
                category=sample.category,
                params=eval_params,
                verdict=verdict,
                per_dataset=per_dataset,
                direction=direction,
                expected=expected,
                nl_description=rule_statement(sample.template, sample.category, eval_params,
                                              positive=direction == "positive"),
            )
            cand.rule = self._build_rule(cand)
            self.promotions.append(cand)

        self.promotions.sort(key=lambda p: (p.verdict != "PROMOTABLE", p.template, p.category,
                                            _params_label(p.template, p.params)))

    @staticmethod
    def _held(p: PromotionCandidate, v: dict) -> tuple[int, float]:
        """How often the candidate's statement held, in its direction, in one dataset."""
        held = v["n"] if p.direction == "positive" else v["m"] - v["n"]
        return held, (held / v["m"] if v["m"] else 0.0)

    def _build_rule(self, p: PromotionCandidate) -> dict:
        """Exportable, executable form of a candidate."""
        core = {"template": p.template, "category": p.category,
                "params": p.params, "direction": p.direction}
        rule_id = "INF-" + hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:8].upper()
        evidence = {}
        for ds, v in sorted(p.per_dataset.items()):
            held, pct = self._held(p, v)
            evidence[self._short_ds(ds)] = {"held": held, "observations": v["m"], "support": round(pct, 4)}
        return {
            "id": rule_id,
            **core,
            "statement": p.nl_description,
            "card": describe_rule(core),
            "min_support": min((e["support"] for e in evidence.values()), default=0.0),
            "evidence": evidence,
        }

    @staticmethod
    def _exportable(p: PromotionCandidate) -> bool:
        # "other" groups unrelated actions, so a rule over it says nothing about any one of them; an unknown tab cannot be matched.
        return (p.verdict == "PROMOTABLE" and not p.category.startswith("other@")
                and not p.category.endswith("@unknown"))

    def write_rules(self, path: str = INFERRED_RULES_PATH) -> int:
        """Write inferred_rules.json, which TemporalInvariantChecker evaluates."""
        from datetime import datetime
        rules = [p.rule for p in self.promotions if self._exportable(p)]
        Path(path).write_text(json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": str(self.dir),
            "datasets": [self._short_ds(d) for d in sorted(self.snapshots_by_dataset)],
            "threshold": self.cfg.promotion_threshold,
            "config": asdict(self.cfg),
            "rules": rules,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[Engine] {len(rules)} executable rules → {path}")
        return len(rules)

    def generate_nl_descriptions(self) -> list[str]:
        """Full rule text of the exported rules."""
        out = []
        for p in self.promotions:
            if not self._exportable(p):
                continue
            c = p.rule["card"]
            out.append(f"[{p.rule['id']}] {p.nl_description}. Trigger: {c['trigger']}. "
                       f"Applies when: {c['applies_when']}. Asserts: {c['asserts']}. "
                       f"Definitions: {c['definitions']}.")
        return out

    def print_report(self, verbose: bool = False) -> None:
        print(f"\n{'='*70}")
        print(f"Invariant Inference Engine - {len(self.snapshots_by_dataset)} dataset(s)")
        print(f"{'='*70}")
        counts = Counter(p.verdict for p in self.promotions)
        print(f"  Invariants that hold everywhere : {counts['PROMOTABLE']}")
        print(f"  Worth investigating             : {len(self._investigate_candidates())}")
        print(f"  Contradictory across datasets   : {counts['CONTRADICTORY_ACROSS_DATASETS']}")
        print(f"  Not enough data                 : {counts['INSUFFICIENT_EVIDENCE']}")
        if not verbose:
            print("  (run with --verbose for the raw per-dataset dump)")
            return

        by_dataset: dict[str, list[Finding]] = defaultdict(list)
        for f in self.findings:
            by_dataset[f.dataset].append(f)
        for dataset in sorted(by_dataset):
            fs = by_dataset[dataset]
            print(f"\n### Dataset: {dataset}  ({len(self.snapshots_by_dataset.get(dataset, []))} snapshots) ###")
            for f in fs:
                if f.verdict == "INCONSISTENT":
                    print(f"    [{f.template}] {f.category}: {_params_label(f.template, f.params)}  ({f.confidence})")

        print("\n── CROSS-DATASET PROMOTION ──")
        for p in self.promotions:
            if p.verdict in ("PROMOTABLE", "CONTRADICTORY_ACROSS_DATASETS"):
                print(f"  [{p.verdict}] {p.rule['id']} {p.nl_description}")
                for ds in sorted(p.per_dataset):
                    v = p.per_dataset[ds]
                    held, pct = self._held(p, v)
                    print(f"      {self._short_ds(ds)}: {held}/{v['m']} ({pct:.0%})")

    @staticmethod
    def _short_ds(dataset: str) -> str:
        """'strenda_schema__strenda_data_json' becomes 'strenda'."""
        return dataset.split("_schema__")[0]

    def _investigate_candidates(self) -> list[PromotionCandidate]:
        """BELOW_THRESHOLD candidates that clear the threshold in at least one dataset."""
        cands = [
            p for p in self.promotions
            if p.verdict == "BELOW_THRESHOLD"
            and any(self._held(p, v)[1] >= self.cfg.promotion_threshold for v in p.per_dataset.values())
        ]
        cands.sort(key=lambda p: -sum(self._held(p, v)[1] >= self.cfg.promotion_threshold
                                      for v in p.per_dataset.values()))
        return cands

    def _counter_examples(self, p: PromotionCandidate, v: dict) -> list[dict]:
        """Steps where the candidate's statement, in its own direction, failed."""
        return v["fail_examples"] if p.direction == "positive" else v["hold_examples"]

    def _support_row(self, p: PromotionCandidate, mark_below: bool) -> str:
        parts = []
        for ds, v in sorted(p.per_dataset.items()):
            held, pct = self._held(p, v)
            flag = "" if mark_below and pct < self.cfg.promotion_threshold else ""
            parts.append(f"{flag}{self._short_ds(ds)} {pct:.0%} ({held}/{v['m']})")
        return " · ".join(parts)

    @staticmethod
    def _statement(p: PromotionCandidate) -> str:
        """The candidate's sentence without the 'after <action>: ' prefix."""
        return p.nl_description.removeprefix(f"after {p.category}: ")

    @classmethod
    def _family_key(cls, p: PromotionCandidate) -> str:
        """Candidates with the same template and sentence form a rule family (same statement, different actions).
        The dashboard groups the same way.
        """
        return f"{p.template}|{cls._statement(p)}"

    def _write_grouped(self, w, candidates: list[PromotionCandidate], show_outliers: bool) -> None:
        """One collapsed <details> block per rule family."""
        if not candidates:
            w("_None._\n")
            return
        T = self.cfg.promotion_threshold
        families: dict[str, list[PromotionCandidate]] = defaultdict(list)
        for p in candidates:
            families[self._family_key(p)].append(p)
        ordered = sorted(families.values(), key=lambda ms: (-len(ms), self._statement(ms[0])))
        for members in ordered:
            members.sort(key=lambda p: p.category)
            n = len(members)
            if n > 1:
                scope = f"{n} actions"
            elif members[0].category == "*":
                scope = "any action"
            else:
                scope = f"<code>{html.escape(members[0].category)}</code>"
            warn = ""
            if show_outliers:
                weak = sorted({self._short_ds(ds) for p in members for ds, v in p.per_dataset.items()
                               if v["m"] >= self.cfg.min_observations_for_promotion
                               and self._held(p, v)[1] < T})
                if weak:
                    warn = f" · below threshold in {', '.join(weak)}"
            w("<details>")
            w(f"<summary><b>{html.escape(self._statement(members[0]))}</b> - {scope}{warn}</summary>\n")
            # Print the definition once if every rule of the family shares it.
            defs = {(p.rule["card"]["applies_when"], p.rule["card"]["asserts"]) for p in members}
            if len(defs) == 1:
                applies_when, asserts = next(iter(defs))
                w(f"_Applies when:_ {applies_when}. _Asserts:_ {asserts}.\n")
            for p in members:
                label = "Any action" if p.category == "*" else f"After `{p.category}`"
                card = p.rule["card"]
                w(f"- **{label}** `{p.rule['id']}`  ")
                w(f"  {self._support_row(p, mark_below=show_outliers)}  ")
                if len(defs) > 1:
                    w(f"  _Applies when:_ {card['applies_when']}. _Asserts:_ {card['asserts']}.")
                if show_outliers:
                    for ds, v in sorted(p.per_dataset.items()):
                        ex = self._counter_examples(p, v)
                        if self._held(p, v)[1] < T and ex:
                            steps = ", ".join(f"run {e.get('run_id')} step {e.get('step')} `{e.get('action')}`"
                                              for e in ex[:self.cfg.max_examples])
                            w(f"  - {self._short_ds(ds)} counter-examples: {steps}")
            w("")
            w("</details>\n")

    def write_readable_report(self, path: str) -> None:
        T = self.cfg.promotion_threshold
        promotable = [p for p in self.promotions if p.verdict == "PROMOTABLE"]
        contradictory = [p for p in self.promotions if p.verdict == "CONTRADICTORY_ACROSS_DATASETS"]
        insufficient = [p for p in self.promotions if p.verdict == "INSUFFICIENT_EVIDENCE"]
        investigate = self._investigate_candidates()

        out: list[str] = []
        w = out.append
        total = sum(len(s) for s in self.snapshots_by_dataset.values())
        w("# Invariant Inference Report\n")
        w(f"Source: `{self.dir}` - {len(self.snapshots_by_dataset)} datasets, {total} snapshots\n")
        w("| Dataset | Snapshots |")
        w("|---|---|")
        for ds, snaps in sorted(self.snapshots_by_dataset.items()):
            w(f"| {self._short_ds(ds)} | {len(snaps)} |")
        w("")
        w("## At a glance\n")
        w(f"- **{len(promotable)} candidate rules hold** in every dataset (≥{T:.0%} in each), as "
          f"{len({self._family_key(p) for p in promotable})} distinct statements; "
          f"{sum(self._exportable(p) for p in promotable)} are exported as executable rules")
        w(f"- **{len(investigate)} worth investigating** - hold in some datasets, not in others")
        w(f"- **{len(contradictory)} contradictory** - datasets clearly point opposite ways")
        w(f"- **{len(insufficient)} not judged** - too few observations in at least one dataset")
        w("")
        w(f"**Reading the numbers:** `strenda 92% (11/12)` means the statement held in 11 of 12 "
          f"steps it applied to in the Strenda dataset. Full rule definitions are in the dashboard "
          f"and in `{INFERRED_RULES_PATH}`.\n")
        w("**Rule families:** every statement below is collapsed. Click it to see the actions it "
          "holds after, with one rule ID and the support per dataset for each. (Needs a viewer that "
          "renders `<details>`, e.g. GitHub or the VS Code preview.)\n")
        w("## 1. Candidates that hold in every dataset\n")
        self._write_grouped(w, promotable, show_outliers=False)
        w("## 2. Worth investigating\n")
        w("Each statement reaches the threshold in the unmarked datasets but not in those marked - "
          "either the behaviour is schema-specific, or those schemas trigger a bug.\n")
        self._write_grouped(w, investigate, show_outliers=True)
        w("## 3. Contradictory across datasets\n")
        self._write_grouped(w, contradictory, show_outliers=True)
        w("## 4. Not enough data\n")
        lacking: dict[str, int] = defaultdict(int)
        by_cat: dict[str, int] = defaultdict(int)
        for p in insufficient:
            by_cat[p.category] += 1
            for ds in self.snapshots_by_dataset:
                v = p.per_dataset.get(ds)
                if v is None or v["m"] < self.cfg.min_observations_for_promotion:
                    lacking[self._short_ds(ds)] += 1
        w("| Dataset | Candidates it has too little data for |")
        w("|---|---|")
        for ds, n in sorted(lacking.items(), key=lambda x: -x[1]):
            w(f"| {ds} | {n} |")
        w("")
        w("| Action category | Candidates not judged |")
        w("|---|---|")
        for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):
            w(f"| {cat} | {n} |")
        w("")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("\n".join(out), encoding="utf-8")
        print(f"[Engine] Readable report → {path}")

    def dashboard_payload(self) -> dict:
        """All data the dashboard shows; every number is computed here."""
        from datetime import datetime
        investigate_ids = {id(p) for p in self._investigate_candidates()}
        group_of = {"PROMOTABLE": "holds", "CONTRADICTORY_ACROSS_DATASETS": "contradictory",
                    "INSUFFICIENT_EVIDENCE": "insufficient"}
        candidates = []
        for p in self.promotions:
            group = "investigate" if id(p) in investigate_ids else group_of.get(p.verdict, "below")
            datasets = {}
            for ds, v in p.per_dataset.items():
                held, pct = self._held(p, v)
                datasets[self._short_ds(ds)] = {
                    "held": held, "m": v["m"], "pct": round(pct, 4),
                    "examples": self._counter_examples(p, v)[:self.cfg.max_examples] if held < v["m"] else [],
                }
            candidates.append({
                "template": p.template,
                "category": p.category,
                "group": group,
                "exported": self._exportable(p),
                "statement": self._statement(p),
                "family": self._family_key(p),
                "datasets": datasets,
                "rule": p.rule,
            })
        return {
            "source": str(self.dir),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "threshold": self.cfg.promotion_threshold,
            "min_obs": self.cfg.min_observations_for_promotion,
            "near_margin": self.cfg.dashboard_near_margin,
            "rules_file": INFERRED_RULES_PATH,
            "datasets": [{"name": self._short_ds(ds), "snapshots": len(s)}
                         for ds, s in sorted(self.snapshots_by_dataset.items())],
            "candidates": candidates,
        }

    def write_html_dashboard(self, path: str) -> None:
        from tests.mutation_testing.inference_dashboard import render_dashboard
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(render_dashboard(self.dashboard_payload()), encoding="utf-8")
        print(f"[Engine] HTML dashboard → {path}")


def main():
    parser = argparse.ArgumentParser(description="Invariant inference engine for MetaConfigurator")
    parser.add_argument("--snapshots-dir", default=SNAPSHOTS_DIR,
                        help="Directory of per-dataset {dataset}.jsonl files")
    parser.add_argument("--datasets", default=None,
                        help="Comma-separated subset of datasets to load (default: all discovered)")
    parser.add_argument("--min-obs", type=int, default=None,
                        help="Override min_observations from the config")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Override promotion_threshold from the config")
    parser.add_argument("--config", default=None,
                        help="Parameter file (default: config/inference_config.json)")
    parser.add_argument("--profile", default=None,
                        help="Profile inside the parameter file (default: 'default')")
    parser.add_argument("--rules", default=INFERRED_RULES_PATH,
                        help=f"Executable rules for TemporalInvariantChecker (default: {INFERRED_RULES_PATH})")
    parser.add_argument("--report", default=f"{REPORTS_DIR}/invariant_report.md",
                        help=f"Readable Markdown report (default: {REPORTS_DIR}/invariant_report.md)")
    parser.add_argument("--dashboard", default=f"{REPORTS_DIR}/invariant_dashboard.html",
                        help=f"Self-contained HTML dashboard (default: {REPORTS_DIR}/invariant_dashboard.html)")
    parser.add_argument("--output-descriptions", default=None,
                        help="Write full rule text for exported rules (Component 3 input)")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print the raw per-dataset dump to the console")
    args = parser.parse_args()

    cfg = with_overrides(load_config(args.config, args.profile),
                         min_observations=args.min_obs, promotion_threshold=args.threshold)
    engine = InvariantInferenceEngine(args.snapshots_dir, config=cfg)
    requested = [d.strip() for d in args.datasets.split(",")] if args.datasets else None
    engine.load_all(requested)
    engine.run()
    engine.print_report(verbose=args.verbose)
    engine.write_rules(args.rules)
    engine.write_readable_report(args.report)
    engine.write_html_dashboard(args.dashboard)

    if args.output_descriptions:
        descs = engine.generate_nl_descriptions()
        Path(args.output_descriptions).write_text("\n".join(descs), encoding="utf-8")
        print(f"[Engine] {len(descs)} rule descriptions → {args.output_descriptions}")


if __name__ == "__main__":
    main()