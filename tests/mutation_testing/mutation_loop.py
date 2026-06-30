"""
mutation_loop.py
================
Located at: tests/mutation_testing/mutation_loop.py

"""

from __future__ import annotations

import os
import time
import random
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable

from playwright.sync_api import Page, sync_playwright

from tests.shared.python.utils import (
    SessionMode,
    dismiss_dialog_if_present,
    setup_test_panel_via_localstorage,
)
from tests.shared.python.test_panel import TestPanel
# from tests.mutation_testing.action_registry import (
#     ActionDef,
#     ACTION_BY_ID,
#     sample_random_action,
# )
from tests.mutation_testing.dom_discoverer import (
    DOMDiscoverer,
    ValueGenerator,
    InteractionExecutor,
    ElementDescriptor,
)
from tests.invariants.invariants_core import InvariantChecker, InvariantReport

DATA_DIR = Path(__file__).parent / "test_data"


# ---------------------------------------------------------------------------
# StepResult
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# MutationLoop
# ---------------------------------------------------------------------------

class MutationLoop:

    def __init__(self, page: Page, mode: SessionMode = SessionMode.DataEditor):
        self.page = page
        self.mode = mode
        self.checker = InvariantChecker(page, mode)
        self.history: list[StepResult] = []
        self._skip_next: bool = False
        self._cached_schema: dict = {}
        self._prev_snapshot: dict | None = None

    def export_history(self, path: str = "history.json") -> None:
        from tests.mutation_testing.replay_runner import export_history
        export_history(self.history, path)

    # ------------------------------------------------------------------
    # Legacy step() — uses static action registry
    # ------------------------------------------------------------------

    def step(self, action_id: str, description: str = "", **kwargs) -> StepResult:
        action = ACTION_BY_ID.get(action_id)
        if action is None:
            raise ValueError(f"Unknown action id: '{action_id}'")

        label = description or action.description
        print(f"\n▶  [{action_id}] {label}")
        if kwargs:
            print(f"   params: {kwargs}")

        action_succeeded = True
        action_error = None
        try:
            action.executor(self.page, **kwargs)
        except Exception as e:
            action_succeeded = False
            action_error = str(e)
            print(f"   ⚠  Action raised: {e}")

        report = None
        if action_succeeded:
            report = self.checker.run_all()
            report.print()
        else:
            print("   ⚠  Skipping invariant check (action failed)")

        result = StepResult(
            action_id=action_id,
            description=label,
            kwargs={k: str(v) for k, v in kwargs.items()},
            action_succeeded=action_succeeded,
            action_error=action_error,
            report=report,
        )
        self.history.append(result)
        return result

    def _interactive_action_select(
            self, elements: list[ElementDescriptor], suggested: ElementDescriptor
    ) -> ElementDescriptor | None:
        """
        In interactive mode, let the user either accept the suggested action
        or pick a specific one by number.
        [Enter]  → accept suggested action
        [1-N]    → pick action by number from the list
        [s]      → skip this step
        [q]      → quit loop
        """
        sorted_elements = sorted(elements, key=lambda e: e.category)
        print(f"\n   Suggested: [{sorted_elements.index(suggested) + 1}] {suggested.human_label}")
        print(f"   [Enter] Accept   [1-{len(sorted_elements)}] Pick action   [s] Skip   [q] Quit")

        while True:
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
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(sorted_elements):
                    chosen = sorted_elements[idx]
                    print(f"   Selected: {chosen.human_label}")
                    return chosen
                else:
                    print(f"   Please enter a number between 1 and {len(sorted_elements)}.")
            else:
                print(f"   Please enter a number, Enter, [s], or [q].")

    # ------------------------------------------------------------------
    # Dynamic run_random()
    # ------------------------------------------------------------------

    def run_random(
        self,
        n: int = 10,
        param_provider: Callable[[ActionDef], dict] | None = None,
        exclude_categories: list[str] | None = None,
        dynamic: bool = True,
        interactive: bool = False,
        screenshots: bool = True,
        screenshots_dir: str = "mutation_screenshots",
    ):
        if not dynamic:
            self._run_random_static(n, param_provider, exclude_categories)
            return

        mode_label = "Interactive" if interactive else "Automatic"
        print(f"\n=== Dynamic random mutation run: {n} steps [{mode_label}] ===")
        if interactive:
            print("   Controls: [Enter] Continue   [s] Skip next   [q] Quit\n")

        if screenshots:
            os.makedirs(screenshots_dir, exist_ok=True)

        discoverer = DOMDiscoverer(self.page)
        executor   = InteractionExecutor(self.page)

        try:
            for i in range(n):
                print(f"\n--- Step {i+1}/{n} ---")

                if self._should_skip():
                    print("   ⏭  Skipped by user")
                    continue

                # 1. Discover
                elements = discoverer.discover()
                if not elements:
                    print("   ⚠  No interactable elements found — skipping step")
                    continue

                # 2. Print available actions (interactive only)
                if interactive:
                    sorted_elements = sorted(elements, key=lambda e: e.category)
                    print(f"   Available actions ({len(elements)}):")
                    current_category = None
                    category_labels = {
                        "1_expand":    "── Expand",
                        "2_edit_field":"── Edit Fields",
                        "3_dropdown":  "── Dropdowns",
                        "4_add":       "── Add",
                        "5_rename":    "── Rename",
                        "6_remove":    "── Remove",
                        "7_mode":      "── Mode",
                        "8_format":    "── Format",
                        "9_other":     "── Other",
                    }
                    for j, e in enumerate(sorted_elements):
                        if e.category != current_category:
                            print(f"\n   {category_labels.get(e.category, '── Other')}")
                            current_category = e.category
                        print(f"     [{j+1:2}] {e.human_label}")
                        print(f"           → {e.testid}")

                # 3. Pick action
                el: ElementDescriptor = _weighted_sample(elements)

                # 4. Interactive action selection — let user override before generating value
                if interactive:
                    override = self._interactive_action_select(elements, el)
                    if override is None:
                        continue
                    el = override

                print(f"\n   ▶  {el.human_label}")
                print(f"      ({el.interaction}: {el.testid})")

                # 5. Generate value for the final chosen action
                schema = self._read_schema_safe()
                gen = ValueGenerator(schema)
                value = gen.for_element(el)
                if value is not None:
                    print(f"   value: {repr(value)}")

                # 6. Execute
                action_succeeded = True
                action_error     = None
                try:
                    executor.execute(el, value, schema=schema)
                except Exception as e:
                    action_succeeded = False
                    action_error     = str(e)
                    print(f"   ⚠  Interaction raised: {e}")

                # 7. Check invariants
                report = None
                if action_succeeded:
                    report = self.checker.run_all()
                    report.print()
                else:
                    print("   ⚠  Skipping invariant check (interaction failed)")

                # 8. Screenshot
                screenshot_path = None
                if action_succeeded and screenshots:
                    if report and not report.passed:
                        self._scroll_to_validation_error()
                    import re
                    safe_testid = re.sub(r'[<>:"/\\|?*]', '_', el.testid)[:60]
                    screenshot_path = os.path.join(
                        screenshots_dir,
                        f"step_{i + 1:02d}_{safe_testid}.png"
                    )
                    self.page.screenshot(path=screenshot_path)
                    print(f"   📸 {screenshot_path}")

                # 9. Pause on invariant failure
                if action_succeeded and report and not report.passed:
                    print(f"\n   ⚠  Invariant failure — browser is paused.")
                    print(f"   Inspect the app, then [Enter] to continue or [q] to quit.")
                    try:
                        choice = input("   > ").strip().lower()
                        if choice == "q":
                            raise StopIteration
                    except EOFError:
                        pass

                # 10. Record
                result = StepResult(
                    action_id=f"dynamic:{el.testid}",
                    description=f"{el.interaction} {el.testid}"
                                + (f" = {repr(value)}" if value is not None else ""),
                    kwargs={
                        "testid": el.testid,
                        "value":  str(value) if value is not None else (
                            executor._last_rename
                            if el.testid.startswith("rename-") else ""
                        ),
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

                # 10b. Save state snapshot for invariant miner
                if action_succeeded:
                    _tp_de = TestPanel(self.page, SessionMode.DataEditor)
                    _tp_se = TestPanel(self.page, SessionMode.SchemaEditor)
                    result.snapshot = {
                        "de_data": _tp_de.get_data(),
                        "de_schema": _tp_de.get_schema(),
                        "se_schema": _tp_se.get_schema(),
                        "se_data": _tp_se.get_data(),
                        "action": el.testid,
                        "step": i + 1,
                    }

                self._prev_snapshot = result.snapshot

                # 11. Interactive pause after step
                if interactive:
                    self._interactive_pause(i + 1, n, result)

        except StopIteration:
            print(f"\n   Loop stopped early at step {len(self.history)}/{n}")
        # Save snapshots for invariant miner
        self._save_snapshots()

    # ------------------------------------------------------------------
    # Interactive helpers
    # ------------------------------------------------------------------

    def _pre_execution_pause(self, el: ElementDescriptor) -> bool:
        print(f"\n   About to execute — inspect the browser first.")
        print(f"   [Enter] Execute   [s] Skip this action   [q] Quit loop")
        while True:
            try:
                choice = input("   > ").strip().lower()
            except EOFError:
                choice = ""
            if choice == "q":
                raise StopIteration
            elif choice == "s":
                print("   Action skipped.")
                return False
            else:
                return True

    def _save_snapshots(self, path: str = "invariant_miner_traces/snapshots.json") -> None:
        import json
        from pathlib import Path
        snapshots = [
            s.snapshot for s in self.history
            if s.snapshot is not None
        ]
        if not snapshots:
            return
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Accumulate across runs — load existing and append
        existing = []
        if out.exists():
            try:
                existing = json.loads(out.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        existing.extend(snapshots)
        out.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
        print(f"[SnapshotCollector] {len(snapshots)} snapshots saved → {out.resolve()}")
        print(f"[SnapshotCollector] Total accumulated: {len(existing)}")

    def _interactive_pause(self, step: int, total: int, result: StepResult):
        print(f"\n   ── Paused after step {step}/{total} ──")
        if result.report:
            print(f"   Mode: {result.report.mode}  |  {result.report.summary()}")
        if result.screenshot_path:
            print(f"   Screenshot: {os.path.abspath(result.screenshot_path)}")
        print(f"   Browser is open — inspect the app now.")
        print(f"   [Enter] Continue   [s] Skip next step   [q] Quit loop")
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
        try:
            self.page.evaluate("""
                () => {
                    const icon = document.querySelector('[data-testid="validation-error-icon"]');
                    if (icon) icon.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            """)
            self.page.wait_for_timeout(400)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Legacy static loop
    # ------------------------------------------------------------------

    def _run_random_static(self, n, param_provider, exclude_categories):
        print(f"\n=== Static random mutation run: {n} steps ===")
        for i in range(n):
            print(f"\n--- Step {i+1}/{n} ---")
            action = sample_random_action(exclude_categories)
            if param_provider is not None:
                kwargs = param_provider(action)
            elif not action.params:
                kwargs = {}
            else:
                print(f"   ⚠  Skipping {action.id} (needs params)")
                continue
            self.step(action.id, **kwargs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_schema_safe(self) -> dict:
        """Return the cached schema — read once at startup, never updated during run."""
        return self._cached_schema

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def print_summary(self):
        total              = len(self.history)
        action_failures    = sum(1 for s in self.history if not s.action_succeeded)
        invariant_failures = sum(
            1 for s in self.history
            if s.action_succeeded and s.report and not s.report.passed
        )
        print(f"\n{'='*60}")
        print(f"Mutation loop summary")
        print(f"  Steps executed : {total}")
        print(f"  Action errors  : {action_failures}")
        print(f"  Invariant fails: {invariant_failures}")

        if invariant_failures > 0:
            print(f"\nFailed steps (replay sequences):")
            for i, s in enumerate(self.history):
                if s.action_succeeded and s.report and not s.report.passed:
                    replay = [h.action_id for h in self.history[:i+1]]
                    print(f"\n  After: {s.action_id}")
                    print(f"  Replay: {replay}")
                    for f in s.report.failures:
                        print(f"    ✗ {f.name}: {f.message}")

    def export_failures(self) -> list[dict]:
        failures = []
        for i, s in enumerate(self.history):
            if s.action_succeeded and s.report and not s.report.passed:
                failures.append({
                    "after_action":       s.action_id,
                    "description":        s.description,
                    "replay_sequence":    [h.action_id for h in self.history[:i+1]],
                    "invariant_failures": [
                        {"name": f.name, "message": f.message, "details": f.details}
                        for f in s.report.failures
                    ],
                    "timestamp": s.timestamp,
                })
        return failures


# ---------------------------------------------------------------------------
# Weighted sampling
# ---------------------------------------------------------------------------

def _weighted_sample(elements: list[ElementDescriptor]) -> ElementDescriptor:
    weights = []
    for el in elements:
        if el.interaction == "fill":
            weights.append(5)
        elif el.testid.startswith("input-select-") and el.tag == "input":
            weights.append(8)  # plain text inputs — highest priority for stress testing
        elif el.testid.startswith("input-select-"):
            weights.append(5)  # dropdowns, spinbuttons, booleans
        elif el.testid == "add-item-required":
            weights.append(30)  # highest — this triggers the empty key bug
        elif el.testid.startswith("add-item-") or el.testid.startswith("add-property-"):
            weights.append(8)
        elif el.testid.startswith("expand-"):
            weights.append(2)
        elif el.testid.startswith("collapse-"):
            weights.append(3)
        elif el.testid.startswith("rename-"):
            weights.append(4)
        elif el.testid.startswith("remove-property-"):
            weights.append(3)
        elif el.testid.startswith("mode-active-"):
            weights.append(15)
        else:
            weights.append(1)
    return random.choices(elements, weights=weights, k=1)[0]


# ---------------------------------------------------------------------------
# run_demo
# ---------------------------------------------------------------------------

def run_demo():
    import sys

    # Tee output to both terminal and log file
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
    print("Select run mode:")
    print("  [1] Automatic  — run all steps without pausing")
    print("  [2] Interactive — pause after each step, pick actions manually")
    while True:
        choice = input("> ").strip()
        if choice in ("1", "2"):
            break
        print("  Please enter 1 or 2.")
    interactive = (choice == "2")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page    = browser.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})

        setup_test_panel_via_localstorage(page)
        page.wait_for_timeout(2000)
        page.evaluate("document.body.style.zoom = '0.75'")
        print("✓ Page loaded")

        dismiss_dialog_if_present(page)

        page.get_by_role("button", name="Example Schemas").click()
        page.wait_for_timeout(1000)
        page.get_by_role("option", name="Autonomous Vehicle Schema").click()
        page.wait_for_timeout(1000)
        print("✓ Autonomous Vehicle Schema loaded")

        page.get_by_text("Data", exact=True).click()
        page.wait_for_timeout(1000)
        page.get_by_text("Text View", exact=True).click()
        page.wait_for_timeout(500)

        with page.expect_file_chooser() as fc_info:
            page.locator("[data-icon='folder-open']").click()
            page.get_by_text("Open JSON/YAML Data").click()
        fc_info.value.set_files(str(DATA_DIR / "self_driving_vehicle_data.json"))
        page.wait_for_timeout(1000)
        print("✓ Data file loaded")

        print("\n=== Baseline invariant check ===")
        checker = InvariantChecker(page, SessionMode.DataEditor)
        checker.run_all().print()

        loop = MutationLoop(page, SessionMode.DataEditor)
        # Read schema the same way INV-D6 does — via DataEditor test panel after app is initialized
        loop._cached_schema = TestPanel(page, SessionMode.DataEditor).get_schema()
        print(f"✓ Schema cached: {list(loop._cached_schema.get('properties', {}).keys())}")

        print("\n=== DOM Discovery (before loop) ===")
        discoverer = DOMDiscoverer(page)
        discoverer.print_discovered()

        loop.run_random(n=100, dynamic=True, interactive=interactive)

        loop.print_summary()

        from tests.mutation_testing.log_exporter import export_html_log
        export_html_log(loop.history, path="mutation_log.html")
        loop.export_history("history.json")

        log_file.close()
        sys.stdout = sys.__stdout__

        input("\nPress Enter to close browser...")
        browser.close()


if __name__ == "__main__":
    run_demo()