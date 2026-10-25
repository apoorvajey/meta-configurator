"""Audit of the actions the discoverer offers against the data-testids that are actually on the page.

Opens the app like a mutation run, then lets you put it in any state by hand (a mode, a view, a blank-key row, ...).
Each time you press Enter it audits the page as it is right now: every data-testid on the page, grouped by kind,
with how many of each the discoverer offers as an action and why the others are not. It also lists visible buttons
that have no data-testid at all (these can never become an action).

Run from the project root:   python -m tests.mutation_testing.discovery.testid_audit
Output: the console and tests/output/testid_audit.json (one entry per audit, appended).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from tests.mutation_testing.common.datasets import SCHEMA_DATA_PAIRS
from tests.mutation_testing.discovery.discoverer import DOMDiscoverer
from tests.mutation_testing.discovery.element import _classify
from tests.mutation_testing.discovery.page_capture import _get_current_active_mode
from tests.mutation_testing.loop.app_setup import VIEWPORT, prepare_app_page
from tests.mutation_testing.loop.profile_config import load_run_config, resolve_schema_choice

OUTPUT_PATH = Path("tests/output/testid_audit.json")

_PAGE_TESTIDS_JS = """
() => [...document.querySelectorAll('[data-testid]')].map(el => ({
    testid: el.getAttribute('data-testid'),
    tag: el.tagName.toLowerCase(),
    visible: el.offsetParent !== null,
}))
"""

_NO_TESTID_BUTTONS_JS = """
() => [...document.querySelectorAll('button, [role="button"]')]
    .filter(el => el.offsetParent !== null && !el.hasAttribute('data-testid') && !el.closest('[data-testid]'))
    .map(el => (el.getAttribute('aria-label') || el.getAttribute('title') || el.id || el.innerText || '').trim().slice(0, 40))
"""


# Every element whose testid ends in '-' (an empty path or name): where it sits and what delete controls it holds.
# The discoverer builds the visual-editor delete actions from property-data-<path> rows that contain a
# button[aria-label="Remove"], and skips the row whose testid is exactly 'property-data-'.
_EMPTY_SUFFIX_JS = """
() => [...document.querySelectorAll('[data-testid$="-"]')].map(el => {
    const row = el.closest('tr');
    const label = row ? row.querySelector('[id^="_label_"]') : null;
    return {
        testid: el.getAttribute('data-testid'),
        tag: el.tagName.toLowerCase(),
        visible: el.offsetParent !== null,
        remove_buttons_inside: el.querySelectorAll('button[aria-label="Remove"]').length,
        row_label_id: label ? label.id : null,
        row_level: row ? row.getAttribute('aria-level') : null,
    };
})
"""


def _group(testid: str) -> str:
    """'property-data-Waypoints.Alpha' -> 'property-data'; 'remove-property-' -> 'remove-property'. The first two
    dash-separated words name the kind of control; what follows is the path or name."""
    words = re.split(r"[.\[:]", testid)[0].split("-")
    return "-".join(w for w in words[:2] if w) or testid


def audit(page: Page) -> dict:
    mode = _get_current_active_mode(page)
    offered = {el.testid for el in DOMDiscoverer(page).discover()}
    on_page = page.evaluate(_PAGE_TESTIDS_JS)

    groups: dict[str, dict] = defaultdict(lambda: {"on_page": 0, "offered": 0, "reasons": defaultdict(int), "examples": []})
    empty_suffix = []
    for item in on_page:
        testid = item["testid"]
        g = groups[_group(testid)]
        g["on_page"] += 1
        if testid.endswith("-"):
            empty_suffix.append(testid)
        if testid in offered:
            g["offered"] += 1
            continue
        if not item["visible"]:
            reason = "hidden (not visible)"
        elif _classify(testid) is None:
            reason = "not an action (skip prefix or no known pattern)"
        else:
            reason = "classified as an action but dropped by discovery"
        g["reasons"][reason] += 1
        if len(g["examples"]) < 3:
            g["examples"].append(testid)

    empty_suffix_details = page.evaluate(_EMPTY_SUFFIX_JS)
    no_testid = page.evaluate(_NO_TESTID_BUTTONS_JS)
    # Offered actions that are not page testids (Text View, expand/collapse, undo/redo) are not part of this comparison.
    return {
        "mode": mode,
        "offered_total": len(offered),
        "groups": {k: {"on_page": v["on_page"], "offered": v["offered"],
                       "not_offered": dict(v["reasons"]), "examples": v["examples"]}
                   for k, v in sorted(groups.items())},
        "empty_suffix_testids": sorted(set(empty_suffix)),
        "empty_suffix_details": empty_suffix_details,
        "visible_buttons_without_testid": sorted(set(no_testid)),
    }


def print_audit(result: dict) -> None:
    print(f"\n=== Audit in {result['mode']}: {result['offered_total']} actions offered by discovery ===")
    print(f"{'kind':<28}{'on page':>8}{'offered':>9}   not offered")
    for kind, g in result["groups"].items():
        why = "; ".join(f"{n}x {r}" for r, n in g["not_offered"].items())
        print(f"{kind:<28}{g['on_page']:>8}{g['offered']:>9}   {why}")
        if g["not_offered"] and g["examples"]:
            print(f"{'':<45}e.g. {', '.join(g['examples'])}")
    if result["empty_suffix_testids"]:
        print(f"\nTestids that end in '-' (an empty path or name): {result['empty_suffix_testids']}")
    for d in result["empty_suffix_details"]:
        print(f"   {d['testid']!r} <{d['tag']}> visible={d['visible']} remove-buttons-inside={d['remove_buttons_inside']} "
              f"row-label-id={d['row_label_id']!r} row-level={d['row_level']}")
    if result["visible_buttons_without_testid"]:
        print(f"\nVisible buttons without any data-testid (never an action): {result['visible_buttons_without_testid']}")


def main() -> None:
    config = load_run_config()
    schema_option_name, data_file = resolve_schema_choice(SCHEMA_DATA_PAIRS, config)
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()
        page.set_viewport_size(VIEWPORT)
        prepare_app_page(page, schema_option_name, data_file.name)
        while True:
            answer = input("\nPut the app in the state to audit, then [Enter] to audit it ([q] to quit) > ").strip().lower()
            if answer == "q":
                break
            result = audit(page)
            print_audit(result)
            results.append(result)
        browser.close()

    if results:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nWritten: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()