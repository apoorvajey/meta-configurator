"""Set-up of the app page for a run: dataset choice, page preparation and the state snapshot of one step."""
from __future__ import annotations

import json
import re
from pathlib import Path
from playwright.sync_api import Page
from tests.shared.python.utils import (
    SessionMode,
    dismiss_dialog_if_present,
    setup_test_panel_via_localstorage,
)
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.discovery.page_capture import (
    _get_current_active_mode,
    capture_gui_facts,
    capture_text_view,
)


DATA_DIR = Path(__file__).resolve().parent.parent / "test_data"


# Schema/data pairs for a run: the schema name must match the option in MetaConfigurator's "Example
# Schemas" dropdown, the data file is uploaded in the Text View. run_config.json's "schema" picks one
# (or "random").
SCHEMA_DATA_PAIRS: list[tuple[str, Path]] = [
    ("Autonomous Vehicle Schema", DATA_DIR / "self_driving_vehicle_data.json"),
    ("Feature Testing Schema",    DATA_DIR / "person_data.json"),
    ("Strenda Schema",            DATA_DIR / "strenda_data.json"),
    ("EnzymeML Schema",           DATA_DIR / "enzymeml_data.json"),
]


def _dataset_slug(schema_option_name: str, data_file: str) -> str:
    """Stable identifier of a schema/data pair, stored in every snapshot so inference works per dataset."""
    def _norm(s: str) -> str:
        return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')
    return f"{_norm(schema_option_name)}__{_norm(data_file)}"


def _safe_filename_part(s: str) -> str:
    """Replace characters that are unsafe in file names (used for all screenshot names)."""
    return re.sub(r'[<>:"/\\|?*]', '_', s)[:60]


def _json_safe(value):
    """`value` if json.dumps accepts it (types are kept: 5, True, "", None), else its repr().

    Used for "input_value", the exact value passed to execute(), which replay feeds back unchanged.
    """
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


VIEWPORT = {"width": 1920, "height": 1080}


PAGE_ZOOM = "0.75"


def setup_app_for_schema(page: Page, schema_option_name: str, data_file_name: str) -> None:
    """Load an example schema by its dropdown name, then upload the data file (a name in DATA_DIR) in the Data Editor Text View."""
    page.get_by_role("button", name="Example Schemas").click()
    page.wait_for_timeout(1000)
    page.get_by_role("option", name=schema_option_name).click()
    page.wait_for_timeout(1000)
    print(f"✓ {schema_option_name} loaded")

    page.get_by_text("Data", exact=True).click()
    page.wait_for_timeout(1000)
    page.get_by_text("Text View", exact=True).click()
    page.wait_for_timeout(500)

    with page.expect_file_chooser() as fc_info:
        page.locator("[data-icon='folder-open']").click()
    fc_info.value.set_files(str(DATA_DIR / data_file_name))
    page.wait_for_timeout(1000)
    print("✓ Data file loaded")


def prepare_app_page(page: Page, schema_option_name: str, data_file_name: str) -> None:
    """Test panel and reload, zoom, dismiss dialog, load schema and data. The caller sets the viewport first."""
    setup_test_panel_via_localstorage(page)
    page.wait_for_timeout(2000)
    page.evaluate(f"document.body.style.zoom = '{PAGE_ZOOM}'")
    print("✓ Page loaded")

    dismiss_dialog_if_present(page)
    setup_app_for_schema(page, schema_option_name, data_file_name)


def scroll_to_validation_error(page: Page) -> None:
    """Scroll the first validation-error icon into view before a failing step's screenshot.

    This changes page state, so replay repeats it (see ReplayRunner's mirror_screenshot_scroll).
    """
    try:
        page.evaluate("""
            () => {
                const icon = document.querySelector('[data-testid="validation-error-icon"]');
                if (icon) icon.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        """)
        page.wait_for_timeout(400)
    except Exception:
        pass


def _short(value, limit: int = 80) -> str:
    """Compact one-line JSON form of a value for logging what a step changed."""
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        s = repr(value)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def build_snapshot(page: Page, action: str, step: int,
                   run_id: str | None, dataset: str | None) -> dict:
    """The snapshot of the app state after a step; used by the loop and by replay so both record the same fields."""
    _tp_de = TestPanel(page, SessionMode.DataEditor)
    _tp_se = TestPanel(page, SessionMode.SchemaEditor)
    return {
        "de_data": _tp_de.get_data(),
        "de_schema": _tp_de.get_schema(),
        "se_schema": _tp_se.get_schema(),
        "se_data": _tp_se.get_data(),
        "de_gui": capture_gui_facts(page) if _get_current_active_mode(page) == "dataEditor" else None,
        "se_gui": capture_gui_facts(page) if _get_current_active_mode(page) == "schemaEditor" else None,
        "de_text": capture_text_view(page, SessionMode.DataEditor),
        "se_text": capture_text_view(page, SessionMode.SchemaEditor),
        "run_id": run_id,
        "dataset": dataset,
        "action": action,
        "step": step,
    }
