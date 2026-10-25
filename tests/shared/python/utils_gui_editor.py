"""Python mirror of utilsGuiEditor.ts: reading and manipulating the GUI (form) editor panel by data-testid."""

from __future__ import annotations
from playwright.sync_api import Page

from tests.shared.python.utils import path_to_string


def check_property_existence(page: Page, property_path: list, should_be_visible: bool):
    """Assert that a GUI editor property row is (or is not) visible."""
    path_str = path_to_string(property_path)
    prop = page.get_by_test_id(f"property-data-{path_str}")
    if should_be_visible:
        prop.wait_for(state="visible", timeout=5000)
        assert prop.is_visible(), f"Expected property '{path_str}' to be visible"
    else:
        assert not prop.is_visible(timeout=2000), \
            f"Expected property '{path_str}' to be hidden"


def read_property_exists(page: Page, property_path: list) -> bool:
    """Non-asserting: True if the property row exists in the DOM. Uses count() > 0 rather than is_visible() to avoid
    false negatives when the row is scrolled out of the viewport.
    """
    try:
        path_str = path_to_string(property_path)
        return page.get_by_test_id(f"property-data-{path_str}").count() > 0
    except Exception:
        return False


def read_top_level_property_keys(page: Page) -> list[str]:
    """Every top-level property-data-<key> row currently rendered (the key contains no '.' or '['). Needed for the
    reverse direction of GUI-vs-data: a row can persist for a key that schema.required still lists after the key
    was removed from data (e.g. by an undo that touched data but not schema).
    """
    try:
        testids = page.evaluate("""
            () => [...document.querySelectorAll('[data-testid^="property-data-"]')]
                .map(el => el.getAttribute('data-testid').slice('property-data-'.length))
        """)
        return [t for t in testids if "." not in t and "[" not in t]
    except Exception:
        return []

def edit_string_property(page: Page, property_path: list, value: str):
    """Type a value into a string property text field."""
    path_str = path_to_string(property_path)
    field = page.get_by_test_id(f"property-data-{path_str}").get_by_role("textbox")
    field.click()
    field.fill(value)
    field.press("Enter")
    page.wait_for_timeout(300)


def check_string_property(page: Page, property_path: list, expected_value: str):
    """Assert the current value of a string property text field."""
    path_str = path_to_string(property_path)
    field = page.get_by_test_id(f"property-data-{path_str}").get_by_role("textbox")
    actual = field.input_value()
    assert actual == expected_value, \
        f"Property '{path_str}': expected '{expected_value}', got '{actual}'"


def read_string_property(page: Page, property_path: list) -> str | None:
    """Non-asserting: the current value, or None if not visible."""
    try:
        path_str = path_to_string(property_path)
        field = page.get_by_test_id(f"property-data-{path_str}").get_by_role("textbox")
        if field.is_visible(timeout=2000):
            return field.input_value()
    except Exception:
        pass
    return None


def edit_boolean_property(page: Page, property_path: list, value: bool):
    """Click the true/false button for a boolean property."""
    path_str = path_to_string(property_path)
    btn = page.get_by_test_id(f"property-data-{path_str}").get_by_role(
        "button", name=str(value).lower()
    )
    btn.click()
    page.wait_for_timeout(300)


def edit_number_property(page: Page, property_path: list, value: float):
    """Set a numeric spinbutton property, using the same native value setter trick as the TypeScript original to
    bypass React's controlled input behaviour.
    """
    path_str = path_to_string(property_path)
    spin = page.get_by_test_id(f"property-data-{path_str}").get_by_role("spinbutton")
    spin.click()
    spin.evaluate(
        """(input, newValue) => {
            const nativeValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            )?.set;
            nativeValueSetter?.call(input, String(newValue));
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.setAttribute('aria-valuenow', String(newValue));
        }""",
        value,
    )
    spin.blur()
    page.wait_for_timeout(300)


def check_number_property(page: Page, property_path: list, expected_value: float):
    """Assert the current value of a numeric spinbutton."""
    path_str = path_to_string(property_path)
    spin = page.get_by_test_id(f"property-data-{path_str}").get_by_role("spinbutton")
    actual = spin.get_attribute("aria-valuenow")
    assert actual == str(expected_value), \
        f"Property '{path_str}': expected '{expected_value}', got aria-valuenow='{actual}'"


def add_array_item(page: Page, property_path: list):
    """Click the add-item button for an array property."""
    path_str = path_to_string(property_path)
    page.get_by_test_id(f"add-item-{path_str}").click()
    page.wait_for_timeout(300)


def add_object_property(page: Page, property_path: list):
    """Click the add-property button for an object node."""
    path_str = path_to_string(property_path)
    page.get_by_test_id(f"add-property-{path_str}").click()
    page.wait_for_timeout(300)


def remove_optional_property_value(page: Page, property_path: list):
    """Click the Remove button for an optional property."""
    path_str = path_to_string(property_path)
    page.get_by_test_id(f"property-data-{path_str}").get_by_role(
        "button", name="Remove"
    ).click()
    page.wait_for_timeout(300)


def expand_or_collapse_property(page: Page, property_name: str):
    """Click the expand/collapse button for an object or array row. Matches the cell name by regex because the full
    label includes the child count, e.g. 'address : object 2 properties'.
    """
    import re
    btn = page.get_by_role("cell", name=re.compile(f"^{property_name} :")).get_by_role("button")
    btn.click()
    page.wait_for_timeout(300)


def check_property_schema_violation(page: Page, property_path: list, should_be_visible: bool):
    """Assert that a schema violation icon is (or is not) shown for a property."""
    path_str = path_to_string(property_path)
    metadata = page.get_by_test_id(f"property-metadata-{path_str}")
    icon = metadata.get_by_test_id("validation-error-icon")
    if should_be_visible:
        metadata.wait_for(state="visible", timeout=5000)
        icon.wait_for(state="visible", timeout=8000)
    else:
        assert not icon.is_visible(timeout=2000), \
            f"Expected no validation error icon for '{path_str}'"


def check_property_required(page: Page, property_path: list, should_be_visible: bool):
    """Assert that the required-star icon is (or is not) shown for a property."""
    path_str = path_to_string(property_path)
    star = page.get_by_test_id(f"property-metadata-{path_str}").get_by_test_id("required-star")
    if should_be_visible:
        star.wait_for(state="visible", timeout=3000)
    else:
        assert not star.is_visible(timeout=2000), \
            f"Expected no required star for '{path_str}'"


def read_required_star_visible(page: Page, property_path: list) -> bool:
    """Non-asserting: True if the required star is visible."""
    try:
        path_str = path_to_string(property_path)
        stars = page.get_by_test_id(
            f"property-metadata-{path_str}"
        ).get_by_test_id("required-star")
        return any(stars.nth(i).is_visible() for i in range(stars.count()))
    except Exception:
        return False