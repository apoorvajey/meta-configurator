"""
tests/shared/python
===================
Python mirrors of the TypeScript shared E2E helpers.

Import pattern:
    from tests.shared.python.test_panel import TestPanel, tp_get_data
    from tests.shared.python.utils import SessionMode, open_app
    from tests.shared.python.utils_gui_editor import edit_string_property
    from tests.shared.python.utils_code_editor import read_text_editor_as_json
"""

from tests.shared.python.utils import (
    SessionMode,
    open_app,
    open_app_with_test_panel,
    setup_test_panel_via_localstorage,
    force_editor_mode,
    get_current_editor_mode,
    force_data_format,
    get_current_data_format,
    select_initial_schema_from_examples,
    dismiss_dialog_if_present,
    path_to_string,
    path_to_json_pointer,
    json_pointer_to_path,
)

from .test_panel import (
    TestPanel,
    tp_get_data,
    tp_get_schema,
    tp_get_current_path,
    tp_get_current_selected_element,
    tp_force_data,
    tp_force_schema,
    tp_force_current_path,
    tp_force_current_selected_element,
)

from .utils_gui_editor import (
    edit_string_property,
    check_string_property,
    read_string_property,
    edit_boolean_property,
    edit_number_property,
    check_number_property,
    add_array_item,
    add_object_property,
    remove_optional_property_value,
    expand_or_collapse_property,
    check_property_existence,
    read_property_exists,
    check_property_schema_violation,
    check_property_required,
    read_required_star_visible,
)

from .utils_code_editor import (
    read_code_editor_text,
    read_text_editor_content,
    check_code_editor_for_text,
    force_code_editor_text,
)

__all__ = [
    # utils
    "SessionMode",
    "open_app", "open_app_with_test_panel", "setup_test_panel_via_localstorage",
    "force_editor_mode", "get_current_editor_mode",
    "force_data_format", "get_current_data_format",
    "select_initial_schema_from_examples", "dismiss_dialog_if_present",
    "path_to_string", "path_to_json_pointer", "json_pointer_to_path",
    # test_panel
    "TestPanel",
    "tp_get_data", "tp_get_schema",
    "tp_get_current_path", "tp_get_current_selected_element",
    "tp_force_data", "tp_force_schema",
    "tp_force_current_path", "tp_force_current_selected_element",
    # gui editor
    "edit_string_property", "check_string_property", "read_string_property",
    "edit_boolean_property",
    "edit_number_property", "check_number_property",
    "add_array_item", "add_object_property", "remove_optional_property_value",
    "expand_or_collapse_property",
    "check_property_existence", "read_property_exists",
    "check_property_schema_violation", "check_property_required",
    "read_required_star_visible",
    # code editor
    "read_code_editor_text", "read_text_editor_content",
    "check_code_editor_for_text", "force_code_editor_text",
]
