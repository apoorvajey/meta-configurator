"""
utils.py
========
Mirrors: tests/shared/utils.ts

App-level helpers: opening the app, switching modes, data formats,
schema selection, and path conversion utilities.

IMPORTANT: This file must only import from stdlib and Playwright.
Never import from anywhere inside the tests/ folder.
"""

from __future__ import annotations
from enum import Enum
import platform
from playwright.sync_api import Page


# ---------------------------------------------------------------------------
# Fixture resolution — local dev server vs. remote/live deployments
# ---------------------------------------------------------------------------
#
# On localhost:5173 (vite dev server), fixture files under test-fixtures/
# are served directly by the dev server config, so a relative path works.
#
# On any non-local base_url (e.g. https://www.metaconfigurator.org), the
# deployed build does NOT ship the e2e/test-fixtures/ directory — only
# public/ is bundled into dist/. A relative "test-fixtures/..." path 404s
# there, which silently breaks FetchView's settings/data/schema loading
# (it swallows the fetch failure and falls through to the default view
# with no error surfaced). The fix: use an absolute URL to the fixture
# file's raw location in the public GitHub repo instead, since
# fetchExternalContent() does a plain fetch() against whatever URL it's
# given (and even special-cases github.com/blob URLs).
#
# NOTE: REMOTE_FIXTURES_REF pins to a branch by default, which means
# remote/live-mode runs track whatever is currently on that branch, not
# necessarily what's actually deployed at the live URL — those can drift
# out of sync. Pin this to a specific commit hash instead of a branch
# name for reproducible runs.
REMOTE_FIXTURES_REF = "develop"
REMOTE_FIXTURES_BASE_URL = (
    f"https://raw.githubusercontent.com/MetaConfigurator/meta-configurator/"
    f"{REMOTE_FIXTURES_REF}/meta_configurator/e2e/test-fixtures"
)


def _is_local_base_url(base_url: str) -> bool:
    return "localhost" in base_url or "127.0.0.1" in base_url


def _fixtures_base_url(base_url: str) -> str:
    """Relative path for local dev server; absolute GitHub raw URL otherwise."""
    if _is_local_base_url(base_url):
        return "test-fixtures"
    return REMOTE_FIXTURES_BASE_URL


# ---------------------------------------------------------------------------
# SessionMode enum
# ---------------------------------------------------------------------------

class SessionMode(str, Enum):
    DataEditor   = "dataEditor"
    SchemaEditor = "schemaEditor"
    Settings     = "settings"


MODE_MENU_TITLES = {
    SessionMode.DataEditor:   "Data",
    SessionMode.SchemaEditor: "Schema",
    SessionMode.Settings:     "Settings",
}


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

LIVE_BASE_URL = "https://www.metaconfigurator.org"


def open_app(
    page: Page,
    initial_settings: str | None = None,
    initial_data: str | None = None,
    initial_schema: str | None = None,
    base_url: str = LIVE_BASE_URL,
):
    from urllib.parse import urlencode
    test_files_path = _fixtures_base_url(base_url)
    params: dict[str, str] = {}
    if initial_settings:
        params["settings"] = f"{test_files_path}/{initial_settings}"
    else:
        params["settings"] = f"{test_files_path}/settings_no_news.json"
    if initial_data:
        params["data"] = f"{test_files_path}/{initial_data}"
    if initial_schema:
        params["schema"] = f"{test_files_path}/{initial_schema}"
    url = f"{base_url}/?{urlencode(params)}"
    print(f"Opening: {url}")
    page.goto(url)
    page.wait_for_load_state("networkidle")


def open_app_with_test_panel(
    page: Page,
    initial_settings: str | None = "settings_testpanel.json",
    initial_data: str | None = None,
    initial_schema: str | None = None,
    base_url: str = LIVE_BASE_URL,
):
    open_app(page, initial_settings, initial_data, initial_schema, base_url)


def setup_test_panel_via_localstorage(page: Page, base_url: str = LIVE_BASE_URL):
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.evaluate("""
        const settings = JSON.parse(localStorage.getItem('settingsData') || '{}');
        if (settings.panels) {
            settings.panels.hidden = (settings.panels.hidden || [])
                .filter(h => h !== 'test');
            const hasTest = settings.panels.dataEditor
                .some(p => p.panelType === 'test');
            if (!hasTest) {
                settings.panels.dataEditor.push({
                    panelType: 'test', mode: 'dataEditor', size: 50
                });
                settings.panels.schemaEditor.push({
                    panelType: 'test', mode: 'schemaEditor', size: 33
                });
            }
        }
        localStorage.setItem('settingsData', JSON.stringify(settings));
    """)
    page.reload()
    page.wait_for_load_state("networkidle")
    print("✓ Test panel enabled via localStorage")


# ---------------------------------------------------------------------------
# Mode switching
# ---------------------------------------------------------------------------

def get_current_editor_mode(page: Page) -> SessionMode:
    active_button = page.get_by_test_id("mode-active-true")
    text = active_button.inner_text()
    for mode, title in MODE_MENU_TITLES.items():
        if title in text:
            return mode
    raise RuntimeError(f"Unable to detect editor mode from button text: '{text}'")


def force_editor_mode(page: Page, new_mode: SessionMode):
    current = get_current_editor_mode(page)
    if current == new_mode:
        return
    if new_mode == SessionMode.Settings:
        page.get_by_test_id("mode-settings-button").click()
    else:
        title = MODE_MENU_TITLES[new_mode]
        page.locator("a").filter(has_text=title).click()
    page.wait_for_timeout(500)


# ---------------------------------------------------------------------------
# Data format switching
# ---------------------------------------------------------------------------

DATA_FORMATS = ["json", "yaml"]


def get_current_data_format(page: Page) -> str:
    text = page.get_by_test_id("format-selector").inner_text().lower()
    for fmt in DATA_FORMATS:
        if fmt in text:
            return fmt
    raise RuntimeError(f"Unable to detect data format from: '{text}'")


def force_data_format(page: Page, new_format: str):
    current = get_current_data_format(page)
    if current == new_format:
        return
    selector = page.get_by_test_id("format-selector")
    selector.get_by_role("combobox", name=current).click()
    page.get_by_role("option", name=new_format).click()
    page.wait_for_timeout(300)


# ---------------------------------------------------------------------------
# Schema selection
# ---------------------------------------------------------------------------

def select_initial_schema_from_examples(page: Page, schema_name: str):
    page.get_by_text("Select a Schema").wait_for(state="visible")
    page.get_by_role("button", name="Example Schema").click()
    page.get_by_role("option", name=schema_name).click()
    page.wait_for_timeout(800)

# ---------------------------------------------------------------------------
# Dialog helpers
# ---------------------------------------------------------------------------

def dismiss_dialog_if_present(page: Page):
    try:
        btn = page.get_by_role("button", name="Close")
        if btn.is_visible(timeout=3000):
            btn.click()
            page.wait_for_timeout(300)
            print("✓ Dialog dismissed")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Path / JSON Pointer utilities
# ---------------------------------------------------------------------------

def path_to_string(path: list) -> str:
    return ".".join(str(p) for p in path)


def path_to_json_pointer(path: list) -> str:
    if not path:
        return ""
    return "/" + "/".join(str(p) for p in path)


def json_pointer_to_path(pointer: str) -> list:
    if not pointer or pointer in ("/", ""):
        return []
    return pointer.lstrip("/").split("/")


# ---------------------------------------------------------------------------
# Keyboard helpers
# ---------------------------------------------------------------------------

def select_all(page: Page):
    modifier = "Meta" if platform.system() == "Darwin" else "Control"
    page.keyboard.press(f"{modifier}+A")