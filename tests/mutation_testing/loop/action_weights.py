"""Action weights: loading of the weight profiles and the weight key of a discovered element."""
from __future__ import annotations

import json
from pathlib import Path
from tests.mutation_testing.discovery.element import ElementDescriptor


_UNDO_REDO_TESTIDS = ("undo", "redo", "schema_undo", "schema_redo")


# The only selectable mode-switch action: discovery keeps one "mode-active-false" entry (the inactive
# tab) and drops "mode-active-true". Clicking it toggles Data and Schema.
_MODE_SWITCH_TESTID = "mode-active-false"


_ACTION_WEIGHTS_DIR = Path(__file__).resolve().parent.parent / "config"
_DEFAULT_WEIGHTS_FILENAME = "action_weights.json"
_active_weights_filename = _DEFAULT_WEIGHTS_FILENAME


def set_action_weights_profile(profile: str | None = None) -> None:
    """Select the weights file: None or "default" -> action_weights.json, "x" -> action_weights_x.json.

    Call before run_random(). Clears the cache so the new profile applies immediately.
    """
    global _active_weights_filename, _action_weights_cache
    _active_weights_filename = (
        _DEFAULT_WEIGHTS_FILENAME if not profile or profile == "default"
        else f"action_weights_{profile}.json"
    )
    _action_weights_cache = None
    print(f"[weights] active profile file: {_active_weights_filename}")


def get_active_weights_filename() -> str:
    """The weights file in use now. Read this at record time; importing the variable would keep the import-time default."""
    return _active_weights_filename


# Fallback weights, Mode > View > Action type. Each level's weight is shared among the children present
# in the current step, so a level with more branches does not get more total probability.
_DEFAULT_HIERARCHICAL_WEIGHTS: dict = {
    "Schema Editor": {
        "weight": 10, "enabled": True,
        "children": {
            "GUI View": {
                "weight": 5, "enabled": True,
                "children": {
                    "add_item_required":      {"weight": 8, "enabled": True},
                    "add_item_or_property":   {"weight": 8, "enabled": True},
                    "input_select_input":     {"weight": 8, "enabled": True},
                    "input_select_other":     {"weight": 5, "enabled": True},
                    "fill":                   {"weight": 5, "enabled": True},
                    "rename":                 {"weight": 4, "enabled": True},
                    "remove_property":        {"weight": 3, "enabled": True},
                    "expand":                 {"weight": 2, "enabled": True},
                    "collapse":               {"weight": 3, "enabled": True},
                    "mode_switch":            {"weight": 2, "enabled": True},
                    "format_selector":        {"weight": 2, "enabled": True},
                    "undo_redo_base":         {"weight": 2, "enabled": True},
                    "undo_redo_boosted":      {"weight": 10, "enabled": True},
                    "default":                {"weight": 1, "enabled": True},
                },
            },
            "Schema Diagram": {"weight": 0, "enabled": False, "children": {}},
            "Text View": {
                "weight": 8, "enabled": True,
                "children": {
                    "edit_text":              {"weight": 3, "enabled": True},
                    "add_child_text":         {"weight": 3, "enabled": True},
                    "change_type_text":       {"weight": 3, "enabled": True},
                    "remove_child_text":      {"weight": 1, "enabled": True},
                    "rename_child_text":      {"weight": 1, "enabled": True},
                    "add_array_item_text":    {"weight": 3, "enabled": True},
                    "remove_array_item_text": {"weight": 3, "enabled": True},
                    "clear_text":              {"weight": 1, "enabled": True},
                },
            },
        },
    },
    "Data Editor": {
        "weight": 10, "enabled": True,
        "children": {
            "GUI View": {
                "weight": 8, "enabled": True,
                "children": {
                    "add_item_required":      {"weight": 8, "enabled": True},
                    "add_item_or_property":   {"weight": 8, "enabled": True},
                    "input_select_input":     {"weight": 8, "enabled": True},
                    "input_select_other":     {"weight": 5, "enabled": True},
                    "fill":                   {"weight": 5, "enabled": True},
                    "rename":                 {"weight": 4, "enabled": True},
                    "remove_property":        {"weight": 3, "enabled": True},
                    "expand":                 {"weight": 2, "enabled": True},
                    "collapse":               {"weight": 3, "enabled": True},
                    "mode_switch":            {"weight": 2, "enabled": True},
                    "format_selector":        {"weight": 2, "enabled": True},
                    "undo_redo_base":         {"weight": 2, "enabled": True},
                    "undo_redo_boosted":      {"weight": 10, "enabled": True},
                    "default":                {"weight": 1, "enabled": True},
                },
            },
            "Text View": {
                "weight": 5, "enabled": True,
                "children": {
                    "edit_text":              {"weight": 3, "enabled": True},
                    "add_child_text":         {"weight": 3, "enabled": True},
                    "change_type_text":       {"weight": 3, "enabled": True},
                    "remove_child_text":      {"weight": 1, "enabled": True},
                    "rename_child_text":      {"weight": 1, "enabled": True},
                    "add_array_item_text":    {"weight": 3, "enabled": True},
                    "remove_array_item_text": {"weight": 3, "enabled": True},
                    "clear_text":              {"weight": 1, "enabled": True},
                },
            },
        },
    },
    "Settings": {"weight": 0, "enabled": False, "children": {}},
}


_action_weights_cache: dict | None = None


def _load_action_weights() -> dict:
    """Active weights file, cached after the first read. Falls back to _DEFAULT_HIERARCHICAL_WEIGHTS if it is missing or malformed."""
    global _action_weights_cache
    if _action_weights_cache is not None:
        return _action_weights_cache

    path = _ACTION_WEIGHTS_DIR / _active_weights_filename
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        _action_weights_cache = {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception as e:
        print(f" Could not load {path.name} ({e}) — using hardcoded hierarchical defaults")
        _action_weights_cache = _DEFAULT_HIERARCHICAL_WEIGHTS
    return _action_weights_cache


# Action types that change schema or data (expand/collapse, mode switch and format toggle do not)
_MUTATING_CATEGORIES = {"edit_field", "add_item", "add_property", "rename", "remove", "edit_text", "add_child_text", "change_type_text", "remove_child_text", "rename_child_text", "add_array_item_text", "remove_array_item_text", "clear_text"}


def _element_action_key(el: ElementDescriptor, boost_undo_redo: bool) -> str:
    """Action-type key of an element within its (Mode, View) branch, e.g. 'add_child_text', 'fill', 'expand'."""
    if el.interaction == "fill":
        return "fill"
    elif el.testid.startswith("input-select-") and el.tag == "input":
        return "input_select_input"
    elif el.testid.startswith("input-select-"):
        return "input_select_other"
    elif el.testid == "add-item-required":
        return "add_item_required"
    elif el.testid.startswith("add-item-") or el.testid.startswith("add-property-"):
        return "add_item_or_property"
    elif el.testid.startswith("expand-"):
        return "expand"
    elif el.testid.startswith("collapse-"):
        return "collapse"
    elif el.testid.startswith("rename-child-text-"):
        return "rename_child_text"
    elif el.testid.startswith("rename-"):
        return "rename"
    elif el.testid.startswith("remove-property-"):
        return "remove_property"
    elif el.testid.startswith("mode-active-"):
        return "mode_switch"
    elif el.testid == "format-selector":
        return "format_selector"
    elif el.testid.startswith("edit-text-"):
        return "edit_text"
    elif el.testid.startswith("add-child-text-"):
        return "add_child_text"
    elif el.testid.startswith("change-type-text-"):
        return "change_type_text"
    elif el.testid.startswith("add-array-item-text-"):
        return "add_array_item_text"
    elif el.testid.startswith("remove-array-item-text-"):
        return "remove_array_item_text"
    elif el.testid.startswith("remove-child-text-"):
        return "remove_child_text"
    elif el.testid.startswith("clear-text-"):
        return "clear_text"
    elif el.testid in _UNDO_REDO_TESTIDS:
        return "undo_redo_boosted" if boost_undo_redo else "undo_redo_base"
    else:
        return "default"


def _mode_label(el: ElementDescriptor) -> str:
    """'Schema Editor' or 'Data Editor', from ElementDescriptor.mode."""
    return "Schema Editor" if el.mode == "schemaEditor" else "Data Editor"


def _element_action_key_by_testid(testid: str | None) -> str | None:
    """Same classification as _element_action_key, from a bare testid (recent_testids holds no elements)."""
    if testid is None:
        return None
    fake = ElementDescriptor(testid=testid, tag="", role="", interaction="click")
    return _element_action_key(fake, boost_undo_redo=False)