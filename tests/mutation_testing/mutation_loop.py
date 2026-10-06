"""Random schema-aware mutation loop: discover an action, execute it, check the invariants, repair, record."""

from __future__ import annotations

import os
import time
import random
from playwright.sync_api import Page, sync_playwright
from tests.shared.python.utils import SessionMode
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.discovery.discoverer import DOMDiscoverer
from tests.mutation_testing.discovery.values import ValueGenerator
from tests.mutation_testing.actions.executor import InteractionExecutor
from tests.mutation_testing.discovery.element import ElementDescriptor, CATEGORY_LABELS
from tests.mutation_testing.discovery.schema_resolution import _resolve_governing_schema
from tests.mutation_testing.discovery.paths import testid_path_string_to_list
from tests.invariants.invariants_core import InvariantChecker, _diff_nested
from tests.invariants.temporal_invariants import TemporalInvariantChecker
from tests.mutation_testing.loop.action_choice import ActionChoiceMixin
from tests.mutation_testing.loop.recording import RecordingMixin
from tests.mutation_testing.loop.repair import RepairMixin
from tests.mutation_testing.loop.reporting import ReportingMixin
from tests.mutation_testing.loop.app_setup import (
    SCHEMA_DATA_PAIRS,
    VIEWPORT,
    _dataset_slug,
    _json_safe,
    _safe_filename_part,
    _short,
    prepare_app_page,
)
from tests.mutation_testing.loop.step_result import StepResult
from tests.mutation_testing.loop.action_weights import (
    _MODE_SWITCH_TESTID,
    _UNDO_REDO_TESTIDS,
    _active_weights_filename,
    _mode_label,
)
from tests.mutation_testing.loop.action_sampling import (
    _focus_key_for_element,
    _update_focus,
    _weighted_sample,
)


class MutationLoop(ActionChoiceMixin, RepairMixin, RecordingMixin, ReportingMixin):

    # Invalid-schema budget: the first MAX_LINGERING_INVALID_EPISODES valid->invalid transitions per run
    # may persist for up to MAX_STEPS_IN_INVALID_SCHEMA further steps before being undone; later ones are
    # undone right after the step that caused them. MAX_RECOVERY_UNDOS caps the undos of one recovery.
    MAX_LINGERING_INVALID_EPISODES = 2
    MAX_STEPS_IN_INVALID_SCHEMA = 3
    MAX_RECOVERY_UNDOS = 6

    def __init__(self, page: Page, mode: SessionMode = SessionMode.DataEditor):
        self.page = page
        self.mode = mode
        self.checker = InvariantChecker(page, mode)
        self.history: list[StepResult] = []
        self._skip_next: bool = False
        self._prev_snapshot: dict | None = None
        self._recent_action_testids: list[str] = []  # last two, for undo/redo chain weighting
        self._prev_failure_names: set[str] = set()  # for detecting persisting failures
        self._repair_already_attempted: set[str] = set()  # failures repair gave up on
        # Invariants evaluated (not skipped) in the last report; "not failing" is not "passing"
        # (INV-D6 is not evaluated in Schema mode).
        self._prev_checked_names: set[str] = set()
        # Invalid-schema bookkeeping (see _update_schema_validity_and_maybe_recover)
        self._schema_valid: bool | None = None
        self._invalid_episodes: int = 0
        self._steps_in_invalid: int = 0
        self._recovery_disabled: bool = False

        # Locality-bias focus: Mode, View and parsed path (a list of str/int segments) of the area worked in
        # recently; reset once focus_age reaches _LOCALITY_DECAY_STEPS without reinforcement.
        self._focus_mode: str | None = None
        self._focus_view: str | None = None
        self._focus_path: list | None = None
        self._focus_age: int = 0

        # Create-then-fill: after a GUI "add property" succeeds, (mode_label, path of the new leaf). Consumed by
        # the next _weighted_sample call only, to edit that leaf next.
        self._pending_create_target: tuple[str, list] | None = None

        # Screenshot settings of the current run, read by _capture_screenshot()
        self._screenshots_enabled: bool = False
        self._screenshots_dir: str = "mutation_screenshots"


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
                  "run_random() — snapshots from this run will have dataset=None and "
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
        self._executor = executor  # print_summary() reads its ensure-visible tally

        try:
            for i in range(n):
                print(f"\n--- Step {i+1}/{n} ---")

                if self._should_skip():
                    print("   Skipped by user")
                    continue

                # Discover
                elements = discoverer.discover()
                if not elements:
                    print("   No interactable elements found — skipping step")
                    continue

                # Pick an action
                el: ElementDescriptor = _weighted_sample(
                    elements, self._recent_action_testids,
                    focus_mode=self._focus_mode, focus_view=self._focus_view,
                    focus_path=self._focus_path, focus_age=self._focus_age,
                    pending_create=self._pending_create_target,
                )
                # One-shot: cleared whether or not _weighted_sample found a match
                self._pending_create_target = None

                # Let the user override the choice
                if interactive:
                    override = self._interactive_action_select(elements, el)
                    if override is None:
                        continue
                    el = override

                print(f"\n   ▶  {el.human_label}")
                print(f"      ({el.interaction}: {el.testid})")

                # Generate the value; the governing schema is the user's schema in the Data Editor and
                # the meta-schema in the Schema Editor.
                element_mode = SessionMode.SchemaEditor if el.mode == "schemaEditor" else SessionMode.DataEditor
                governing_schema = _resolve_governing_schema(self.page, el.mode)
                try:
                    instance = TestPanel(self.page, element_mode).get_data()
                except Exception:
                    instance = {}

                gen = ValueGenerator(schema=governing_schema, instance=instance)
                value = gen.for_element(el)
                if value is not None:
                    print(f"   value: {repr(value)}")

                # Execute
                action_succeeded = True
                action_error     = None
                # Per-step seed, set right before executing so the executor's own random choices can be
                # reproduced on replay (recorded in the step and exported to history.json).
                step_seed = random.SystemRandom().getrandbits(32)
                random.seed(step_seed)
                try:
                    executor.execute(el, value, schema=governing_schema)
                except Exception as e:
                    action_succeeded = False
                    action_error     = str(e)
                    print(f"   Interaction raised: {e}")

                # Text View actions choose their value inside the executor, so record what changed:
                # diff the edited document before/after and keep the first few changes.
                observed_effect = None
                if (action_succeeded and not el.testid.startswith(("expand-", "collapse-"))
                        and el.testid not in _UNDO_REDO_TESTIDS and el.testid != _MODE_SWITCH_TESTID):
                    try:
                        instance_after = TestPanel(self.page, element_mode).get_data()
                        observed_effect = [
                            {"path": p, "before": _short(a), "after": _short(b)}
                            for p, a, b in _diff_nested(instance, instance_after)[:3]
                        ] or None
                    except Exception:
                        observed_effect = None

                # Create-then-fill: target the new leaf on the next step. add-item-required is excluded
                # because it already fills the new entry; bare add-item- (array items) is not handled.
                if (action_succeeded and el.testid.startswith("add-property-")
                        and el.testid != "add-property-"
                        and executor._last_added_property_name):
                    new_leaf_path = (
                        testid_path_string_to_list(executor._last_added_property_parent_path)
                        + [executor._last_added_property_name]
                    )
                    self._pending_create_target = (_mode_label(el), new_leaf_path)

                # Check single-snapshot invariants
                report = None
                if action_succeeded:
                    report = self.checker.run_all()
                    report.print()
                else:
                    print("   Skipping invariant check (interaction failed)")

                # Screenshot
                screenshot_path = None
                if action_succeeded:
                    screenshot_path = self._capture_screenshot(
                        filename=f"step_{i + 1:02d}_{_safe_filename_part(el.testid)}.png",
                        invariants_failed=bool(report and not report.passed),
                    )

                # Pause on invariant failure
                if pause_on_failure and action_succeeded and report and not report.passed:
                    print("\n     Invariant failure — browser is paused.")
                    print("   Inspect the app, then [Enter] to continue or [q] to quit.")
                    try:
                        choice = input("   > ").strip().lower()
                        if choice == "q":
                            raise StopIteration
                    except EOFError:
                        pass

                # Record. "value" is the stringified form; "input_value" is the exact, type-preserved value
                # that replay feeds back in. The el_* fields let replay rebuild the ElementDescriptor.
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
                        "observed_effect": observed_effect,
                        "observed_value": observed_effect[0]["after"] if observed_effect else None,
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

                # Last two action testids, for the undo/redo chain cap
                self._recent_action_testids.append(el.testid)
                self._recent_action_testids = self._recent_action_testids[-2:]

                # Only successful steps move the locality focus
                if action_succeeded:
                    self._focus_mode, self._focus_view, self._focus_path, self._focus_age = _update_focus(
                        self._focus_mode, self._focus_view, self._focus_path, self._focus_age,
                        _focus_key_for_element(el),
                    )

                # Save the state snapshot and run the temporal checks
                if action_succeeded:
                    result.snapshot = self._build_snapshot(el.testid, i + 1)
                    self._append_snapshot(result.snapshot)

                    # No temporal check right after undo/redo: their effect depends on what they reverse, so
                    # comparing the next action against that snapshot would blame it for the undo's changes.
                    prev_action = (self._prev_snapshot or {}).get("action", "")
                    if prev_action in _UNDO_REDO_TESTIDS:
                        print(f"   Temporal check suppressed - previous action was '{prev_action}'")
                    elif self._prev_snapshot is not None:
                        temporal_report = TemporalInvariantChecker(
                            prev=self._prev_snapshot,
                            curr=result.snapshot,
                            action=el.testid,
                        ).run_all()
                        if temporal_report.results:
                            temporal_report.print()
                            # Merge into the main report for the HTML log
                            if report is not None:
                                report.results.extend(temporal_report.results)

                # None after a failed action, so the next step skips its temporal check (replay mirrors this)
                self._prev_snapshot = result.snapshot

                # clear-text empties the whole document and starves later steps of anything to target, so it
                # gets an unconditional one-shot repair: check once, then always undo. The "repair:" prefix keeps
                # [clear-text step, undo step] together as one repair block.
                # Known gap: if clear-text was a no-op on an already empty document, the undo reverts the
                # previous action instead.
                if action_succeeded and el.testid.startswith("clear-text-"):
                    undo_testid = "schema_undo" if el.mode == "schemaEditor" else "undo"
                    print(f"\n   Auto-undoing '{el.testid}' (clear-text is always reverted after its check)")
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
                                "succeeded": undo_report.passed},
                        action_succeeded=True,
                        action_error=None,
                        report=undo_report,
                        screenshot_path=undo_screenshot_path,
                        snapshot=undo_snapshot,
                    )
                    self.history.append(undo_result)

                    # Same bookkeeping as the end of _check_and_repair_persisting_failures, which this step
                    # bypasses: the next step compares against the post-undo state.
                    self._prev_snapshot = undo_snapshot
                    self._repair_already_attempted &= {r.name for r in undo_report.failures}
                    self._prev_failure_names = {r.name for r in undo_report.failures}
                    self._prev_checked_names = {r.name for r in undo_report.results
                                                if not getattr(r, "skipped", False)}
                    report = undo_report

                    print("   restored cleanly" if undo_report.passed
                          else "      restored, but other invariant(s) still failing")
                else:
                    report = self._check_and_repair_persisting_failures(
                        report, el.testid, action_succeeded, i + 1
                    )

                # Invalid-schema budget / forced recovery
                if action_succeeded:
                    self._update_schema_validity_and_maybe_recover(i + 1, result)

                # Interactive pause after the step
                if interactive:
                    self._interactive_pause(i + 1, n, result)

        except StopIteration:
            print(f"\n   Loop stopped early at step {len(self.history)}/{n}")

        self._save_snapshots()


def run_demo():
    import sys

    run_id = time.strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join("mutation_logs", run_id)
    os.makedirs(log_dir, exist_ok=True)

    # Write output to the terminal and to the log file
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

    # run_mode from the config if valid, otherwise ask at the terminal
    configured_mode = resolve_run_mode(run_config)
    if configured_mode is not None:
        choice = {"background": "1", "watch_failures": "2", "interactive": "3"}[configured_mode]
        print(f"[config] run_mode fixed to '{configured_mode}' via run_config.json — skipping prompt")
    else:
        print("Select run mode:")
        print("  [1] Background     — fully hands-free, no pausing")
        print("  [2] Watch failures — pauses only on invariant failure")
        print("  [3] Interactive    — pauses after every step")
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

        # Shared with the replay runner so both set the app up identically
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

        # Settings actually used, stored in history.json's meta so a replay can match them even when
        # run_config.json allows a random choice. Replay reads screenshots_enabled and run_id from here.
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