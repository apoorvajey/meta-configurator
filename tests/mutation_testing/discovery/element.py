"""Discovered page elements: ElementDescriptor and the classification of data-testid values."""
from __future__ import annotations

from dataclasses import dataclass
from tests.mutation_testing.discovery.paths import humanize_path


CATEGORY_LABELS = {
    "0_undo_redo":              "Undo or redo the last change",
    "1_expand":                 "Show or hide a section",
    "2_edit_field":             "Change a value (using the visual editor)",
    "3_field":                  "Change a dropdown or checkbox (visual editor)",
    "4_add":                    "Add something new (visual editor)",
    "5_rename":                 "Rename something (visual editor)",
    "6_remove":                 "Delete something (visual editor)",
    "7_mode":                   "Switch between Data and Schema",
    "8_format":                 "Switch file format (JSON/YAML)",
    "10_text_edit_value":       "Change a value (by typing)",
    "11_text_add_child":        "Add a new field (by typing)",
    "12_text_change_type":      "Change what kind of thing this is (by typing)",
    "13_text_remove_child":     "Delete a field (by typing)",
    "14_text_rename_child":     "Rename a field (by typing)",
    "15_text_add_array_item":   "Add a list item (by typing)",
    "16_text_remove_array_item":"Delete a list item (by typing)",
    "17_text_clear_all":        "Clear the whole document (by typing)",
    "9_other":                  "Other",
}


@dataclass
class ElementDescriptor:
    testid: str
    tag: str
    role: str
    interaction: str
    label: str = ""
    css_selector: str | None = None
    js_click: str | None = None
    mode: str = ""  # "dataEditor" | "schemaEditor" - see _infer_element_mode()

    @property
    def view(self) -> str:
        """Which 'view' bucket this action belongs to, for the Mode > View > Action hierarchy.

        Schema Diagram actions are not discovered yet, so 'Schema Diagram' is never returned; it exists in the weights
        config only as a placeholder.
        """
        _text_view_prefixes = (
            "edit-text-", "add-child-text-", "change-type-text-",
            "remove-child-text-", "rename-child-text-",
            "add-array-item-text-", "remove-array-item-text-",
            "clear-text-",
        )
        if self.testid.startswith(_text_view_prefixes):
            return "Text View"
        return "GUI View"

    @property
    def property_name(self) -> str | None:
        for prefix in ("input-string-", "input-number-", "input-boolean-",
                       "property-data-", "add-item-", "add-property-"):
            if self.testid.startswith(prefix):
                return self.testid[len(prefix):]
        return None

    @property
    def category(self) -> str:
        if self.testid.startswith("expand-") or self.testid.startswith("collapse-"):
            return "1_expand"
        if self.testid.startswith(("input-string-", "input-number-", "input-boolean-")):
            return "2_edit_field"
        if self.testid.startswith("input-select-"):
            return "3_field"
        if self.testid.startswith(("add-item-", "add-property-")):
            return "4_add"
        if self.testid.startswith("rename-property-"):
            return "5_rename"
        if self.testid.startswith("remove-property-"):
            return "6_remove"
        if self.testid == "mode-active-false":
            return "7_mode"
        if self.testid == "format-selector":
            return "8_format"
        if self.testid in ("undo", "redo", "schema_undo", "schema_redo"):
            return "0_undo_redo"
        if self.testid.startswith("edit-text-"):
            return "10_text_edit_value"
        if self.testid.startswith("add-child-text-"):
            return "11_text_add_child"
        if self.testid.startswith("change-type-text-"):
            return "12_text_change_type"
        if self.testid.startswith("remove-child-text-"):
            return "13_text_remove_child"
        if self.testid.startswith("rename-child-text-"):
            return "14_text_rename_child"
        if self.testid.startswith("add-array-item-text-"):
            return "15_text_add_array_item"
        if self.testid.startswith("remove-array-item-text-"):
            return "16_text_remove_array_item"
        if self.testid.startswith("clear-text-"):
            return "17_text_clear_all"
        return "9_other"

    @property
    def input_type(self) -> str | None:
        if self.testid.startswith("input-string-"):
            return "string"
        if self.testid.startswith("input-number-"):
            return "number"
        if self.testid.startswith("input-boolean-"):
            return "boolean"
        return None

    @property
    def human_label(self) -> str:
        if self.testid.startswith("input-string-"):
            prop = self.testid[len("input-string-"):]
            return f'Change "{humanize_path(prop)}"'
        if self.testid.startswith("input-number-"):
            prop = self.testid[len("input-number-"):]
            return f'Change the number "{humanize_path(prop)}"'
        if self.testid.startswith("input-boolean-"):
            prop = self.testid[len("input-boolean-"):]
            return f'Switch "{humanize_path(prop)}" on or off'
        if self.testid.startswith("input-select-"):
            prop = self.testid[len("input-select-"):]
            return f'Choose a new value for "{humanize_path(prop)}"'
        if self.testid.startswith("expand-"):
            prop = self.testid[len("expand-"):]
            return f'Open "{humanize_path(prop)}" to see what\'s inside'
        if self.testid.startswith("collapse-"):
            prop = self.testid[len("collapse-"):]
            return f'Close "{humanize_path(prop)}"'
        if self.testid.startswith("add-item-"):
            prop = self.testid[len("add-item-"):]
            return f'Add a new item to the "{humanize_path(prop)}" list'
        if self.testid.startswith("add-property-"):
            suffix = self.testid[len("add-property-"):]
            return f'Add a new field to "{humanize_path(suffix)}"' if suffix else \
                   'Add a new field here'
        if self.testid.startswith("rename-property-"):
            prop = self.testid[len("rename-property-"):]
            return f'Rename "{humanize_path(prop)}"'
        if self.testid.startswith("remove-property-"):
            prop = self.testid[len("remove-property-"):]
            return f'Delete "{humanize_path(prop)}"'
        if self.testid == "mode-active-false":
            return "Switch between Data and Schema"
        if self.testid == "format-selector":
            return "Switch file format (JSON / YAML / XML)"
        if self.testid == "undo":
            return "Undo the last change (in Data)"
        if self.testid == "redo":
            return "Redo the last undone change (in Data)"
        if self.testid == "schema_undo":
            return "Undo the last change (in Schema)"
        if self.testid == "schema_redo":
            return "Redo the last undone change (in Schema)"
        if self.testid.startswith("edit-text-"):
            mode_and_path = self.testid[len("edit-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Change "{humanize_path(path_str)}" {mode_label} — it\'s currently {self.label}'
        if self.testid.startswith("add-child-text-"):
            mode_and_path = self.testid[len("add-child-text-"):]
            mode_str, _, rest = mode_and_path.partition(":")
            path_str, _, prop_name = rest.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            if prop_name == "*":
                return (f'Add a new field (not in the schema) to "{humanize_path(path_str)}" '
                        f'{mode_label} — it already has {self.label}')
            if prop_name:
                return (f'Add "{prop_name}" to "{humanize_path(path_str)}" '
                        f'{mode_label} — it already has {self.label}')
            return f'Add a new field to "{humanize_path(path_str)}" {mode_label} — it already has {self.label}'
        if self.testid.startswith("change-type-text-"):
            mode_and_path = self.testid[len("change-type-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Change what kind of thing "{humanize_path(path_str)}" is {mode_label} — it\'s currently {self.label}'
        if self.testid.startswith("remove-child-text-"):
            mode_and_path = self.testid[len("remove-child-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Delete "{humanize_path(path_str)}" {mode_label} — it\'s currently {self.label}'
        if self.testid.startswith("rename-child-text-"):
            mode_and_path = self.testid[len("rename-child-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Rename "{humanize_path(path_str)}" {mode_label}'
        if self.testid.startswith("add-array-item-text-"):
            mode_and_path = self.testid[len("add-array-item-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            target_desc = humanize_path(path_str)
            return f'Add a new item to the "{target_desc}" list {mode_label} — it already has {self.label}'
        if self.testid.startswith("remove-array-item-text-"):
            mode_and_path = self.testid[len("remove-array-item-text-"):]
            mode_str, _, path_str = mode_and_path.partition(":")
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Delete "{humanize_path(path_str)}" {mode_label} — it\'s currently {self.label}'
        if self.testid.startswith("clear-text-"):
            # Targets the whole document, so there is no path component
            mode_str = self.testid[len("clear-text-"):]
            mode_label = "in the Data section" if mode_str == "dataEditor" else "in the Schema section"
            return f'Select everything and delete it {mode_label} — clears the whole document'
        return self.testid


_SKIP_PREFIXES = (
    "current-path",
    "submit-",
    "current-selected",
    "test-component",
    "data-input",
    "schema-input",
    "current-path-input",
    "current-selected-element-input",
    "property-metadata-",
    "required-star",
    "validation-error-icon",
    "mode-settings-button",
    # Root-level schema meta-fields: mutating them corrupts the validation context for the rest of the run
    "input-select-$ref",
    "input-select-$schema",
    "input-select-$id",
    "remove-property-$defs",
    "remove-property-$schema",
    "rename-property-$defs",
)


_CLICK_PATTERNS = (
    "mode-active-",
    "mode-settings-button",
    "add-item-",
    "add-property-",
    "remove-property-",
    "rename-property-",
    "expand-",
    "collapse-",
    "undo",
    "redo",
    "schema_undo",
    "schema_redo",
    "edit-text-",
    "add-child-text-",
    "change-type-text-",
    "remove-child-text-",
    "rename-child-text-",
    "add-array-item-text-",
    "remove-array-item-text-",
    "clear-text-",
)


_FILL_PATTERNS = (
    "input-string-",
    "input-number-",
    "input-boolean-",
)


_SELECT_PATTERNS = (
    "format-selector",
    "input-select-",
)


def _infer_element_mode(testid: str, current_mode: str) -> str:
    """'dataEditor' or 'schemaEditor' for a discovered testid.

    Text View and undo/redo testids encode the mode; GUI View testids do not, so they get `current_mode`
    (the mode active on screen, computed once per discover() call).
    """
    if testid in ("undo", "redo"):
        return "dataEditor"
    if testid in ("schema_undo", "schema_redo"):
        return "schemaEditor"
    if testid.startswith("clear-text-"):
        # No path suffix: the mode is everything after the prefix
        return testid[len("clear-text-"):]
    _mode_prefixed = (
        "edit-text-", "add-child-text-", "change-type-text-",
        "remove-child-text-", "rename-child-text-",
        "add-array-item-text-", "remove-array-item-text-",
    )
    for p in _mode_prefixed:
        if testid.startswith(p):
            rest = testid[len(p):]
            mode_str, _, _ = rest.partition(":")
            return mode_str
    return current_mode


def _classify(testid: str) -> str | None:
    for p in _SKIP_PREFIXES:
        if testid.startswith(p) or testid == p.rstrip("-"):
            return None
    for p in _FILL_PATTERNS:
        if testid.startswith(p):
            return "fill"
    for p in _CLICK_PATTERNS:
        if testid.startswith(p):
            return "click"
    for p in _SELECT_PATTERNS:
        if testid == p or testid.startswith(p):
            return "select"
    return None
