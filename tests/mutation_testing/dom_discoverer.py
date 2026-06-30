"""
dom_discoverer.py
=================
Located at: tests/mutation_testing/dom_discoverer.py
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Page


# ---------------------------------------------------------------------------
# ElementDescriptor
# ---------------------------------------------------------------------------

@dataclass
class ElementDescriptor:
    testid: str
    tag: str
    role: str
    interaction: str
    label: str = ""
    css_selector: str | None = None
    js_click: str | None = None

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
            if prop.isdigit():
                return f'Type into array item [{prop}] string field'
            return f'Type into "{prop}" string field'
        if self.testid.startswith("input-number-"):
            prop = self.testid[len("input-number-"):]
            return f'Set "{prop}" number field'
        if self.testid.startswith("input-boolean-"):
            prop = self.testid[len("input-boolean-"):]
            return f'Toggle "{prop}" boolean'
        if self.testid.startswith("input-select-"):
            prop = self.testid[len("input-select-"):]
            return f'Edit "{prop}" field'
        if self.testid.startswith("expand-"):
            prop = self.testid[len("expand-"):]
            return f'Expand "{prop}" node to reveal children'
        if self.testid.startswith("collapse-"):
            prop = self.testid[len("collapse-"):]
            return f'Collapse "{prop}" node'
        if self.testid.startswith("add-item-"):
            prop = self.testid[len("add-item-"):]
            return f'Add new item to "{prop}" array'
        if self.testid.startswith("add-property-"):
            suffix = self.testid[len("add-property-"):]
            return f'Add new property to "{suffix}" object' if suffix else \
                   'Add new property to current object (root of current view)'
        if self.testid.startswith("rename-property-"):
            prop = self.testid[len("rename-property-"):]
            return f'Rename "{prop}" property (click pencil icon)'
        if self.testid.startswith("remove-property-"):
            prop = self.testid[len("remove-property-"):]
            return f'Remove "{prop}" property'
        if self.testid == "mode-active-false":
            return "Switch editor mode (Data ↔ Schema)"
        if self.testid == "format-selector":
            return "Change data format (JSON / YAML / XML)"
        return self.testid


# ---------------------------------------------------------------------------
# DOMDiscoverer
# ---------------------------------------------------------------------------

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
    # "mode-active-",
    "format-selector",
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


class DOMDiscoverer:

    def __init__(self, page: Page):
        self.page = page

    def discover(self) -> list[ElementDescriptor]:
        raw: list[dict] = self.page.evaluate("""
            () => {
                const skip = [
                    'current-path', 'submit-', 'current-selected', 'test-component',
                    'data-input', 'schema-input', 'current-path-input',
                    'current-selected-element-input', 'property-metadata-',
                    'required-star', 'validation-error-icon',
                ];

                function resolveAddPropertyLabel(el) {
                    const row = el.closest('tr');
                    if (!row) return '';
                    const currentLevel = parseInt(row.getAttribute('aria-level') || '1');
                    const parentLevel = currentLevel - 1;
                    let prev = row.previousElementSibling;
                    while (prev) {
                        const prevLevel = parseInt(prev.getAttribute('aria-level') || '1');
                        const label = prev.querySelector('[id^="_label_"]');
                        if (prevLevel === parentLevel && label) {
                            return label.id.replace('_label_', '');
                        }
                        prev = prev.previousElementSibling;
                    }
                    return '';
                }

                return [...document.querySelectorAll('[data-testid]')]
                    .filter(el => {
                        if (el.offsetParent === null) return false;
                        const testid = el.getAttribute('data-testid') || '';
                        for (const s of skip) {
                            if (testid.startsWith(s) || testid === s.replace(/-$/, '')) {
                                return false;
                            }
                        }
                        if (testid.startsWith('input-select-')) {
                            if (testid.startsWith('input-select-guiEditor.')) return false;
                            if (testid.startsWith('input-select-textEditor.')) return false;
                            if (testid.startsWith('input-select-schemaDiagram.')) return false;
                            const isInputItself = el.tagName === 'INPUT' && (el.type === 'text' || !el.type);
                            return isInputItself || el.querySelector('[role="combobox"], [role="spinbutton"], .p-selectbutton, [role="textbox"], input[type="text"], input:not([type])') !== null;
                        }
                        // Skip rename/remove/add with empty path suffix
                        if ((testid === 'rename-property-') ||
                            (testid === 'remove-property-') ||
                            (testid === 'add-property-')) return false;
                        return true;
                    })
                    .map(el => {
                        let testid = el.getAttribute('data-testid');
                        if (testid === 'add-property-') {
                            const parentPath = resolveAddPropertyLabel(el);
                            testid = 'add-property-' + parentPath;
                        }
                        return {
                            testid: el.getAttribute('data-testid'),  // ← use getAttribute directly, not the variable
                            tag:    el.tagName.toLowerCase(),
                            role:   el.getAttribute('role') || '',
                            value:  el.value || el.innerText?.slice(0, 40) || '',
                        };
                    });
            }
        """)
        # # Debug — find all rename elements including ones being filtered
        # debug_renames = self.page.evaluate("""
        #     () => [...document.querySelectorAll('[data-testid^="rename-property-"]')]
        #         .map(el => ({
        #             testid: el.getAttribute('data-testid'),
        #             visible: el.offsetParent !== null,
        #             tag: el.tagName.toLowerCase()
        #         }))
        # """)
        # for r in debug_renames:
        #     print(f"   [rename-debug] {r['testid']} visible={r['visible']} tag={r['tag']}")

        expand_raw: list[dict] = self.page.evaluate("""
            () => {
                return [...document.querySelectorAll('button.p-treetable-node-toggle-button')]
                    .filter(btn => btn.style.visibility !== 'hidden' && btn.offsetParent !== null)
                    .map(btn => {
                        const row = btn.closest('tr');
                        const label = row?.querySelector('[id^="_label_"]');
                        const path = label?.id?.replace('_label_', '');
                        if (!path || path.includes('..') || path.endsWith('.')) return null;

                        const nextRow = row?.nextElementSibling;
                        const nextToggler = nextRow?.querySelector('button.p-treetable-node-toggle-button');
                        const currentMargin = parseFloat(btn.style.marginLeft) || 0;
                        const nextMargin = nextToggler ? parseFloat(nextToggler.style.marginLeft) || 0 : -1;
                        const isExpanded = nextMargin > currentMargin;

                        return {
                            testid: (isExpanded ? 'collapse-' : 'expand-') + path,
                            tag:    'button',
                            role:   '',
                            value:  '',
                            js_click: `(function() {
                                const label = document.getElementById('_label_' + '${path}');
                                if (!label) return;
                                const row = label.closest('tr');
                                if (!row) return;
                                const btn = row.querySelector('button.p-treetable-node-toggle-button');
                                if (btn) btn.click();
                            })()`
                        };
                    })
                    .filter(x => x !== null);
            }
        """)

        result: list[ElementDescriptor] = []
        for r in raw + expand_raw:
            interaction = _classify(r["testid"])
            if interaction is None:
                continue
            result.append(ElementDescriptor(
                testid=r["testid"],
                tag=r["tag"],
                role=r["role"],
                interaction=interaction,
                label=r["testid"],
                js_click=r.get("js_click"),
            ))

        result = [el for el in result if el.testid != "mode-active-true"]

        seen = set()
        deduped = []
        for el in result:
            if el.testid.startswith("mode-active-"):
                if el.testid in seen:
                    continue
                seen.add(el.testid)
            deduped.append(el)
        return deduped

    def print_discovered(self, elements: list[ElementDescriptor] | None = None):
        els = elements if elements is not None else self.discover()
        sorted_els = sorted(els, key=lambda e: e.category)
        print(f"\nDiscovered {len(els)} interactable elements:")
        current_category = None
        category_labels = {
            "1_expand":    "── Expand / Collapse",
            "2_edit_field":"── Edit Fields",
            "3_field": "── Fields",
            "4_add":       "── Add",
            "5_rename":    "── Rename",
            "6_remove":    "── Remove",
            "7_mode":      "── Mode",
            "8_format":    "── Format",
            "9_other":     "── Other",
        }
        for el in sorted_els:
            if el.category != current_category:
                print(f"\n  {category_labels.get(el.category, '── Other')}")
                current_category = el.category
            print(f"  [{el.interaction:6}] {el.human_label}")
            print(f"             → {el.testid}  (tag={el.tag})")
        return els


# ---------------------------------------------------------------------------
# ValueGenerator
# ---------------------------------------------------------------------------

_FAKE_STRINGS = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo",
    "Test_123", "hello world", "foo-bar", "value_42",
    "", "x" * 50,
]

_INVALID_TYPE_STRINGS = [
    "NOT_A_NUMBER", "null", "undefined", "true",
    "<script>alert(1)</script>", "12345678901234567890",
    "  ",
]


class ValueGenerator:

    def __init__(self, schema: dict | None = None):
        self.schema = schema or {}

    def _prop_schema(self, property_name: str) -> dict:
        return self.schema.get("properties", {}).get(property_name, {})

    def for_element(self, el: ElementDescriptor, force_valid: bool | None = None) -> Any:
        # Generate values for both fill and select interactions on string fields
        if el.interaction == "select" and el.testid.startswith("input-select-"):
            prop_name = el.testid[len("input-select-"):]
            prop_schema = self._prop_schema(prop_name)
            if prop_schema.get("type") == "string":
                use_valid = force_valid if force_valid is not None else (random.random() < 0.5)
                return self._string_value(prop_schema, use_valid)
            # For meta-schema string fields with no schema definition (e.g. $id, description, title)
            if el.tag == "input" and not prop_schema:
                use_valid = force_valid if force_valid is not None else (random.random() < 0.5)
                return self._string_value({}, use_valid)
        if el.interaction != "fill":
            return None

        use_valid = force_valid if force_valid is not None else (random.random() < 0.5)
        prop_name   = el.property_name
        prop_schema = self._prop_schema(prop_name) if prop_name else {}

        if el.input_type == "string":
            return self._string_value(prop_schema, use_valid)
        if el.input_type == "number":
            return self._number_value(prop_schema, use_valid)
        if el.input_type == "boolean":
            return self._boolean_value(use_valid)

        return random.choice(_FAKE_STRINGS) if use_valid else random.choice(_INVALID_TYPE_STRINGS)

    def _string_value(self, prop_schema: dict, valid: bool) -> str:
        if not valid:
            return random.choice(_INVALID_TYPE_STRINGS)

        enum_vals = prop_schema.get("enum")
        if enum_vals:
            return random.choice(enum_vals)

        min_len = prop_schema.get("minLength", 1)
        max_len = prop_schema.get("maxLength", 30)
        pattern = prop_schema.get("pattern")

        if pattern:
            return "ValidValue_" + str(random.randint(1, 99))

        length = random.randint(min_len, min(max_len, 20))
        chars = string.ascii_letters + string.digits + "_"
        return "".join(random.choices(chars, k=max(length, 1)))

    def _number_value(self, prop_schema: dict, valid: bool) -> float | str:
        if not valid:
            return random.choice(["NOT_A_NUMBER", "null", "", -999999])

        minimum = prop_schema.get("minimum", prop_schema.get("exclusiveMinimum", 0))
        maximum = prop_schema.get("maximum", prop_schema.get("exclusiveMaximum", 1000))
        minimum = minimum if minimum is not None else 0
        maximum = maximum if maximum is not None else 1000

        if prop_schema.get("type") == "integer":
            return random.randint(int(minimum), int(maximum))

        val = minimum + random.random() * (maximum - minimum)
        return round(val, 2)

    def _boolean_value(self, valid: bool) -> str:
        return random.choice(["true", "false"])

    def select_option(self) -> str:
        return random.choices(["json", "yaml", "xml"], weights=[5, 3, 1])[0]


# ---------------------------------------------------------------------------
# InteractionExecutor
# ---------------------------------------------------------------------------

_FORMAT_OPTIONS = ["json", "yaml", "xml"]


class InteractionExecutor:

    def __init__(self, page: Page):
        self.page = page
        self._last_rename   = ""
        self._current_schema: dict | None = None

    def execute(self, el: ElementDescriptor, value: Any = None,
                schema: dict | None = None) -> None:
        self._current_schema = schema

        if el.interaction == "fill":
            self._fill(el, value)
        elif el.interaction == "click":
            self._click(el, value)
        elif el.interaction == "select":
            self._select(el, value)
        else:
            raise ValueError(f"Unknown interaction type '{el.interaction}' for {el.testid}")

        self.page.wait_for_timeout(400)

    def _get_prop_schema(self, testid: str) -> dict:
        """
        Look up the schema for a property given its full path testid
        e.g. 'input-select-Vehicles.bus1.DrivingSpeed' → DrivingSpeed schema
        """
        schema = self._current_schema or {}
        if not schema:
            return {}

        # Strip the prefix to get the dot-path
        for prefix in ("input-select-", "input-string-", "input-number-"):
            if testid.startswith(prefix):
                path = testid[len(prefix):]
                break
        else:
            return {}

        parts = path.replace("[", ".").replace("]", "").split(".")
        current = schema
        for part in parts:
            if not part:
                continue
            # Try properties first, then additionalProperties
            if "properties" in current and part in current["properties"]:
                current = current["properties"][part]
            elif "additionalProperties" in current and isinstance(current["additionalProperties"], dict):
                current = current["additionalProperties"]
                # Now navigate into this schema's properties
                if "properties" in current and part in current["properties"]:
                    current = current["properties"][part]
            elif "items" in current and isinstance(current["items"], dict):
                current = current["items"]
                if "properties" in current and part in current["properties"]:
                    current = current["properties"][part]
            else:
                return {}
        return current if isinstance(current, dict) else {}

    # ------------------------------------------------------------------
    # Schema-aware name generator
    # ------------------------------------------------------------------

    def _generate_property_name(self, parent_path: str) -> str:
        """
        Generate a valid property name by reading the propertyNames pattern
        from the parent object's schema.
        Falls back to 6 random lowercase letters when schema is unavailable.
        """
        schema = self._current_schema or {}
        if not parent_path:
            # Top-level property — check root schema's propertyNames
            prop_names_schema = schema.get("propertyNames", {})
            pattern = prop_names_schema.get("pattern", "")
        else:
            # Navigate to the parent schema
            parts = parent_path.replace("[", ".").replace("]", "").split(".")
            parent_schema = schema
            for part in parts:
                if not part:
                    continue
                parent_schema = (
                        parent_schema.get("properties", {}).get(part) or
                        parent_schema.get("additionalProperties") or
                        {}
                )
            prop_names_schema = parent_schema.get("propertyNames", {})
            pattern = prop_names_schema.get("pattern", "")

        if not pattern:
            return "".join(random.choices(string.ascii_lowercase, k=6))

        # Generate based on detected pattern structure
        has_upper = "A-Z" in pattern
        has_lower = "a-z" in pattern

        if has_upper and has_lower:
            upper_pos = pattern.index("A-Z")
            lower_pos = pattern.index("a-z")
            if upper_pos < lower_pos:
                # Capital first, then lowercase (e.g. Waypoints: ^[A-Z][a-z]+[0-9]*$)
                first = random.choice(string.ascii_uppercase)
                rest = "".join(random.choices(string.ascii_lowercase, k=5))
                return first + rest
        if has_lower:
            # All lowercase (e.g. Vehicles: ^[a-z]+[0-9]*$)
            return "".join(random.choices(string.ascii_lowercase, k=6))

        # Unknown pattern — safe fallback
        return "".join(random.choices(string.ascii_lowercase, k=6))
    # ------------------------------------------------------------------
    # fill
    # ------------------------------------------------------------------

    def _fill(self, el: ElementDescriptor, value: Any) -> None:
        if el.input_type == "boolean":
            btn_label = str(value).lower() if value is not None else "true"
            container = self.page.get_by_test_id(el.testid)
            btn = container.get_by_role("button", name=btn_label)
            btn.click()
            return

        locator = self.page.get_by_test_id(el.testid)

        # For numeric testids (array items), use .first to avoid strict mode violation
        if el.property_name and el.property_name.isdigit():
            locator = locator.first

        try:
            current = locator.input_value(timeout=2000)
            print(f"   field '{el.testid}': '{current}' → '{value}'")
        except Exception:
            print(f"   field '{el.testid}': (unreadable) → '{value}'")

        locator.click()
        locator.fill(str(value) if value is not None else "")
        locator.press("Enter")

    def _rename_property(self, el: ElementDescriptor, forced_name: str | None = None) -> None:
        path = el.testid[len("rename-property-"):]
        label_id = f"_label_{path}"
        # Get parent path for schema-aware name generation
        # e.g. "Vehicles.car1" → "Vehicles", "Waypoints.Gamma" → "Waypoints"

        parts = path.split(".")
        parent_path = ".".join(parts[:-1]) if len(parts) > 1 else ""
        new_name = forced_name if forced_name else self._generate_property_name(parent_path)

        # Click the pencil icon
        pencil = self.page.get_by_test_id(el.testid)
        pencil.scroll_into_view_if_needed()
        pencil.hover()
        self.page.wait_for_timeout(100)
        pencil.click(timeout=5000)
        self.page.wait_for_timeout(200)

        # After clicking pencil, the label becomes contenteditable
        # Find it directly rather than by id to avoid strict mode violations
        try:
            self.page.wait_for_selector(
                f'[id="{label_id}"][contenteditable="true"]', timeout=2000
            )
            label = self.page.locator(f'[id="{label_id}"][contenteditable="true"]')
        except Exception:
            # Fallback to first match
            label = self.page.locator(f'[id="{label_id}"]').first

        label.click()
        self.page.keyboard.press("Control+a")
        self.page.keyboard.type(new_name)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(300)

        self._last_rename = new_name
        print(f"   renamed '{path}' → '{new_name}'")

    def _click_add_item_required(self) -> None:
        self.page.get_by_test_id("add-item-required").first.click()
        self.page.wait_for_timeout(600)

        # Find all required[N] inputs and locate the empty one
        inputs = self.page.evaluate("""
            () => [...document.querySelectorAll('[data-testid^="input-select-required["]')]
                .map(el => ({testid: el.getAttribute('data-testid'), value: el.value}))
        """)
        print(f"   [add-item-required] inputs found: {inputs}")

        empty_input = next((i for i in inputs if i["value"] == ""), None)
        if empty_input:
            inp = self.page.get_by_test_id(empty_input["testid"])
            name = self._generate_property_name("")
            print(f"   [add-item-required] filling '{empty_input['testid']}' with '{name}'")
            inp.click()
            inp.fill(name)
            inp.press("Tab")
            self.page.wait_for_timeout(400)
        else:
            print("   [add-item-required] ⚠️ no empty required[N] input found")

    # ------------------------------------------------------------------
    # click
    # ------------------------------------------------------------------

    def _click(self, el: ElementDescriptor, value: Any = None) -> None:
        if el.js_click:
            self.page.evaluate(el.js_click)
            return

        if el.testid.startswith("add-property-") and el.testid != "add-property-":
            self._click_add_property(el, forced_name=value)
            return
        if el.testid.startswith("rename-property-"):
            self._rename_property(el, forced_name=value)
            return

        if el.testid == "add-item-required":
            self._click_add_item_required()
            return

        self.page.get_by_test_id(el.testid).first.click()

    def _click_add_property(self, el: ElementDescriptor, forced_name: str | None = None) -> None:
        print(f"   [add-property-debug] testid={el.testid} forced_name={forced_name!r}")
        parent_path = el.testid[len("add-property-"):]

        self.page.evaluate(f"""
            () => {{
                const spans = [...document.querySelectorAll('[data-testid="add-property-"]')];
                for (const span of spans) {{
                    const row = span.closest('tr');
                    if (!row) continue;
                    let contextDepth = -1;
                    let p = row.previousElementSibling;
                    while (p) {{
                        const tog = p.querySelector('button.p-treetable-node-toggle-button');
                        if (tog) {{ contextDepth = parseFloat(tog.style.marginLeft) || 0; break; }}
                        p = p.previousElementSibling;
                    }}
                    let prev = row.previousElementSibling;
                    while (prev) {{
                        const toggler = prev.querySelector('button.p-treetable-node-toggle-button');
                        const label   = prev.querySelector('[id^="_label_"]');
                        if (toggler && label) {{
                            const prevMargin = parseFloat(toggler.style.marginLeft) || 0;
                            if (prevMargin < contextDepth) {{
                                const resolvedPath = label.id.replace('_label_', '');
                                if (resolvedPath === '{parent_path}') {{
                                    span.click();
                                    return;
                                }}
                                break;
                            }}
                        }}
                        prev = prev.previousElementSibling;
                    }}
                }}
            }}
        """)

        self.page.wait_for_timeout(400)

        new_name = forced_name if forced_name else self._generate_property_name(parent_path)
        print(f"   [name-gen] path='{parent_path}' → '{new_name}'")

        # Wait for contenteditable to actually appear
        try:
            self.page.wait_for_selector('[contenteditable="true"]', timeout=3000)
        except Exception:
            print("   [name-gen] contenteditable never appeared — name may not be set")

        self.page.evaluate("""
            () => {
                const editables = [...document.querySelectorAll('[contenteditable="true"]')];
                const active = editables[editables.length - 1];
                if (active) {
                    active.focus();
                    document.execCommand('selectAll', false, null);
                }
            }
        """)
        self.page.wait_for_timeout(200)
        self.page.keyboard.type(new_name)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(300)
        print(f"   new property named '{new_name}'")

    # ------------------------------------------------------------------
    # select
    # ------------------------------------------------------------------

    def _select(self, el: ElementDescriptor, value: Any = None) -> None:
        if el.testid == "format-selector":
            self._select_format(value)
        elif el.testid.startswith("input-select-"):
            self._select_dropdown(el, value)
        else:
            self.page.get_by_test_id(el.testid).first.click()

    def _select_dropdown(self, el: ElementDescriptor, value: Any = None) -> None:
        container   = self.page.get_by_test_id(el.testid).first
        spinbutton  = container.get_by_role("spinbutton")
        selectbutton= container.locator(".p-selectbutton")
        combobox    = container.get_by_role("combobox")

        if spinbutton.count() > 0:
            prop_schema = self._get_prop_schema(el.testid)
            self._fill_number(el, spinbutton, prop_schema)
        elif selectbutton.count() > 0:
            self._fill_boolean(selectbutton)
        elif combobox.count() > 0:
            self._fill_enum(combobox)
        else:
            # Check if the element itself is a text input (not a wrapper)
            tag = self.page.get_by_test_id(el.testid).first.evaluate("el => el.tagName.toLowerCase()")
            if tag == "input":
                # Element is the input itself — use it directly
                self._fill_string(el, self.page.get_by_test_id(el.testid).first, value)
            else:
                textbox = container.get_by_role("textbox")
                if textbox.count() > 0:
                    self._fill_string(el, textbox, value)
                else:
                    self.page.keyboard.press("Escape")

    def _fill_number(self, el: ElementDescriptor, spinbutton, prop_schema: dict = None) -> None:
        prop_schema = prop_schema or {}
        minimum = prop_schema.get("minimum", 0)
        maximum = prop_schema.get("maximum", 100)
        multiple_of = prop_schema.get("multipleOf")
        is_integer = prop_schema.get("type") == "integer"
        use_valid = random.random() < 0.5

        if not use_valid:
            invalid_choices = [
                maximum + 100,
                minimum - 100,
                round(random.uniform(maximum, maximum * 2), 2),
            ]
            if multiple_of:
                invalid_choices.append(multiple_of * random.randint(1, 10) + 0.5)
            value = random.choice(invalid_choices)
        elif multiple_of:
            min_mult = int(minimum / multiple_of) + (1 if minimum % multiple_of else 0)
            max_mult = int(maximum / multiple_of)
            value = random.randint(min_mult, max_mult) * multiple_of if min_mult <= max_mult else multiple_of
        elif is_integer:
            value = random.randint(int(minimum), int(maximum))
        else:
            value = round(random.uniform(minimum, maximum), 2)

        # Write the value to the spinbutton
        try:
            current = spinbutton.input_value(timeout=2000)
            print(f"   field '{el.testid}': '{current}' → '{value}'")
        except Exception:
            print(f"   field '{el.testid}': (unreadable) → '{value}'")
        spinbutton.click()
        spinbutton.fill(str(value))
        spinbutton.press("Enter")


    def _fill_boolean(self, selectbutton) -> None:
        choice = random.choice(["true", "false"])
        print(f"   boolean → '{choice}'")
        btn = selectbutton.get_by_role("button", name=choice)
        if btn.count() > 0:
            btn.first.click()

    def _fill_enum(self, combobox) -> None:
        combobox.first.click()
        self.page.wait_for_timeout(300)
        options = self.page.locator(".p-select-list .p-select-option")
        count = options.count()
        if count > 0:
            options.nth(random.randint(0, count - 1)).click()
        else:
            self.page.keyboard.press("Escape")

    def _fill_string(self, el: ElementDescriptor, textbox, value: Any) -> None:
        try:
            current = textbox.input_value(timeout=2000)
            print(f"   field '{el.testid}': '{current}' → '{value}'")
        except Exception:
            print(f"   field '{el.testid}': (unreadable) → '{value}'")

        # Try Playwright fill first
        textbox.click()
        textbox.press("Control+a")
        textbox.fill(str(value) if value is not None else "")
        textbox.press("Tab")
        self.page.wait_for_timeout(300)

        # Verify — if value didn't commit, use native event dispatch
        try:
            after = textbox.input_value(timeout=1000)
            if after != str(value):
                print(f"   [fill-retry] Tab didn't commit — trying Enter")
                textbox.click()
                textbox.press("Control+a")
                textbox.fill(str(value) if value is not None else "")
                textbox.press("Enter")
                self.page.wait_for_timeout(300)
        except Exception:
            pass

    def _select_format(self, format_value: Any = None) -> None:
        selector = self.page.get_by_test_id("format-selector")
        try:
            current = selector.inner_text().lower().strip()
            for fmt in _FORMAT_OPTIONS:
                if fmt in current:
                    current = fmt
                    break
            else:
                current = "json"
        except Exception:
            current = "json"

        if format_value is None:
            choices = [f for f in _FORMAT_OPTIONS if f != current]
            format_value = random.choice(choices)

        if format_value == current:
            return

        selector.get_by_role("combobox").click()
        self.page.get_by_role("option", name=format_value).click()
        self.page.wait_for_timeout(300)