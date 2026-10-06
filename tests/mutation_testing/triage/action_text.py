"""Readable descriptions of recorded actions."""
from __future__ import annotations




EDITOR_KINDS = [
    ("add-array-item-text-", "add_array_item"),
    ("add-child-text-", "add_child"),
    ("remove-array-item-text-", "remove_array_item"),
    ("remove-child-text-", "remove_child"),
    ("change-type-text-", "change_type"),
    ("rename-child-text-", "rename_child"),
    ("edit-text-", "edit"),
    ("clear-text-", "clear"),
]


PATH_KINDS = [
    ("add-property-", "add_property"),
    ("add-item-", "add_item"),
    ("collapse-", "collapse"),
    ("expand-", "expand"),
]


STANDALONE_KINDS = {
    "undo": "undo", "redo": "redo", "schema_undo": "undo", "schema_redo": "redo",
    "format-selector": "format", "mode-active-false": "switch", "mode-active-true": "switch",
}


# what the log does not record for each kind of action
MISSING_DETAIL = {
    "edit": "new value not recorded",
    "change_type": "new type not recorded",
    "rename_child": "new name not recorded",
    "add_property": "property name not recorded",
    "add_array_item": "item content not recorded",
    "format": "chosen format not recorded",
    "switch": "target tab not recorded",
}


def parse_action(action_id: str) -> dict:
    """Split an action id such as
    'dynamic:add-child-text-schemaEditor:definitions.File.properties.content:if'
    into kind, panel, parent path and child name.
    """
    raw = action_id or ""
    is_repair = raw.startswith("repair:")
    for p in ("dynamic:", "repair:"):
        if raw.startswith(p):
            raw = raw[len(p):]
            break
    out = {"kind": "other", "editor": None, "path": "", "child": None, "repair": is_repair, "raw": raw}
    if raw in STANDALONE_KINDS:
        out["kind"] = STANDALONE_KINDS[raw]
        out["standalone"] = raw
        return out
    for prefix, kind in EDITOR_KINDS:
        if raw.startswith(prefix):
            editor, _, rest = raw[len(prefix):].partition(":")
            out.update(kind=kind, editor=editor)
            if kind == "add_child":
                if ":" in rest:
                    parent, _, child = rest.rpartition(":")
                    out.update(path=parent, child=None if child in ("", "*") else child)
                else:
                    out.update(path=rest)
            else:
                out["path"] = rest.rstrip(":*").strip(":")
            return out
    for prefix, kind in PATH_KINDS:
        if raw.startswith(prefix):
            out.update(kind=kind, path=raw[len(prefix):])
            return out
    return out


def missing_detail(action_id: str, step: dict | None = None) -> str:
    # "observed_value" (written by the mutation loop) holds what the action actually changed.
    observed = (step or {}).get("observed_value")
    if observed:
        return f"now: {str(observed)[:60]}"
    p = parse_action(action_id)
    if p["kind"] == "add_child" and p["child"] is None:
        return "child name not recorded"
    return MISSING_DETAIL.get(p["kind"], "")


def translate_action(action_id: str, mode: str, panels: dict, include_path: bool = True) -> str:
    p = parse_action(action_id)
    kind, path, child = p["kind"], p["path"], p["child"]
    where = panels.get(p["editor"] or mode, "")
    in_ = f" in the {where}" if where else ""
    par = f" ({where})" if where else ""
    root = "the top level of the schema" if p["editor"] == "schemaEditor" else "the top level of the data"

    if kind == "add_child":
        if include_path:
            head = f"Added the child field '{child}'" if child else "Added a child field"
            label = f"{head} under '{path}'{in_}" if path else f"{head} at {root}{in_}"
        else:
            label = f"Added a child field{in_}"
    elif kind in ("edit", "change_type", "rename_child", "remove_child", "add_array_item", "remove_array_item"):
        verb = {
            "edit": "Edited the value at", "change_type": "Changed the type of",
            "rename_child": "Renamed the field", "remove_child": "Removed the field",
            "add_array_item": "Added a new item to the array", "remove_array_item": "Removed the array item",
        }[kind]
        bare = {
            "edit": "Edited a value", "change_type": "Changed a field's type", "rename_child": "Renamed a field",
            "remove_child": "Removed a field", "add_array_item": "Added an array item", "remove_array_item": "Removed an array item",
        }[kind]
        label = (f"{verb} '{path}'{in_}" if path else f"{verb} {root}{in_}") if include_path else f"{bare}{in_}"
    elif kind == "clear":
        label = f"Cleared all the text{in_}"
    elif kind in ("add_item", "add_property", "collapse", "expand"):
        verb = {"add_item": "Added an item to", "add_property": "Added a property to",
                "collapse": "Collapsed", "expand": "Expanded"}[kind]
        bare = {"add_item": "Added an item", "add_property": "Added a property",
                "collapse": "Collapsed a node", "expand": "Expanded a node"}[kind]
        via = " using the GUI" if kind in ("add_item", "add_property") else ""
        label = f"{verb} '{path}'{via}{par}" if (include_path and path) else f"{bare}{via}{par}"
    elif kind == "undo":
        label = f"Clicked Undo{par}"
    elif kind == "redo":
        label = f"Clicked Redo{par}"
    elif kind == "format":
        label = "Changed the file format"
    elif kind == "switch":
        label = "Switched to another view/tab"
    else:
        label = p["raw"].replace("-", " ").replace(":", " -> ") or "unknown action"
    return f"Test tool: {label}" if p["repair"] else label
