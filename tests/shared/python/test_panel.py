"""
test_panel.py
=============
Mirrors: tests/shared/utilsTestPanel.ts

The test panel is MetaConfigurator's internal state inspector.
It exposes raw data, schema, currentPath, and currentSelectedElement
for both DataEditor and SchemaEditor modes.

This is the ground truth for all invariant checks.
Every invariant reads from here before looking at any view.
"""

from __future__ import annotations
import json
from typing import Any
from playwright.sync_api import Page

from tests.shared.python.utils import SessionMode, path_to_json_pointer, json_pointer_to_path


# ---------------------------------------------------------------------------
# TestPanel class
# ---------------------------------------------------------------------------

class TestPanel:
    """
    Python mirror of utilsTestPanel.ts.

    Wraps all read/write operations on MetaConfigurator's test panel.
    The test panel data-testid structure is:

        [data-testid="test-component-{mode}"]
            [data-testid="data"]                  ← raw data JSON
            [data-testid="schema"]                ← raw schema JSON
            [data-testid="current-path"]          ← JSON pointer (hidden at root)
            [data-testid="current-selected-element"] ← JSON pointer (hidden at root)

            [data-testid="data-input"]            ← write data
            [data-testid="submit-data"]
            [data-testid="schema-input"]          ← write schema
            [data-testid="submit-schema"]
            [data-testid="current-path-input"]    ← write path
            [data-testid="submit-current-path"]
            [data-testid="current-selected-element-input"]  ← write selected
            [data-testid="submit-current-selected-element"]
    """

    def __init__(self, page: Page, mode: SessionMode = SessionMode.DataEditor):
        self.page = page
        self.mode = mode

    def _component(self):
        """Locate the test panel component for this mode."""
        tc = self.page.get_by_test_id(f"test-component-{self.mode.value}")
        tc.wait_for(state="visible", timeout=5000)
        return tc

    # ------------------------------------------------------------------
    # Read operations (ground truth)
    # ------------------------------------------------------------------

    def get_data(self) -> Any:
        """
        Read the raw internal data object.
        Mirrors: tpGetData() in utilsTestPanel.ts
        """
        el = self._component().get_by_test_id("data")
        el.wait_for(state="visible", timeout=5000)
        content = el.text_content()
        if content is None:
            raise RuntimeError("Test panel data element returned null")
        return json.loads(content)

    def get_schema(self) -> Any:
        """
        Read the raw internal schema object.
        Mirrors: tpGetSchema() in utilsTestPanel.ts
        """
        el = self._component().get_by_test_id("schema")
        el.wait_for(state="visible", timeout=5000)
        content = el.text_content()
        if content is None:
            raise RuntimeError("Test panel schema element returned null")
        return json.loads(content)

    def get_current_path(self, expect_non_root: bool = True) -> list:
        """
        Read the current navigation path.
        Returns [] when at root (element is hidden at root, matching TS behaviour).
        Mirrors: tpGetCurrentPath() in utilsTestPanel.ts
        """
        el = self._component().get_by_test_id("current-path")
        if expect_non_root:
            el.wait_for(state="visible", timeout=3000)
            raw = el.text_content() or ""
            return json_pointer_to_path(raw)
        else:
            # At root the element is hidden — return []
            if el.is_visible():
                raw = el.text_content() or ""
                return json_pointer_to_path(raw)
            return []

    def get_current_selected_element(self, expect_non_root: bool = True) -> list:
        """
        Read the currently selected element path.
        Mirrors: tpGetCurrentSelectedElement() in utilsTestPanel.ts
        """
        el = self._component().get_by_test_id("current-selected-element")
        if expect_non_root:
            el.wait_for(state="visible", timeout=3000)
            raw = el.text_content() or ""
            return json_pointer_to_path(raw)
        else:
            if el.is_visible():
                raw = el.text_content() or ""
                return json_pointer_to_path(raw)
            return []

    # ------------------------------------------------------------------
    # Write operations (test setup / mutation seeding)
    # ------------------------------------------------------------------

    def force_data(self, data: Any):
        """
        Overwrite the internal data object.
        Mirrors: tpForceData() in utilsTestPanel.ts
        """
        tc = self._component()
        inp = tc.get_by_test_id("data-input")
        inp.wait_for(state="visible", timeout=3000)
        inp.fill(json.dumps(data))
        inp.dispatch_event("change")
        tc.get_by_test_id("submit-data").click()
        self.page.wait_for_timeout(300)

    def force_schema(self, schema: Any):
        """
        Overwrite the internal schema object.
        Mirrors: tpForceSchema() in utilsTestPanel.ts
        """
        tc = self._component()
        inp = tc.get_by_test_id("schema-input")
        inp.wait_for(state="visible", timeout=3000)
        inp.fill(json.dumps(schema))
        inp.dispatch_event("change")
        tc.get_by_test_id("submit-schema").click()
        self.page.wait_for_timeout(300)

    def force_current_path(self, path: list):
        """
        Set the current navigation path.
        Mirrors: tpForceCurrentPath() in utilsTestPanel.ts
        """
        tc = self._component()
        inp = tc.get_by_test_id("current-path-input")
        inp.wait_for(state="visible", timeout=3000)
        inp.fill(path_to_json_pointer(path))
        inp.dispatch_event("change")
        tc.get_by_test_id("submit-current-path").click()
        self.page.wait_for_timeout(300)

    def force_current_selected_element(self, path: list):
        """
        Set the currently selected element.
        Mirrors: tpForceCurrentSelectedElement() in utilsTestPanel.ts
        """
        tc = self._component()
        inp = tc.get_by_test_id("current-selected-element-input")
        inp.wait_for(state="visible", timeout=3000)
        inp.fill(path_to_json_pointer(path))
        inp.dispatch_event("change")
        tc.get_by_test_id("submit-current-selected-element").click()
        self.page.wait_for_timeout(300)


# ---------------------------------------------------------------------------
# Module-level convenience functions
# (mirrors the standalone exported functions in utilsTestPanel.ts)
# ---------------------------------------------------------------------------

def tp_get_data(page: Page, mode: SessionMode = SessionMode.DataEditor) -> Any:
    return TestPanel(page, mode).get_data()

def tp_get_schema(page: Page, mode: SessionMode = SessionMode.DataEditor) -> Any:
    return TestPanel(page, mode).get_schema()

def tp_get_current_path(page: Page, mode: SessionMode = SessionMode.DataEditor,
                        expect_non_root: bool = True) -> list:
    return TestPanel(page, mode).get_current_path(expect_non_root)

def tp_get_current_selected_element(page: Page, mode: SessionMode = SessionMode.DataEditor,
                                    expect_non_root: bool = True) -> list:
    return TestPanel(page, mode).get_current_selected_element(expect_non_root)

def tp_force_data(page: Page, data: Any, mode: SessionMode = SessionMode.DataEditor):
    TestPanel(page, mode).force_data(data)

def tp_force_schema(page: Page, schema: Any, mode: SessionMode = SessionMode.DataEditor):
    TestPanel(page, mode).force_schema(schema)

def tp_force_current_path(page: Page, path: list, mode: SessionMode = SessionMode.DataEditor):
    TestPanel(page, mode).force_current_path(path)

def tp_force_current_selected_element(page: Page, path: list,
                                      mode: SessionMode = SessionMode.DataEditor):
    TestPanel(page, mode).force_current_selected_element(path)
