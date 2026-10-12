"""InteractionExecutor: performs a discovered action on the page; the actions live in this package's mixins."""

from __future__ import annotations

import json
import random
import re
from typing import Any
from playwright.sync_api import Page
from tests.mutation_testing.discovery.element import ElementDescriptor
from tests.mutation_testing.actions.gui_actions import GuiActionsMixin
from tests.mutation_testing.actions.text_view_add import TextViewAddMixin
from tests.mutation_testing.actions.text_view_modify import TextViewModifyMixin
from tests.mutation_testing.actions.text_view_primitives import TextViewPrimitivesMixin
from tests.mutation_testing.actions.text_view_remove import TextViewRemoveMixin
from tests.mutation_testing.discovery.values import _WORD_BANK
from tests.mutation_testing.discovery.paths import path_to_testid_string


class InteractionExecutor(GuiActionsMixin, TextViewPrimitivesMixin, TextViewAddMixin, TextViewModifyMixin, TextViewRemoveMixin):

    def __init__(self, page: Page):
        self.page = page
        self._last_rename   = ""
        self._current_schema: dict | None = None
        self._silent_no_toggle_count = 0  # ancestors without a toggle button, reported in the run summary
        # (parent_path, name) of the property just created via GUI "add property", for create-then-fill.
        # Reset at the start of every _click_add_property so a stale name is never mistaken for a new one.
        self._last_added_property_parent_path = ""
        self._last_added_property_name = ""

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
        """The schema of a property given its full-path testid, e.g. 'input-select-Vehicles.bus1.DrivingSpeed'."""
        schema = self._current_schema or {}
        if not schema:
            return {}

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
            if "properties" in current and part in current["properties"]:
                current = current["properties"][part]
            elif "additionalProperties" in current and isinstance(current["additionalProperties"], dict):
                current = current["additionalProperties"]
                if "properties" in current and part in current["properties"]:
                    current = current["properties"][part]
            elif "items" in current and isinstance(current["items"], dict):
                current = current["items"]
                if "properties" in current and part in current["properties"]:
                    current = current["properties"][part]
            else:
                return {}
        return current if isinstance(current, dict) else {}

    def _generate_property_name(self, parent_path: str) -> str:
        """A property name shaped by the parent's propertyNames pattern (capital-first or lowercase), from the shared
        word bank. The casing is a heuristic on the pattern text ("A-Z", "a-z"); it does not verify that the name
        satisfies the pattern or, for add-item-required, that it is a property of the schema.
        """
        schema = self._current_schema or {}
        if not parent_path:
            prop_names_schema = schema.get("propertyNames", {})
            pattern = prop_names_schema.get("pattern", "")
        else:
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

        word = random.choice(_WORD_BANK)

        if not pattern:
            return word.lower()

        has_upper = "A-Z" in pattern
        has_lower = "a-z" in pattern

        if has_upper and has_lower:
            upper_pos = pattern.index("A-Z")
            lower_pos = pattern.index("a-z")
            if upper_pos < lower_pos:
                # Capital first, then lowercase (e.g. Waypoints: ^[A-Z][a-z]+[0-9]*$)
                return word[0].upper() + word[1:].lower()
        if has_lower:
            # All lowercase (e.g. Vehicles: ^[a-z]+[0-9]*$)
            return word.lower()

        return word.lower()

    # Structural JSON Schema wrappers, never collapsible tree nodes; _ensure_path_visible skips them
    _COMPOSITION_KEYWORDS = {"allOf", "anyOf", "oneOf", "not", "if", "then", "else"}
    _DEFS_PREFIX_RE = re.compile(r"^(\$defs|definitions)\.(.+)$")

    def _split_defs_prefix(self, path: list) -> list:
        """Split a pre-merged first segment ('$defs.stringProperty') into '$defs' and the name. The tree renders $defs
        as its own collapsible row, so it must be expanded as a separate ancestor.
        """
        if path and isinstance(path[0], str):
            m = self._DEFS_PREFIX_RE.match(path[0])
            if m:
                return [m.group(1), m.group(2), *path[1:]]
        return path

    def _ensure_path_visible(self, path: list) -> None:
        path = self._split_defs_prefix(path)
        for i in range(1, len(path)):
            segment = path[i - 1]
            if isinstance(segment, str) and segment in self._COMPOSITION_KEYWORDS:
                continue

            ancestor_str = path_to_testid_string(path[:i])
            if not ancestor_str:
                continue

            state = self.page.evaluate(f"""
                () => {{
                    const label = document.getElementById('_label_' + {json.dumps(ancestor_str)});
                    if (!label) return 'missing';
                    const row = label.closest('tr');
                    if (!row) return 'missing';
                    const btn = row.querySelector('button.p-treetable-node-toggle-button');
                    if (!btn || btn.style.visibility === 'hidden' || btn.offsetParent === null) return 'no-toggle';
                    const nextRow = row.nextElementSibling;
                    const nextToggler = nextRow?.querySelector('button.p-treetable-node-toggle-button');
                    const currentMargin = parseFloat(btn.style.marginLeft) || 0;
                    const nextMargin = nextToggler ? (parseFloat(nextToggler.style.marginLeft) || 0) : -1;
                    return (nextMargin > currentMargin) ? 'expanded' : 'collapsed';
                }}
            """)

            if state == "collapsed":
                self.page.evaluate(f"""
                    () => {{
                        const label = document.getElementById('_label_' + {json.dumps(ancestor_str)});
                        const row = label?.closest('tr');
                        const btn = row?.querySelector('button.p-treetable-node-toggle-button');
                        if (btn) btn.click();
                    }}
                """)
                # Wait for the child row to mount; subtrees under $defs can take longer than a fixed sleep
                next_str = path_to_testid_string(path[:i + 1])
                try:
                    self.page.wait_for_function(
                        f"document.getElementById('_label_' + {json.dumps(next_str)}) !== null",
                        timeout=3000,
                    )
                except Exception:
                    self.page.wait_for_timeout(300)
            elif state == "expanded":
                pass
            elif state == "no-toggle":
                # Not collapsible (expected sometimes, e.g. discovery gaps): tally instead of printing per ancestor
                self._silent_no_toggle_count += 1
            else:  # "missing"
                print(f"   [ensure-visible] ancestor '{ancestor_str}' row not found in DOM "
                      f"(parent not yet expanded, or the path has no row of its own)")
