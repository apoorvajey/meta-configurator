"""Python mirror of utils.ts: opening the app, switching modes and formats, schema selection and path conversion.

Imports only the standard library and Playwright, never anything inside tests/.
"""

from __future__ import annotations
from enum import Enum
import json
import os
import platform
from pathlib import Path
from playwright.sync_api import Page


# Fixture files. The vite dev server (localhost:5173) serves test-fixtures/ itself, so a relative path works.
# Deployed builds do not ship e2e/test-fixtures/, and a relative path would 404 without any error, so the
# files are fetched from the public GitHub repository instead. REMOTE_FIXTURES_REF is a branch by default
# and can drift from the deployed build; pin it to a commit hash for reproducible runs.
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


class SessionMode(str, Enum):
    DataEditor   = "dataEditor"
    SchemaEditor = "schemaEditor"
    Settings     = "settings"


MODE_MENU_TITLES = {
    SessionMode.DataEditor:   "Data",
    SessionMode.SchemaEditor: "Schema",
    SessionMode.Settings:     "Settings",
}


HOSTED_URL = "https://www.metaconfigurator.org"
LOCAL_URL = "http://localhost:5173"
_RUN_CONFIG = Path(__file__).resolve().parents[2] / "mutation_testing" / "config" / "run_config.json"


def configured_local_url() -> str:
    """"local_url" of run_config.json, else http://localhost:5173."""
    try:
        config = json.loads(_RUN_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LOCAL_URL
    return str(config.get("local_url") or LOCAL_URL).strip()


def _app_url() -> str:
    """The app the tests run against. Order: the environment variable METACONFIGURATOR_URL (CI sets it), then "app" in
    run_config.json ("hosted", "local" = "local_url", default http://localhost:5173, or a full URL), else the hosted site."""
    env = os.environ.get("METACONFIGURATOR_URL")
    if env:
        return env
    try:
        config = json.loads(_RUN_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return HOSTED_URL
    app = str(config.get("app") or "hosted").strip()
    if app == "local":
        return configured_local_url()
    if app.startswith(("http://", "https://")):
        return app
    return HOSTED_URL


LIVE_BASE_URL = _app_url()

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


def select_initial_schema_from_examples(page: Page, schema_name: str):
    page.get_by_text("Select a Schema").wait_for(state="visible")
    page.get_by_role("button", name="Example Schema").click()
    page.get_by_role("option", name=schema_name).click()
    page.wait_for_timeout(800)

def dismiss_dialog_if_present(page: Page):
    try:
        btn = page.get_by_role("button", name="Close")
        if btn.is_visible(timeout=3000):
            btn.click()
            page.wait_for_timeout(300)
            print("✓ Dialog dismissed")
    except Exception:
        pass


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


def select_all(page: Page):
    modifier = "Meta" if platform.system() == "Darwin" else "Control"
    page.keyboard.press(f"{modifier}+A")