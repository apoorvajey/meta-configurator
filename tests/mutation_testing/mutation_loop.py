"""Mutation loop: discovers the actions available in MetaConfigurator, picks one by weight,
runs it, checks the invariants and records the step.

    python -m tests.mutation_testing.mutation_loop

Settings come from run_config.json (see profile_config.py).
"""

from __future__ import annotations

import json
import os
import re
import time
import random
from pathlib import Path
from dataclasses import dataclass, field

from playwright.sync_api import Page, sync_playwright

from tests.shared.python.utils import (
    SessionMode,
    dismiss_dialog_if_present,
    setup_test_panel_via_localstorage,
)
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.dom_discoverer import (
    DOMDiscoverer,
    ValueGenerator,
    InteractionExecutor,
    ElementDescriptor,
    CATEGORY_LABELS,
    _get_current_active_mode,
    _resolve_governing_schema,
    js_click_for_testid,
    testid_path_string_to_list,
    capture_gui_facts,
    capture_text_view,
)
from tests.invariants.invariants_core import InvariantChecker, InvariantReport
from tests.invariants.temporal_invariants import TemporalInvariantChecker

DATA_DIR = Path(__file__).parent / "test_data"

# Example schemas from MetaConfigurator's dropdown; the name must match the dropdown text.
# run_config.json's "schema" picks one of them.
SCHEMA_DATA_PAIRS: list[tuple[str, Path]] = [
    ("Autonomous Vehicle Schema", DATA_DIR / "self_driving_vehicle_data.json"),
    ("Feature Testing Schema",    DATA_DIR / "person_data.json"),
    ("Strenda Schema",            DATA_DIR / "strenda_data.json"),
    ("EnzymeML Schema",           DATA_DIR / "enzymeml_data.json"),
]


def _dataset_slug(schema_option_name: str, data_file: str) -> str:
    """Stable name for a schema/data pair. It is tagged onto every snapshot so rules are inferred per dataset."""
    def _norm(s: str) -> str:
        return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')
    return f"{_norm(schema_option_name)}__{_norm(data_file)}"


def _safe_filename_part(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', s)[:60]


def _json_safe(value):
    """`value` if it survives json.dumps, otherwise its repr()."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


VIEWPORT = {"width": 1920, "height": 1080}
PAGE_ZOOM = "0.75"


def setup_app_for_schema(page: Page, schema_option_name: str, data_file_name: str) -> None:
    """Load an example schema by its dropdown name, then upload its data file in the Data Editor Text View."""
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
    """Set the page up the same way for a live run and a replay: test panel, zoom, dialog, schema and data.
    The caller sets the viewport first.
    """
    setup_test_panel_via_localstorage(page)
    page.wait_for_timeout(2000)
    page.evaluate(f"document.body.style.zoom = '{PAGE_ZOOM}'")
    print("✓ Page loaded")

    dismiss_dialog_if_present(page)
    setup_app_for_schema(page, schema_option_name, data_file_name)


def scroll_to_validation_error(page: Page) -> None:
    """Scroll the first validation-error icon into view before a screenshot. A replay repeats this so page state matches."""
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


def build_snapshot(page: Page, action: str, step: int,
                   run_id: str | None, dataset: str | None) -> dict:
    """Snapshot of the app state after a step. Shared by the loop and the replay so both record the same fields."""
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


@dataclass
class StepResult:
    action_id: str
    description: str
    kwargs: dict
    action_succeeded: bool
    action_error: str | None
    report: InvariantReport | None
    screenshot_path: str | None = None
    available_actions: list | None = None
    timestamp: float = field(default_factory=time.time)
    snapshot: dict | None = None

    @property
    def invariants_passed(self) -> bool:
        if not self.action_succeeded or self.report is None:
            return False
        return self.report.passed


class MutationLoop:

    def __init__(self, page: Page, mode: SessionMode = SessionMode.DataEditor):
        self.page = page
        self.mode = mode
        self.checker = InvariantChecker(page, mode)
        self.history: list[StepResult] = []
        self._skip_next: bool = False
        self._prev_snapshot: dict | None = None
        self._recent_action_testids: list[str] = []
        self._prev_failure_names: set[str] = set()
        self._repair_already_attempted: set[str] = set()  # failures the repair gave up on

        # Locality focus. Mode, view and path are always set or cleared together; path is a parsed list of segments.
        self._focus_mode: str | None = None
        self._focus_view: str | None = None
        self._focus_path: list | None = None
        self._focus_age: int = 0

        # Set after a successful GUI add-property to (mode_label, path of the new leaf).
        # _weighted_sample uses it on the next call only.
        self._pending_create_target: tuple[str, list] | None = None

        self._screenshots_enabled: bool = False
        self._screenshots_dir: str = "mutation_screenshots"

    def export_history(self, path: str = "history.json",
                        schema_option_name: str | None = None,
                        data_file: str | None = None,
                        run_config: dict | None = None,
                        resolved_settings: dict | None = None) -> None:
        """Write the run to history.json. schema_option_name and data_file let a replay load the same pair."""
        from tests.mutation_testing.replay_runner import export_history
        export_history(self.history, path,
                        schema_option_name=schema_option_name, data_file=data_file,
                        run_config=run_config, resolved_settings=resolved_settings)

    def _interactive_action_select(
            self, elements: list[ElementDescriptor], suggested: ElementDescriptor
    ) -> ElementDescriptor | None:
        """Pick a category first, then an action in it. [a] shows the flat list, [s] skips, [q] quits."""
        while True:
            categories: dict[str, list[ElementDescriptor]] = {}
            for e in elements:
                categories.setdefault(e.category, []).append(e)
            sorted_category_keys = sorted(categories.keys())

            print(f"\n   Suggested: {suggested.human_label}  ({CATEGORY_LABELS.get(suggested.category, suggested.category)})")
            print(f"   [Enter] Accept suggested   [a] Show all {len(elements)} actions   [s] Skip   [q] Quit")
            print("   Categories:")
            for i, cat_key in enumerate(sorted_category_keys):
                label = CATEGORY_LABELS.get(cat_key, cat_key)
                count = len(categories[cat_key])
                marker = " *" if cat_key == suggested.category else ""
                print(f"     [{i+1:2}] {label} ({count}){marker}")

            try:
                choice = input("   > ").strip().lower()
            except EOFError:
                choice = ""

            if choice == "q":
                raise StopIteration
            elif choice == "s":
                print("   Step skipped.")
                return None
            elif choice == "":
                return suggested
            elif choice == "a":
                picked = self._select_from_flat_list(elements, suggested)
                if picked == "QUIT":
                    raise StopIteration
                if picked == "BACK":
                    continue
                return picked
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(sorted_category_keys):
                    cat_key = sorted_category_keys[idx]
                    picked = self._select_from_category(categories[cat_key], suggested, cat_key)
                    if picked == "BACK":
                        continue
                    if picked == "QUIT":
                        raise StopIteration
                    return picked
                else:
                    print(f"   Please enter a number between 1 and {len(sorted_category_keys)}.")
            else:
                print("   Please enter a number, Enter, [a], [s], or [q].")

    def _select_from_category(
            self, category_elements: list[ElementDescriptor], suggested: ElementDescriptor, cat_key: str
    ) -> ElementDescriptor | None | str:
        """Returns an ElementDescriptor, None (skip), 'BACK' or 'QUIT'."""
        label = CATEGORY_LABELS.get(cat_key, cat_key)
        while True:
            print(f"\n   {label} ({len(category_elements)}):")
            for j, e in enumerate(category_elements):
                marker = " *" if e is suggested else ""
                print(f"     [{j+1:3}] {e.human_label}{marker}")
            print("   [Enter] Accept suggested   [b] Back to categories   [s] Skip   [q] Quit")

            try:
                choice = input("   > ").strip().lower()
            except EOFError:
                choice = ""

            if choice == "q":
                return "QUIT"
            elif choice == "b":
                return "BACK"
            elif choice == "s":
                print("   Step skipped.")
                return None
            elif choice == "":
                return suggested
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(category_elements):
                    chosen = category_elements[idx]
                    print(f"   Selected: {chosen.human_label}")
                    return chosen
                else:
                    print(f"   Please enter a number between 1 and {len(category_elements)}.")
            else:
                print("   Please enter a number, Enter, [b], [s], or [q].")

    def _select_from_flat_list(
            self, elements: list[ElementDescriptor], suggested: ElementDescriptor
    ) -> ElementDescriptor | None | str:
        sorted_elements = sorted(elements, key=lambda e: e.category)
        print(f"\n   All {len(sorted_elements)} actions:")
        current_category = None
        for j, e in enumerate(sorted_elements):
            if e.category != current_category:
                print(f"\n   ── {CATEGORY_LABELS.get(e.category, e.category)}")
                current_category = e.category
            marker = " *" if e is suggested else ""
            print(f"     [{j+1:3}] {e.human_label}{marker}")
        print("\n   [Enter] Accept suggested   [b] Back to categories   [s] Skip   [q] Quit")

        while True:
            try:
                choice = input("   > ").strip().lower()
            except EOFError:
                choice = ""

            if choice == "q":
                return "QUIT"
            elif choice == "b":
                return "BACK"
            elif choice == "s":
                print("   Step skipped.")
                return None
            elif choice == "":
                return suggested
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(sorted_elements):
                    chosen = sorted_elements[idx]
                    print(f"   Selected: {chosen.human_label}")
                    return chosen
                else:
                    print(f"   Please enter a number between 1 and {len(sorted_elements)}.")
            else:
                print("   Please enter a number, Enter, [b], [s], or [q].")

    def _find_last_known_good_snapshot(self, persisting_names: set[str]) -> tuple[dict | None, int | None]:
        """Most recent recorded step with none of the persisting failures, as (snapshot, step number), or (None, None)."""
        for step_result in reversed(self.history):
            if step_result.report is None or step_result.snapshot is None:
                continue
            names_here = {r.name for r in step_result.report.failures}
            if not (names_here & persisting_names):
                return step_result.snapshot, step_result.snapshot.get("step")
        return None, None

    def _read_full_snapshot(self) -> dict:
        _tp_de = TestPanel(self.page, SessionMode.DataEditor)
        _tp_se = TestPanel(self.page, SessionMode.SchemaEditor)
        return {
            "de_data": _tp_de.get_data(),
            "de_schema": _tp_de.get_schema(),
            "se_schema": _tp_se.get_schema(),
            "se_data": _tp_se.get_data(),
        }

    def _repair_policy_walk_back_to_known_good(self, persisting_names: set[str], last_action_testid: str) -> dict:
        """Undo one click at a time until the live state matches the last known-good snapshot.

        No fixed undo count is used: one loop step can take several undo entries (change-type-text is a delete followed by an insert).
        Matching the snapshot is not enough, because Text View content is not part of it, so success is confirmed by a fresh
        invariant check. Returns {"succeeded": bool, "steps": [...]}.
        """
        target_snapshot, target_step = self._find_last_known_good_snapshot(persisting_names)
        if target_snapshot is None:
            print(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
            print("      No known-good state on record to walk back to - "
                  "falling back to the single-undo policy")
            return self._repair_policy_single_undo_or_revert(persisting_names, last_action_testid)

        current_mode = _get_current_active_mode(self.page)
        # the caller has already switched back to the prior mode if the failure followed a mode switch
        mode_undo = "schema_undo" if current_mode == "schemaEditor" else "undo"
        mode_redo = "schema_redo" if current_mode == "schemaEditor" else "redo"
        MAX_UNDO_ATTEMPTS = 15

        print(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
        print(f"      Walking back toward the recorded state from step {target_step}")

        def _cleared(report: InvariantReport) -> bool:
            return not ({r.name for r in report.failures} & persisting_names)

        target_fields = {k: target_snapshot[k] for k in ("de_data", "de_schema", "se_schema", "se_data")}
        steps = []
        clicks_done = 0
        resolved = False

        # Zero clicks if the state already matches the target; undoing anyway would revert an unrelated earlier action.
        if self._read_full_snapshot() == target_fields:
            fresh = self.checker.run_all()
            if _cleared(fresh):
                resolved = True
                print(f"      ✓ Live state already matches step {target_step}'s recorded state, "
                      f"and the failure is confirmed resolved (0 undos needed)")
            else:
                print(f"      Live state matches step {target_step}'s recorded snapshot, but "
                      f"{sorted(persisting_names)} still fails there too - undo/redo can't fix "
                      f"this (it depends on something the snapshot doesn't track, e.g. raw Text "
                      f"View editor content) - giving up without clicking anything")
        else:
            for attempt in range(1, MAX_UNDO_ATTEMPTS + 1):
                self._click_testid_direct(mode_undo)
                self._recent_action_testids.append(mode_undo)
                clicks_done += 1
                report_now = self.checker.run_all()
                steps.append({"testid": mode_undo, "report": report_now})

                if self._read_full_snapshot() == target_fields:
                    if _cleared(report_now):
                        resolved = True
                        print(f"      ✓ Reached step {target_step}'s recorded state after "
                              f"{attempt} undo(s), and the failure is confirmed resolved")
                    else:
                        print(f"      Reached step {target_step}'s recorded state after "
                              f"{attempt} undo(s), but {sorted(persisting_names)} still fails "
                              f"there too - undo/redo can't fix this one")
                    break

            self._recent_action_testids = self._recent_action_testids[-2:]

        if resolved:
            return {"succeeded": True, "cleared": sorted(persisting_names), "steps": steps}

        print(f"      ✗ Reverting {clicks_done} undo(s) and giving up on auto-repair for this failure")
        for _ in range(clicks_done):
            self._click_testid_direct(mode_redo)
            self._recent_action_testids.append(mode_redo)
            report_now = self.checker.run_all()
            steps.append({"testid": mode_redo, "report": report_now})
        self._recent_action_testids = self._recent_action_testids[-2:]

        return {"succeeded": False, "still_failing": sorted(persisting_names), "steps": steps}


    def _repair_policy_single_undo_or_revert(self, persisting_names: set[str], last_action_testid: str) -> dict:
        """Undo once (or redo, if the last action was itself an undo/redo). If the failures clear, keep it; otherwise revert.

        Each real click is returned as its own step so a replay can reproduce it. Returns {"succeeded": bool, "steps": [...]}.
        """
        current_mode = _get_current_active_mode(self.page)
        # the caller has already switched back to the prior mode if the failure followed a mode switch
        mode_undo = "schema_undo" if current_mode == "schemaEditor" else "undo"
        mode_redo = "schema_redo" if current_mode == "schemaEditor" else "redo"

        if last_action_testid == mode_undo:
            repair_click, revert_click = mode_redo, mode_undo
        elif last_action_testid == mode_redo:
            repair_click, revert_click = mode_undo, mode_redo
        else:
            repair_click, revert_click = mode_undo, mode_redo

        print(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
        print(f"      Attempting repair: {repair_click}")
        self._click_testid_direct(repair_click)
        self._recent_action_testids.append(repair_click)

        post_repair_report = self.checker.run_all()
        still_failing = {r.name for r in post_repair_report.failures} & persisting_names

        if not still_failing:
            print("      ✓ Repair succeeded - persisting failure(s) cleared")
            return {
                "succeeded": True,
                "cleared": sorted(persisting_names),
                "steps": [{"testid": repair_click, "report": post_repair_report}],
            }

        print(f"      ✗ Repair did not clear {sorted(still_failing)} - reverting "
              f"({revert_click}) and giving up on auto-repair for this failure "
              f"(it will keep logging normally, just without further repair attempts)")
        self._click_testid_direct(revert_click)
        self._recent_action_testids.append(revert_click)
        self._recent_action_testids = self._recent_action_testids[-2:]
        final_report = self.checker.run_all()

        return {
            "succeeded": False,
            "still_failing": sorted(still_failing),
            "steps": [
                {"testid": repair_click, "report": post_repair_report},
                {"testid": revert_click, "report": final_report},
            ],
        }

    def _click_testid_direct(self, testid: str) -> None:
        """Click a fixed control (undo, redo, mode switch) without running discovery."""
        js = js_click_for_testid(testid)
        if js:
            self.page.evaluate(js)
        else:
            self.page.get_by_test_id(testid).first.click()
        self.page.wait_for_timeout(400)

    def _click_mode_switch_for_repair(self) -> InvariantReport:
        self._click_testid_direct(_MODE_SWITCH_TESTID)
        self._recent_action_testids.append(_MODE_SWITCH_TESTID)
        self._recent_action_testids = self._recent_action_testids[-2:]
        return self.checker.run_all()

    def _log_mode_switch_step(self, report: InvariantReport, step_num: int, role: str, desc: str) -> None:
        screenshot_path = self._capture_screenshot(
            filename=f"step_{step_num:02d}_repair_{role}.png",
            invariants_failed=bool(report and not report.passed),
        )
        sub_result = StepResult(
            action_id=f"repair:{_MODE_SWITCH_TESTID}",
            description=desc,
            kwargs={"testid": _MODE_SWITCH_TESTID, "value": "", "role": role},
            action_succeeded=True,
            action_error=None,
            report=report,
            screenshot_path=screenshot_path,
        )
        sub_result.snapshot = self._build_snapshot(_MODE_SWITCH_TESTID, step_num)
        self._append_snapshot(sub_result.snapshot)
        self.history.append(sub_result)
        self._prev_snapshot = sub_result.snapshot

    def _capture_screenshot(self, filename: str, invariants_failed: bool) -> str | None:
        """Screenshot for any logged step, or None when screenshots are off.
        If invariants failed it first scrolls to the error icon, which a replay repeats.
        """
        if not self._screenshots_enabled:
            return None
        if invariants_failed:
            self._scroll_to_validation_error()
        screenshot_path = os.path.join(self._screenshots_dir, filename)
        self.page.screenshot(path=screenshot_path)
        print(f"   {screenshot_path}")
        return screenshot_path

    def _check_and_repair_persisting_failures(
        self, report: InvariantReport | None, last_action_testid: str,
        action_succeeded: bool, step_num: int,
    ) -> InvariantReport | None:
        """A failure that shows up in two consecutive steps triggers a repair; its first appearance is left alone.
        Clear-text actions are handled separately in run_random().

        If the last action was a mode switch, the loop switches back first (undo stacks are per mode) and forward again afterwards.
        Returns the report to use for the next step.
        """
        current_failure_names = {r.name for r in report.failures} if report else set()
        persisting = (current_failure_names & self._prev_failure_names) - self._repair_already_attempted

        if persisting and action_succeeded:
            switched_for_repair = last_action_testid == _MODE_SWITCH_TESTID
            if switched_for_repair:
                print("\n   Persisting failure follows a mode switch - "
                      "switching back to the prior mode before repair "
                      "(undo/redo only touch the currently active mode's stack)")
                switch_back_report = self._click_mode_switch_for_repair()
                self._log_mode_switch_step(
                    switch_back_report, step_num, "mode_switch_back",
                    "switching back to the prior mode before repair",
                )

            repair_info = self._repair_policy_walk_back_to_known_good(persisting, last_action_testid)
            repair_steps = repair_info["steps"]

            for step_idx, sub_step in enumerate(repair_steps):
                is_last = step_idx == len(repair_steps) - 1
                if repair_info["succeeded"]:
                    role = "succeeded"
                    desc = f"auto-repair cleared persisting failure(s) {sorted(persisting)}"
                elif is_last:
                    role = "revert"
                    desc = (f"auto-repair did not clear {repair_info['still_failing']} "
                            f"- reverted, giving up on further repair for this failure")
                else:
                    role = "attempt"
                    desc = f"attempting auto-repair for persisting failure(s) {sorted(persisting)}"

                sub_report = sub_step["report"]
                sub_screenshot_path = self._capture_screenshot(
                    filename=(f"step_{step_num:02d}_repair{step_idx + 1}_"
                              f"{_safe_filename_part(sub_step['testid'])}.png"),
                    invariants_failed=bool(sub_report and not sub_report.passed),
                )

                sub_result = StepResult(
                    action_id=f"repair:{sub_step['testid']}",
                    description=desc,
                    # role lets log_exporter tell an attempt from its revert; it renders from kwargs, not from the description.
                    kwargs={"testid": sub_step["testid"], "value": "",
                            "persisting_failures": sorted(persisting),
                            "succeeded": repair_info["succeeded"],
                            "role": role},
                    action_succeeded=True,
                    action_error=None,
                    report=sub_report,
                    screenshot_path=sub_screenshot_path,
                )
                sub_result.snapshot = self._build_snapshot(sub_step["testid"], step_num)
                self._append_snapshot(sub_result.snapshot)
                self.history.append(sub_result)

                if is_last:
                    # The next step compares against the state after the repair (or revert).
                    self._prev_snapshot = sub_result.snapshot
                    report = sub_step["report"]

            if not repair_steps:
                # No click to record, but report and _prev_snapshot must reflect the current state, or this check repeats every step.
                report = self.checker.run_all()
                self._prev_snapshot = self._build_snapshot(last_action_testid, step_num)
                self._append_snapshot(self._prev_snapshot)

            if not repair_info["succeeded"]:
                self._repair_already_attempted |= set(repair_info["still_failing"])

            if switched_for_repair:
                print("   Switching forward again to restore the mode "
                      "the triggering step left us in")
                switch_forward_report = self._click_mode_switch_for_repair()
                self._log_mode_switch_step(
                    switch_forward_report, step_num, "mode_switch_forward",
                    "switching forward again after repair",
                )

        # Forget failures that stopped failing, so a later recurrence gets a fresh repair attempt.
        final_failure_names = {r.name for r in report.failures} if report else set()
        self._repair_already_attempted &= final_failure_names
        self._prev_failure_names = final_failure_names

        return report

    def run_random(
            self,
            n: int = 10,
            interactive: bool = False,
            pause_on_failure: bool = False,
            screenshots: bool = True,
            screenshots_dir: str = "mutation_screenshots",
            run_id: str | None = None,
            schema_option_name: str | None = None,
            data_file: str | None = None,
    ):
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S")
        if schema_option_name and data_file:
            self._dataset = _dataset_slug(schema_option_name, data_file)
        else:
            self._dataset = None
            print("[SnapshotCollector] schema_option_name/data_file not provided to "
                  "run_random() - snapshots from this run will have dataset=None and "
                  "won't be groupable per-dataset")
        mode_label = "Interactive" if interactive else "Automatic"
        print(f"\n=== Dynamic random mutation run: {n} steps [{mode_label}] ===")
        if interactive:
            print("   Controls: [Enter] Continue   [s] Skip next   [q] Quit\n")

        if screenshots:
            screenshots_dir = os.path.join(screenshots_dir, self.run_id)
            os.makedirs(screenshots_dir, exist_ok=True)
            print(f"   Screenshots for this run → {screenshots_dir}")

        self._screenshots_enabled = screenshots
        self._screenshots_dir = screenshots_dir

        discoverer = DOMDiscoverer(self.page)
        executor   = InteractionExecutor(self.page)
        self._executor = executor

        try:
            for i in range(n):
                print(f"\n--- Step {i+1}/{n} ---")

                if self._should_skip():
                    print("   Skipped by user")
                    continue

                elements = discoverer.discover()
                if not elements:
                    print("   No interactable elements found - skipping step")
                    continue

                el: ElementDescriptor = _weighted_sample(
                    elements, self._recent_action_testids,
                    focus_mode=self._focus_mode, focus_view=self._focus_view,
                    focus_path=self._focus_path, focus_age=self._focus_age,
                    pending_create=self._pending_create_target,
                )
                # One-shot: cleared every step, matched or not.
                self._pending_create_target = None

                if interactive:
                    override = self._interactive_action_select(elements, el)
                    if override is None:
                        continue
                    el = override

                print(f"\n   ▶  {el.human_label}")
                print(f"      ({el.interaction}: {el.testid})")

                # Schema and instance depend on el.mode: the Data Editor uses the user's schema, the Schema Editor the meta-schema.
                element_mode = SessionMode.SchemaEditor if el.mode == "schemaEditor" else SessionMode.DataEditor
                governing_schema = _resolve_governing_schema(self.page, el.mode)
                try:
                    instance = (TestPanel(self.page, element_mode).get_schema()
                                if element_mode == SessionMode.SchemaEditor
                                else TestPanel(self.page, element_mode).get_data())
                except Exception:
                    instance = {}

                gen = ValueGenerator(schema=governing_schema, instance=instance)
                value = gen.for_element(el)
                if value is not None:
                    print(f"   value: {repr(value)}")

                action_succeeded = True
                action_error     = None
                # Seed set right before execute so random choices inside the executor (names, generated items) can be replayed.
                step_seed = random.SystemRandom().getrandbits(32)
                random.seed(step_seed)
                try:
                    executor.execute(el, value, schema=governing_schema)
                except Exception as e:
                    action_succeeded = False
                    action_error     = str(e)
                    print(f"   Interaction raised: {e}")

                # After a successful GUI add-property, target the new leaf on the next step.
                # add-item-required fills its entry inline; a bare add-item- is not handled.
                if (action_succeeded and el.testid.startswith("add-property-")
                        and el.testid != "add-property-"
                        and executor._last_added_property_name):
                    new_leaf_path = (
                        testid_path_string_to_list(executor._last_added_property_parent_path)
                        + [executor._last_added_property_name]
                    )
                    self._pending_create_target = (_mode_label(el), new_leaf_path)

                report = None
                if action_succeeded:
                    report = self.checker.run_all()
                    report.print()
                else:
                    print("   Skipping invariant check (interaction failed)")

                screenshot_path = None
                if action_succeeded:
                    screenshot_path = self._capture_screenshot(
                        filename=f"step_{i + 1:02d}_{_safe_filename_part(el.testid)}.png",
                        invariants_failed=bool(report and not report.passed),
                    )

                if pause_on_failure and action_succeeded and report and not report.passed:
                    print("\n   Invariant failure - browser is paused.")
                    print("   Inspect the app, then [Enter] to continue or [q] to quit.")
                    try:
                        choice = input("   > ").strip().lower()
                        if choice == "q":
                            raise StopIteration
                    except EOFError:
                        pass

                # input_value keeps the exact typed value for replay; value is the older stringified form.
                # The el_* fields let replay rebuild the element ("role" is already used by repair steps).
                _entered = value if value is not None else (
                    executor._last_rename if el.testid.startswith("rename-") else None)
                resolved_property_name = (
                    executor._last_added_property_name
                    if action_succeeded and el.testid.startswith("add-property-") else None
                )
                result = StepResult(
                    action_id=f"dynamic:{el.testid}",
                    description=f"{el.interaction} {el.testid}"
                                + (f" = {repr(value)}" if value is not None else ""),
                    kwargs={
                        "testid": el.testid,
                        "value": str(value) if value is not None else (
                            executor._last_rename
                            if el.testid.startswith("rename-") else ""
                        ),
                        "input_value": _json_safe(value),
                        "value_repr": repr(_entered) if _entered is not None else None,
                        "created_name": resolved_property_name,
                        "el_mode": el.mode,
                        "el_interaction": el.interaction,
                        "el_tag": getattr(el, "tag", ""),
                        "el_role": getattr(el, "role", ""),
                        "el_view": getattr(el, "view", None),
                        "resolved_property_name": resolved_property_name,
                        "step_seed": step_seed,
                    },
                    action_succeeded=action_succeeded,
                    action_error=action_error,
                    report=report,
                    screenshot_path=screenshot_path,
                    available_actions=[
                        (e.testid, e.human_label, e.interaction)
                        for e in sorted(elements, key=lambda x: x.category)
                    ],
                )
                self.history.append(result)

                self._recent_action_testids.append(el.testid)
                self._recent_action_testids = self._recent_action_testids[-2:]

                # Only successful steps move the focus.
                if action_succeeded:
                    self._focus_mode, self._focus_view, self._focus_path, self._focus_age = _update_focus(
                        self._focus_mode, self._focus_view, self._focus_path, self._focus_age,
                        _focus_key_for_element(el),
                    )

                if action_succeeded:
                    result.snapshot = self._build_snapshot(el.testid, i + 1)
                    self._append_snapshot(result.snapshot)

                    # No temporal check right after an undo/redo: their effect depends on what they reverse,
                    # so a fixed rule would blame the next action for it.
                    prev_action = (self._prev_snapshot or {}).get("action", "")
                    if prev_action in _UNDO_REDO_TESTIDS:
                        print(f"   Temporal check suppressed - previous action was "
                              f"'{prev_action}' (undo/redo has no dedicated temporal "
                              f"check yet)")
                    elif self._prev_snapshot is not None:
                        temporal_report = TemporalInvariantChecker(
                            prev=self._prev_snapshot,
                            curr=result.snapshot,
                            action=el.testid,
                        ).run_all()
                        if temporal_report.results:
                            temporal_report.print()
                            if report is not None:
                                report.results.extend(temporal_report.results)

                # None after a failed action, so the next step skips its temporal check. Replay does the same.
                self._prev_snapshot = result.snapshot

                # Clear-text is always undone right away, pass or fail: a cleared document leaves nothing to target.
                # It reuses the "repair:" id so the test case reducer keeps the pair together.
                # If clear-text did nothing because the document was already empty, this undo reverts the previous action.
                if action_succeeded and el.testid.startswith("clear-text-"):
                    undo_testid = "schema_undo" if el.mode == "schemaEditor" else "undo"
                    print(f"\n   Auto-undoing '{el.testid}' immediately "
                          f"(clear-text is always reverted right after its "
                          f"own invariant check, regardless of pass/fail)")
                    self._click_testid_direct(undo_testid)
                    self._recent_action_testids.append(undo_testid)
                    self._recent_action_testids = self._recent_action_testids[-2:]

                    undo_report = self.checker.run_all()
                    undo_snapshot = self._build_snapshot(undo_testid, i + 1)
                    self._append_snapshot(undo_snapshot)
                    undo_screenshot_path = self._capture_screenshot(
                        filename=f"step_{i + 1:02d}_autoundo_{_safe_filename_part(undo_testid)}.png",
                        invariants_failed=not undo_report.passed,
                    )
                    undo_result = StepResult(
                        action_id=f"repair:{undo_testid}",
                        description=f"auto-undo of {el.testid} (unconditional, not failure-gated)",
                        kwargs={"testid": undo_testid, "value": "",
                                "role": "clear_text_auto_undo",
                                "succeeded": undo_report.passed,
                                "target_index": len(self.history) - 1},
                        action_succeeded=True,
                        action_error=None,
                        report=undo_report,
                        screenshot_path=undo_screenshot_path,
                        snapshot=undo_snapshot,
                    )
                    self.history.append(undo_result)

                    # Same bookkeeping as _check_and_repair_persisting_failures, which this path bypasses.
                    self._prev_snapshot = undo_snapshot
                    self._repair_already_attempted &= {r.name for r in undo_report.failures}
                    self._prev_failure_names = {r.name for r in undo_report.failures}
                    report = undo_report

                    print(
                        f"      {'✓ restored cleanly' if undo_report.passed else 'restored, but other invariant(s) still failing'}")
                else:
                    report = self._check_and_repair_persisting_failures(
                        report, el.testid, action_succeeded, i + 1
                    )

                if interactive:
                    self._interactive_pause(i + 1, n, result)

        except StopIteration:
            print(f"\n   Loop stopped early at step {len(self.history)}/{n}")

    def _build_snapshot(self, action: str, step: int) -> dict:
        return build_snapshot(self.page, action, step, self.run_id, self._dataset)

    def _append_snapshot(self, snapshot: dict) -> None:
        """Append one snapshot as a JSON line right away, so a run killed by the overnight timeout keeps what it wrote.
        Without a dataset the line goes to _untagged.jsonl.
        """
        slug = self._dataset or "_untagged"
        out = Path("invariant_miner_traces") / f"{slug}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, default=str))
            f.write("\n")
            f.flush()

    def _interactive_pause(self, step: int, total: int, result: StepResult):
        print(f"\n   ── Paused after step {step}/{total} ──")
        if result.report:
            print(f"   Mode: {result.report.mode}  |  {result.report.summary()}")
        if result.screenshot_path:
            print(f"   Screenshot: {os.path.abspath(result.screenshot_path)}")
        print("   Browser is open - inspect the app now.")
        print("   [Enter] Continue   [s] Skip next step   [q] Quit loop")
        while True:
            try:
                choice = input("   > ").strip().lower()
            except EOFError:
                choice = ""
            if choice == "q":
                print("   Stopping loop.")
                raise StopIteration
            elif choice == "s":
                self._skip_next = True
                print("   Next step will be skipped.")
                break
            else:
                break

    def _should_skip(self) -> bool:
        skip = self._skip_next
        self._skip_next = False
        return skip

    def _scroll_to_validation_error(self) -> None:
        scroll_to_validation_error(self.page)

    def print_summary(self):
        total = len(self.history)
        action_failures = sum(1 for s in self.history if not s.action_succeeded)
        invariant_failures = sum(
            1 for s in self.history
            if s.action_succeeded and s.report and not s.report.passed
        )
        print(f"\n{'=' * 60}")
        print("Mutation loop summary")
        print(f"  Steps executed : {total}")
        print(f"  Action errors  : {action_failures}")
        print(f"  Invariant fails: {invariant_failures}")

        no_toggle_count = getattr(getattr(self, "_executor", None), "_silent_no_toggle_count", 0)
        if no_toggle_count:
            print(f"  Ensure-visible : {no_toggle_count} ancestor(s) had no toggle button "
                  f"this run (composition keywords already excluded - see dom_discoverer.py)")

        if invariant_failures > 0:
            print("\nFailed steps (grouped - full step-by-step detail is in the HTML "
                  "log and history.json, not repeated here):")
            self._print_failure_groups()

    def _print_failure_groups(self):
        """Collapse runs of consecutive steps with the same failure into one line."""
        run_start = None
        run_len = 0
        run_sig = None  # frozenset of (name, message) for the current run

        def flush():
            if run_sig is None:
                return
            first = self.history[run_start]
            if run_len == 1:
                print(f"\n  Step {run_start + 1}: {first.action_id}")
            else:
                last = self.history[run_start + run_len - 1]
                print(f"\n  Steps {run_start + 1}\u2013{run_start + run_len} "
                      f"({run_len}x, first: {first.action_id}, last: {last.action_id}):")
            for name, message in sorted(run_sig):
                print(f"    \u2717 {name}: {message}")

        for i, s in enumerate(self.history):
            if not (s.action_succeeded and s.report and not s.report.passed):
                flush()
                run_sig = None
                continue
            sig = frozenset((f.name, f.message) for f in s.report.failures)
            if sig == run_sig:
                run_len += 1
            else:
                flush()
                run_start, run_len, run_sig = i, 1, sig
        flush()

_UNDO_REDO_TESTIDS = ("undo", "redo", "schema_undo", "schema_redo")

# The only mode-switch action discovery keeps; clicking it toggles Data and Schema.
_MODE_SWITCH_TESTID = "mode-active-false"

_ACTION_WEIGHTS_DIR = Path(__file__).parent / "config"
_DEFAULT_WEIGHTS_FILENAME = "action_weights.json"
_active_weights_filename = _DEFAULT_WEIGHTS_FILENAME


def set_action_weights_profile(profile: str | None = None) -> None:
    """Choose the weights file: None or "default" uses action_weights.json, anything else action_weights_<profile>.json.
    Call it before run_random().
    """
    global _active_weights_filename, _action_weights_cache
    _active_weights_filename = (
        _DEFAULT_WEIGHTS_FILENAME if not profile or profile == "default"
        else f"action_weights_{profile}.json"
    )
    _action_weights_cache = None
    print(f"[weights] active profile file: {_active_weights_filename}")


# Each level's weight is shared among the children present this step,
# so a branch with many elements gets no extra pull.

_DEFAULT_HIERARCHICAL_WEIGHTS: dict = {
    "Schema Editor": {
        "weight": 10, "enabled": True,
        "children": {
            "GUI View": {
                "weight": 5, "enabled": True,
                "children": {
                    "add_item_required":      {"weight": 8, "enabled": True},
                    "add_item_or_property":   {"weight": 8, "enabled": True},
                    "input_select_input":     {"weight": 8, "enabled": True},
                    "input_select_other":     {"weight": 5, "enabled": True},
                    "fill":                   {"weight": 5, "enabled": True},
                    "rename":                 {"weight": 4, "enabled": True},
                    "remove_property":        {"weight": 3, "enabled": True},
                    "expand":                 {"weight": 2, "enabled": True},
                    "collapse":               {"weight": 3, "enabled": True},
                    "mode_switch":            {"weight": 2, "enabled": True},
                    "format_selector":        {"weight": 2, "enabled": True},
                    "undo_redo_base":         {"weight": 2, "enabled": True},
                    "undo_redo_boosted":      {"weight": 5, "enabled": True},
                    "default":                {"weight": 1, "enabled": True},
                },
            },
            "Schema Diagram": {"weight": 0, "enabled": False, "children": {}},
            "Text View": {
                "weight": 8, "enabled": True,
                "children": {
                    "edit_text":              {"weight": 3, "enabled": True},
                    "add_child_text":         {"weight": 3, "enabled": True},
                    "change_type_text":       {"weight": 3, "enabled": True},
                    "remove_child_text":      {"weight": 1, "enabled": True},
                    "rename_child_text":      {"weight": 1, "enabled": True},
                    "add_array_item_text":    {"weight": 3, "enabled": True},
                    "remove_array_item_text": {"weight": 3, "enabled": True},
                    "clear_text":              {"weight": 1, "enabled": True},
                },
            },
        },
    },
    "Data Editor": {
        "weight": 10, "enabled": True,
        "children": {
            "GUI View": {
                "weight": 8, "enabled": True,
                "children": {
                    "add_item_required":      {"weight": 8, "enabled": True},
                    "add_item_or_property":   {"weight": 8, "enabled": True},
                    "input_select_input":     {"weight": 8, "enabled": True},
                    "input_select_other":     {"weight": 5, "enabled": True},
                    "fill":                   {"weight": 5, "enabled": True},
                    "rename":                 {"weight": 4, "enabled": True},
                    "remove_property":        {"weight": 3, "enabled": True},
                    "expand":                 {"weight": 2, "enabled": True},
                    "collapse":               {"weight": 3, "enabled": True},
                    "mode_switch":            {"weight": 2, "enabled": True},
                    "format_selector":        {"weight": 2, "enabled": True},
                    "undo_redo_base":         {"weight": 2, "enabled": True},
                    "undo_redo_boosted":      {"weight": 5, "enabled": True},
                    "default":                {"weight": 1, "enabled": True},
                },
            },
            "Text View": {
                "weight": 5, "enabled": True,
                "children": {
                    "edit_text":              {"weight": 3, "enabled": True},
                    "add_child_text":         {"weight": 3, "enabled": True},
                    "change_type_text":       {"weight": 3, "enabled": True},
                    "remove_child_text":      {"weight": 1, "enabled": True},
                    "rename_child_text":      {"weight": 1, "enabled": True},
                    "add_array_item_text":    {"weight": 3, "enabled": True},
                    "remove_array_item_text": {"weight": 3, "enabled": True},
                    "clear_text":              {"weight": 1, "enabled": True},
                },
            },
        },
    },
    "Settings": {"weight": 0, "enabled": False, "children": {}},
}

_action_weights_cache: dict | None = None


def _load_action_weights() -> dict:
    """Load the active weights file once. Falls back to the built-in defaults if the file is missing or malformed."""
    global _action_weights_cache
    if _action_weights_cache is not None:
        return _action_weights_cache

    path = _ACTION_WEIGHTS_DIR / _active_weights_filename
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        _action_weights_cache = {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception as e:
        print(f"Could not load {path.name} ({e}) - using hardcoded hierarchical defaults")
        _action_weights_cache = _DEFAULT_HIERARCHICAL_WEIGHTS
    return _action_weights_cache


# Categories that change schema or data; used to decide when to boost undo/redo.
_MUTATING_CATEGORIES = {"edit_field", "add_item", "add_property", "rename", "remove", "edit_text", "add_child_text", "change_type_text", "remove_child_text", "rename_child_text", "add_array_item_text", "remove_array_item_text", "clear_text"}


def _element_action_key(el: ElementDescriptor, boost_undo_redo: bool) -> str:
    """Leaf action type of an element, e.g. 'fill', 'expand' or 'add_child_text'."""
    if el.interaction == "fill":
        return "fill"
    elif el.testid.startswith("input-select-") and el.tag == "input":
        return "input_select_input"
    elif el.testid.startswith("input-select-"):
        return "input_select_other"
    elif el.testid == "add-item-required":
        return "add_item_required"
    elif el.testid.startswith("add-item-") or el.testid.startswith("add-property-"):
        return "add_item_or_property"
    elif el.testid.startswith("expand-"):
        return "expand"
    elif el.testid.startswith("collapse-"):
        return "collapse"
    elif el.testid.startswith("rename-child-text-"):
        return "rename_child_text"
    elif el.testid.startswith("rename-"):
        return "rename"
    elif el.testid.startswith("remove-property-"):
        return "remove_property"
    elif el.testid.startswith("mode-active-"):
        return "mode_switch"
    elif el.testid == "format-selector":
        return "format_selector"
    elif el.testid.startswith("edit-text-"):
        return "edit_text"
    elif el.testid.startswith("add-child-text-"):
        return "add_child_text"
    elif el.testid.startswith("change-type-text-"):
        return "change_type_text"
    elif el.testid.startswith("add-array-item-text-"):
        return "add_array_item_text"
    elif el.testid.startswith("remove-array-item-text-"):
        return "remove_array_item_text"
    elif el.testid.startswith("remove-child-text-"):
        return "remove_child_text"
    elif el.testid.startswith("clear-text-"):
        return "clear_text"
    elif el.testid in _UNDO_REDO_TESTIDS:
        return "undo_redo_boosted" if boost_undo_redo else "undo_redo_base"
    else:
        return "default"


def _mode_label(el: ElementDescriptor) -> str:
    return "Schema Editor" if el.mode == "schemaEditor" else "Data Editor"


def _element_action_key_by_testid(testid: str | None) -> str | None:
    if testid is None:
        return None
    fake = ElementDescriptor(testid=testid, tag="", role="", interaction="click")
    return _element_action_key(fake, boost_undo_redo=False)


# Locality bias: favour the area the loop worked on recently, like a user who stays in one part of the document.
# Only the mode/view and element picks are biased, not the action type. The constants below are untuned.

_LOCALITY_DECAY_STEPS = 4  # steps without reinforcement before the focus expires
_LOCALITY_MODE_VIEW_MULTIPLIER = 4.0  # max multiplier on the focus mode/view weight, at age 0
_LOCALITY_ELEMENT_MULTIPLIER = 6.0  # max multiplier per shared path segment at the element pick
_LOCALITY_MIN_SHARED_DEPTH = 1  # shared leading segments needed to count as nearby

# Text View testids are "{prefix}{mode}:{path}"; GUI testids carry only the path.
_TEXT_VIEW_PATH_PREFIXES = (
    "edit-text-", "add-child-text-", "change-type-text-",
    "remove-child-text-", "rename-child-text-",
    "add-array-item-text-", "remove-array-item-text-",
)
_GUI_VIEW_PATH_PREFIXES = (
    "input-string-", "input-number-", "input-boolean-", "input-select-",
    "add-item-", "add-property-", "expand-", "collapse-",
    "rename-property-", "remove-property-",
)


def _focus_key_for_element(el: ElementDescriptor) -> tuple[str, str, list] | None:
    """(mode, view, path) of the node an action targets, or None for actions without one
    (mode switch, format selector, undo/redo, clear-text).

    The path is a list of segments, so 'Vehicles.car1' and 'Vehicles.car10' do not share a prefix.
    """
    testid = el.testid
    mode_label = _mode_label(el)

    for prefix in _TEXT_VIEW_PATH_PREFIXES:
        if testid.startswith(prefix):
            mode_and_rest = testid[len(prefix):]
            _, _, rest = mode_and_rest.partition(":")
            path_str, _, _ = rest.partition(":")  # drop add-child-text's trailing :prop_name
            return (mode_label, "Text View", testid_path_string_to_list(path_str))

    if testid.startswith("clear-text-"):
        return None

    for prefix in _GUI_VIEW_PATH_PREFIXES:
        if testid.startswith(prefix) and testid != prefix:
            path_str = testid[len(prefix):]
            return (mode_label, "GUI View", testid_path_string_to_list(path_str))

    return None


def _subtree_proximity(path_a: list, path_b: list) -> int:
    """Number of leading path segments two paths share."""
    common = 0
    for a, b in zip(path_a, path_b):
        if a != b:
            break
        common += 1
    return common


def _locality_strength(age: int, decay_steps: int) -> float:
    """1.0 for a focus touched just now, falling linearly to 0.0 at decay_steps."""
    if age >= decay_steps:
        return 0.0
    return 1.0 - (age / decay_steps)


def _update_focus(
    current_mode: str | None, current_view: str | None, current_path: list | None, current_age: int,
    touched_key: tuple[str, str, list] | None,
) -> tuple[str | None, str | None, list | None, int]:
    """Update the locality focus after a step.
    A touched node in the same mode and view that shares enough path with the focus becomes the new focus.
    With no live focus it becomes the anchor. Otherwise the old focus ages by one step.
    """
    if touched_key is not None:
        touched_mode, touched_view, touched_path = touched_key
        if (current_path is not None
                and touched_mode == current_mode
                and touched_view == current_view
                and _subtree_proximity(current_path, touched_path) >= _LOCALITY_MIN_SHARED_DEPTH):
            return touched_mode, touched_view, touched_path, 0
        if current_path is None or current_age >= _LOCALITY_DECAY_STEPS:
            return touched_mode, touched_view, touched_path, 0

    if current_path is not None:
        return current_mode, current_view, current_path, current_age + 1
    return current_mode, current_view, current_path, current_age


def _weighted_sample(
    elements: list[ElementDescriptor], recent_testids: list[str] | None = None,
    focus_mode: str | None = None, focus_view: str | None = None,
    focus_path: list | None = None, focus_age: int = 0,
    pending_create: tuple[str, list] | None = None,
) -> ElementDescriptor:
    """Pick a mode, then a view, then an action type, then an element, each by the configured weights.
    Only branches present this step take part.

    recent_testids boosts undo/redo after a mutation or another undo/redo.
    pending_create is a property added by the previous step: if a value-setting action targets it, that action is returned directly.
    """
    if pending_create is not None:
        target_mode, target_path = pending_create
        _FILL_LIKE_ACTION_TYPES = {"fill", "input_select_input", "input_select_other", "edit_text"}
        matches = []
        for c in elements:
            key = _focus_key_for_element(c)
            if key is None or key[0] != target_mode or key[2] != target_path:
                continue
            if _element_action_key(c, boost_undo_redo=False) in _FILL_LIKE_ACTION_TYPES:
                matches.append(c)
        if matches:
            picked = random.choice(matches)
            print(f"   create-then-fill: targeting the field just created "
                  f"({target_mode} '{'.'.join(str(p) for p in target_path)}') -> {picked.testid}")
            return picked
        else:
            print(f"   (create-then-fill: no matching fill action found for "
                  f"{target_mode} '{'.'.join(str(p) for p in target_path)}' this step - "
                  f"falling back to normal weighted sampling)")

    recent_testids = recent_testids or []
    last = recent_testids[-1] if recent_testids else None
    second_last = recent_testids[-2] if len(recent_testids) >= 2 else None

    last_was_undo_redo = last in _UNDO_REDO_TESTIDS
    already_chained_twice = last_was_undo_redo and second_last in _UNDO_REDO_TESTIDS
    boost_after_undo_redo_chain = last_was_undo_redo and not already_chained_twice

    last_action_key = _element_action_key_by_testid(last) if last else None
    boost_after_mutation = (not last_was_undo_redo) and (last_action_key in _MUTATING_CATEGORIES)

    boost_undo_redo = boost_after_undo_redo_chain or boost_after_mutation

    config = _load_action_weights()

    tagged = [
        (el, _mode_label(el), el.view, _element_action_key(el, boost_undo_redo))
        for el in elements
    ]

    mode_groups: dict[str, list] = {}
    for entry in tagged:
        mode_groups.setdefault(entry[1], []).append(entry)
    mode_weights = {
        m: config.get(m, {}).get("weight", 0)
        for m in mode_groups
        if config.get(m, {}).get("enabled", True)
    }
    mode_weights = {m: w for m, w in mode_weights.items() if w > 0}
    if not mode_weights:
        return random.choice(elements)

    locality_strength = _locality_strength(focus_age, _LOCALITY_DECAY_STEPS)
    if locality_strength > 0 and focus_mode in mode_weights:
        mode_weights[focus_mode] *= 1.0 + (_LOCALITY_MODE_VIEW_MULTIPLIER - 1.0) * locality_strength

    chosen_mode = random.choices(list(mode_weights.keys()), weights=list(mode_weights.values()), k=1)[0]

    mode_config = config.get(chosen_mode, {}).get("children", {})
    view_groups: dict[str, list] = {}
    for entry in mode_groups[chosen_mode]:
        view_groups.setdefault(entry[2], []).append(entry)
    view_weights = {
        v: mode_config.get(v, {}).get("weight", 0)
        for v in view_groups
        if mode_config.get(v, {}).get("enabled", True)
    }
    view_weights = {v: w for v, w in view_weights.items() if w > 0}
    if not view_weights:
        # All views under this mode are disabled or empty: fall back to this mode's elements.
        print(f"No enabled View branch under '{chosen_mode}' has any discoverable "
              f"actions this step - falling back to uniform choice within '{chosen_mode}'")
        return random.choice([entry[0] for entry in mode_groups[chosen_mode]])

    # Pull toward the focus view only inside the focus mode; "Text View" in one editor is not the same tab as in the other.
    if locality_strength > 0 and chosen_mode == focus_mode and focus_view in view_weights:
        view_weights[focus_view] *= 1.0 + (_LOCALITY_MODE_VIEW_MULTIPLIER - 1.0) * locality_strength

    chosen_view = random.choices(list(view_weights.keys()), weights=list(view_weights.values()), k=1)[0]

    view_config = mode_config.get(chosen_view, {}).get("children", {})
    action_groups: dict[str, list] = {}
    for entry in view_groups[chosen_view]:
        action_groups.setdefault(entry[3], []).append(entry)
    action_weights = {
        a: view_config.get(a, {}).get("weight", 0)
        for a in action_groups
        if view_config.get(a, {}).get("enabled", True)
    }
    action_weights = {a: w for a, w in action_weights.items() if w > 0}
    if not action_weights:
        print(f"No enabled Action-type under '{chosen_mode} > {chosen_view}' has any "
              f"discoverable actions this step - falling back to uniform choice within it")
        return random.choice([entry[0] for entry in view_groups[chosen_view]])
    chosen_action = random.choices(list(action_weights.keys()), weights=list(action_weights.values()), k=1)[0]

    # Uniform among matching elements, unless a focus exists in this mode and view;
    # then weight by the number of path segments shared with the focus.
    candidates = [entry[0] for entry in action_groups[chosen_action]]
    if (len(candidates) > 1 and locality_strength > 0
            and chosen_mode == focus_mode and chosen_view == focus_view and focus_path is not None):
        weights = []
        for c in candidates:
            c_key = _focus_key_for_element(c)
            depth = _subtree_proximity(focus_path, c_key[2]) if c_key is not None else 0
            if depth >= _LOCALITY_MIN_SHARED_DEPTH:
                weights.append(1.0 + (_LOCALITY_ELEMENT_MULTIPLIER - 1.0) * locality_strength * depth)
            else:
                weights.append(1.0)
        return random.choices(candidates, weights=weights, k=1)[0]
    return random.choice(candidates)


def run_demo():
    import sys

    run_id = time.strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join("mutation_logs", run_id)
    os.makedirs(log_dir, exist_ok=True)

    class Tee:
        def __init__(self, *files):
            self.files = files

        def write(self, obj):
            for f in self.files:
                f.write(obj)
                f.flush()

        def flush(self):
            for f in self.files:
                f.flush()

    log_file = open("mutation_terminal.log", "w", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, log_file)

    from tests.mutation_testing.profile_config import (
        load_run_config,
        apply_configured_profile,
        resolve_schema_choice,
        resolve_run_mode,
        resolve_num_steps,
        resolve_screenshots,
    )

    run_config = load_run_config()

    # Use run_mode from the config if it is valid; otherwise ask at the prompt.
    configured_mode = resolve_run_mode(run_config)
    if configured_mode is not None:
        choice = {"background": "1", "watch_failures": "2", "interactive": "3"}[configured_mode]
        print(f"[config] run_mode fixed to '{configured_mode}' via run_config.json - skipping prompt")
    else:
        print("Select run mode:")
        print("  [1] Background     - fully hands-free, no pausing")
        print("  [2] Watch failures - pauses only on invariant failure")
        print("  [3] Interactive    - pauses after every step")
        while True:
            choice = input("> ").strip()
            if choice in ("1", "2", "3"):
                break
            print("  Please enter 1, 2, or 3.")

    interactive = (choice == "3")
    pause_on_failure = (choice in ("2", "3"))

    schema_option_name, data_file = resolve_schema_choice(SCHEMA_DATA_PAIRS, run_config)
    n_steps = resolve_num_steps(run_config)
    screenshots_enabled = resolve_screenshots(run_config)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page    = browser.new_page()
        page.set_viewport_size(VIEWPORT)

        prepare_app_page(page, schema_option_name, data_file.name)

        print("\n=== Baseline invariant check ===")
        checker = InvariantChecker(page, SessionMode.DataEditor)
        checker.run_all().print()

        loop = MutationLoop(page, SessionMode.DataEditor)

        print("\n=== DOM Discovery (before loop) ===")
        discoverer = DOMDiscoverer(page)
        elements = discoverer.discover()
        from collections import Counter
        counts = Counter(e.category for e in elements)
        print(f"   {len(elements)} actions discovered:")
        for cat, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"     {CATEGORY_LABELS.get(cat, cat):<25} {count}")
        apply_configured_profile(run_config)

        loop.run_random(n=n_steps, interactive=interactive, pause_on_failure=pause_on_failure,
                        screenshots=screenshots_enabled,
                        run_id=run_id, schema_option_name=schema_option_name, data_file=data_file.name)
        loop.print_summary()

        from tests.mutation_testing.log_exporter import export_html_log
        export_html_log(loop.history, path=os.path.join(log_dir, "mutation_log.html"),
                        schema_option_name=schema_option_name, data_file=data_file.name)

        # Settings actually used for this run, saved in history.json's meta.
        # Replay reads screenshots_enabled and run_id from here.
        _run_mode_labels = {"1": "background", "2": "watch_failures", "3": "interactive"}
        resolved_settings = {
            "run_id": run_id,
            "run_mode": _run_mode_labels[choice],
            "interactive": interactive,
            "pause_on_failure": pause_on_failure,
            "schema_option_name": schema_option_name,
            "data_file": data_file.name,
            "n_steps": n_steps,
            "screenshots_enabled": screenshots_enabled,
            "weights_profile_file": _active_weights_filename,
        }

        history_dir = os.path.join("mutation_history", run_id)
        os.makedirs(history_dir, exist_ok=True)
        loop.export_history(os.path.join(history_dir, "history.json"),
                            schema_option_name=schema_option_name,
                            data_file=data_file.name,
                            run_config=run_config,
                            resolved_settings=resolved_settings)

        log_file.close()
        sys.stdout = sys.__stdout__

        input("\nPress Enter to close browser...")
        browser.close()


if __name__ == "__main__":
    run_demo()