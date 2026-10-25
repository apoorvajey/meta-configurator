"""Random schema-aware mutation loop: discover an action, execute it, check the invariants, repair, record."""

from __future__ import annotations

import os
import faulthandler
import time
import random
from dataclasses import asdict
from pathlib import Path
from playwright.sync_api import Page, sync_playwright
from tests.mutation_testing.runs.run_layout import RUNS_DIR, mark_run_dir
from tests.shared.python.utils import SessionMode
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.discovery.discoverer import DOMDiscoverer
from tests.mutation_testing.discovery.values import ValueGenerator, get_valid_probability
from tests.mutation_testing.actions.executor import InteractionExecutor
from tests.mutation_testing.discovery.element import ElementDescriptor, CATEGORY_LABELS
from tests.mutation_testing.discovery.schema_resolution import _resolve_governing_schema
from tests.mutation_testing.discovery.paths import testid_path_string_to_list
from tests.invariants.invariants_core import InvariantChecker, _diff_nested
from tests.invariants.temporal_invariants import TemporalInvariantChecker
from tests.mutation_testing.loop.action_choice import ActionChoiceMixin
from tests.mutation_testing.loop.recording import RecordingMixin
from tests.mutation_testing.loop.repair import RepairMixin
from tests.mutation_testing.loop.repair_engine import INVERSE_KEY, capture_inverse
from tests.mutation_testing.loop.reporting import ReportingMixin
from tests.mutation_testing.loop.app_setup import (
    SCHEMA_DATA_PAIRS,
    VIEWPORT,
    _dataset_slug,
    _json_safe,
    _safe_filename_part,
    _short,
    format_effect_lines,
    prepare_app_page,
)
from tests.mutation_testing.runs.step_result import StepResult
from tests.mutation_testing.runs.known_bugs import KnownBugMatcher, invariant_id
from tests.mutation_testing.loop import console as run_console
from tests.mutation_testing.loop.console import RunConsole, log_only, marker, show, surfaced
from tests.mutation_testing.loop.action_weights import (
    _MODE_SWITCH_TESTID,
    _UNDO_REDO_TESTIDS,
    get_active_weights_filename,
    _mode_label,
)
from tests.mutation_testing.loop.action_sampling import (
    LocalityConfig,
    _focus_key_for_element,
    _update_focus,
    _weighted_sample,
    expand_first,
    followup_element,
)
from tests.mutation_testing.loop.loop_limits import LoopLimits


class MutationLoop(ActionChoiceMixin, RepairMixin, RecordingMixin, ReportingMixin):

    # The repair, invalid-schema and stuck-run limits (max_undo_attempts, unchanged_undo_limit, max_stuck_steps,
    # max_lingering_invalid_episodes, max_steps_in_invalid_schema, max_recovery_undos) are in self._limits, set per run
    # from the "loop_limits" block of run_config.json; see loop_limits.LoopLimits for what each one does.

    # Quiet console (see loop/console.py): a progress line every this many steps
    QUIET_PROGRESS_EVERY = 10

    def __init__(self, page: Page, mode: SessionMode = SessionMode.DataEditor):
        self.page = page
        self.mode = mode
        self.checker = InvariantChecker(page, mode)
        self.history: list[StepResult] = []
        self._skip_next: bool = False
        self._prev_snapshot: dict | None = None
        self._recent_action_testids: list[str] = []  # last two, for undo/redo chain weighting
        # Snapshot from before the edit that self._prev_snapshot records: the state an undo right after that edit must
        # restore (inferred undo_inverse rules)
        self._pre_edit_snapshot: dict | None = None
        self._prev_failure_names: set[str] = set()  # for detecting persisting failures
        self._repair_already_attempted: set[str] = set()  # failures repair gave up on
        self._no_repair_noted: set[str] = set()           # do_not_repair known bugs already announced on the console
        self._failures_from_mode_switch: set[str] = set()  # failures that first appeared on a mode switch: never repaired
        # Invariants evaluated (not skipped) in the last report; "not failing" is not "passing"
        # (INV-D6 is not evaluated in Schema mode).
        self._prev_checked_names: set[str] = set()
        # Invalid-schema bookkeeping (see _update_schema_validity_and_maybe_recover)
        self._schema_valid: bool | None = None
        self._invalid_episodes: int = 0
        self._steps_in_invalid: int = 0
        self._recovery_disabled: bool = False

        # Stuck-run bookkeeping (see LoopLimits.max_stuck_steps)
        self._stuck_steps: int = 0
        self._stuck_names: set[str] = set()
        self.stopped_early: str | None = None   # why the run ended before num_steps, if it did

        # Quiet console: what was last printed per failing invariant, so a repeat is not printed again
        self._failure_signature: dict[str, tuple[str, str]] = {}
        self._failure_count: dict[str, int] = {}
        self._failure_first_step: dict[str, int] = {}
        self._failure_last_step: dict[str, int] = {}

        # Known bugs (runs/known_bugs.py): their failures are logged but not printed as failures. A step is
        # "known-bug only" when it has failures of known bugs and none of anything else.
        self._known_bugs = KnownBugMatcher()
        self._steps_with_known: set[int] = set()
        self._steps_with_other: set[int] = set()

        # Locality-bias focus: Mode, View and parsed path (a list of str/int segments) of the area worked in
        # recently; reset once focus_age reaches locality.decay_steps without reinforcement.
        self._focus_mode: str | None = None
        self._focus_view: str | None = None
        self._focus_path: list | None = None
        self._focus_age: int = 0
        self._locality: LocalityConfig = LocalityConfig()   # set per run by run_random(locality=...)
        self._limits: LoopLimits = LoopLimits()             # set per run by run_random(limits=...)

        # Create-then-fill: after a GUI "add property" succeeds, (mode_label, path of the new leaf). Consumed by
        # the next _weighted_sample call only, to edit that leaf next.
        self._pending_create_target: tuple[str, list] | None = None

        # Steps per action category so far in this run (see RecordingMixin._count_use)
        self._use_counts: dict[str, int] = {}

        # Expand-then-add: when the sampled action is hidden in a collapsed node, this step expands the node and the
        # action's testid is kept here for the next step (see expand_first / followup_element).
        self._followup_testid: str | None = None

        # Screenshot settings of the current run, read by _capture_screenshot()
        self._screenshots_enabled: bool = False
        self._screenshots_failures_only: bool = False
        # Failures-only mode: screenshots of the last two logged steps, kept in memory (see _capture_screenshot)
        self._last_shot: bytes | None = None
        self._shot_before: bytes | None = None
        self._screenshots_dir: str = "screenshots"


    def run_random(
            self,
            n: int = 10,
            interactive: bool = False,
            pause_on_failure: bool = False,
            screenshots: bool = True,
            screenshots_dir: str | None = None,
            run_id: str | None = None,
            schema_option_name: str | None = None,
            data_file: str | None = None,
            locality: LocalityConfig | None = None,
            limits: LoopLimits | None = None,
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
            screenshots_dir = screenshots_dir or os.path.join(RUNS_DIR, self.run_id, "screenshots")
            print(f"   Screenshots for this run → {screenshots_dir}")

        self._locality = locality or LocalityConfig()
        self._limits = limits or LoopLimits()
        self._screenshots_enabled = screenshots
        from tests.mutation_testing.loop.profile_config import resolve_screenshots_failures_only
        self._screenshots_failures_only = resolve_screenshots_failures_only()
        self._screenshots_dir = screenshots_dir

        discoverer = DOMDiscoverer(self.page)
        executor   = InteractionExecutor(self.page)
        self._executor = executor  # print_summary() reads its ensure-visible tally

        try:
            for i in range(n):
                if run_console.is_quiet() and i and i % self.QUIET_PROGRESS_EVERY == 0:
                    failing = max(0, sum(1 for s in self.history if s.report is not None and not s.report.passed)
                                  - self.known_bug_only_steps())
                    show(f"   ... step {i}/{n} ({failing} failing so far) ...")
                print(f"\n--- Step {i+1}/{n} ---")
                marker(f"STEP {i+1}/{n}")
                self._arm_hang_watchdog()

                if self._should_skip():
                    print("   Skipped by user")
                    continue

                elements = discoverer.discover()
                if not elements:
                    print("   No interactable elements found — skipping step")
                    continue

                el: ElementDescriptor = _weighted_sample(
                    elements, self._recent_action_testids,
                    focus_mode=self._focus_mode, focus_view=self._focus_view,
                    focus_path=self._focus_path, focus_age=self._focus_age,
                    pending_create=self._pending_create_target,
                    locality=self._locality,
                )
                # One-shot: cleared whether or not _weighted_sample found a match
                self._pending_create_target = None

                # Planned follow-up (expand-then-add): run it if it is now offered, else keep the sampled action
                followup, self._followup_testid = self._followup_testid, None
                el = followup_element(elements, followup) or el

                # Let the user override the choice
                if interactive:
                    override = self._interactive_action_select(elements, el)
                    if override is None:
                        continue
                    el = override

                # A virtual element (its button hides in a collapsed node): this step expands the node and the action
                # itself is the next step, so each step is logged and checked on its own.
                el, self._followup_testid = expand_first(el, elements)

                print(f"\n   ▶  {el.human_label}")
                marker(f"ACTION {el.human_label}")
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

                # Inverse of a Text View edit (the text from before it), for the reverse-action repair; None for other actions
                try:
                    inverse = capture_inverse(self.page, el.testid)
                except Exception:
                    inverse = None

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
                        diffs = _diff_nested(instance, instance_after)
                        observed_effect = [
                            {"path": p, "before": _short(a), "after": _short(b)}
                            for p, a, b in diffs[:3]
                        ] or None
                        print("\n".join(format_effect_lines(diffs)))
                    except Exception:
                        observed_effect = None

                # Create-then-fill: target the new leaf on the next step. add-item-required is excluded
                # because it already fills the new entry, array items are handled in the block below.
                if (action_succeeded and el.testid.startswith("add-property-")
                        and el.testid != "add-property-"
                        and executor._last_added_property_name):
                    new_leaf_path = (
                        testid_path_string_to_list(executor._last_added_property_parent_path)
                        + [executor._last_added_property_name]
                    )
                    self._pending_create_target = (_mode_label(el), new_leaf_path)
                    # Same idea for array items: the GUI appends an empty entry, so target the last index next step.
                    # add-item-required is excluded because it already fills the new entry.
                    if (action_succeeded and el.testid.startswith("add-item-")
                            and el.testid not in ("add-item-", "add-item-required")):
                        try:
                            parent_path = testid_path_string_to_list(el.testid[len("add-item-"):])
                            node = instance_after  # set in the observed_effect block above
                            for seg in parent_path:
                                node = node[seg]
                            if isinstance(node, list) and node:
                                self._pending_create_target = (_mode_label(el), parent_path + [len(node) - 1])
                        except Exception:
                            pass  # instance_after missing or path not resolvable: sample normally

                report = None
                if action_succeeded:
                    report = self.checker.run_all()
                    self._print_report(report, i + 1, n, el.human_label, f"dynamic:{el.testid}")
                else:
                    print("   Skipping invariant check (interaction failed)")

                screenshot_path = None
                if action_succeeded:
                    screenshot_path = self._capture_screenshot(
                        filename=f"step_{i + 1:02d}_{_safe_filename_part(el.testid)}.png",
                        invariants_failed=bool(report and not report.passed),
                    )
                prev_screenshot_path = None
                if screenshot_path and report and not report.passed:
                    prev_screenshot_path = self._save_pre_step_screenshot(
                        filename=f"step_{i + 1:02d}_before_{_safe_filename_part(el.testid)}.png")

                # a step whose only failures are known bugs does not pause the run
                if pause_on_failure and action_succeeded and report and not report.passed and (i + 1) in self._steps_with_other:
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
                        INVERSE_KEY: inverse,
                    },
                    action_succeeded=action_succeeded,
                    action_error=action_error,
                    report=report,
                    screenshot_path=screenshot_path,
                    prev_screenshot_path=prev_screenshot_path,
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
                        _focus_key_for_element(el), self._locality,
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
                            pre_edit=self._pre_edit_snapshot,
                        ).run_all()
                        if temporal_report.results:
                            self._print_report(temporal_report, i + 1, n, el.human_label, f"dynamic:{el.testid}")
                            # Merge into the main report for the HTML log
                            if report is not None:
                                report.results.extend(temporal_report.results)

                # None after a failed action, so the next step skips its temporal check (replay mirrors this)
                self._pre_edit_snapshot = self._prev_snapshot
                self._prev_snapshot = result.snapshot

                # clear-text empties the whole document and starves later steps of anything to target, so it
                # gets an unconditional one-shot repair: check once, then always undo. The undo is recorded on the
                # clear-text step (kwargs["repairs"]), not as a step of its own.
                if action_succeeded and el.testid.startswith("clear-text-"):
                    print(f"\n   Auto-undoing '{el.testid}' (clear-text is always reverted after its check)")
                    undo_report = self._auto_undo_clear_text(el, result, i + 1)
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

                # Abandon a run that is stuck on the same failure
                if not interactive and self._run_is_stuck(report):
                    self.stopped_early = (f"abandoned after step {i + 1}/{n}: {self._stuck_steps} consecutive failing "
                                          f"steps with the same failing invariant ({', '.join(sorted(self._stuck_names))})")
                    show(f"\n   Run {self.stopped_early}")
                    break

                if interactive:
                    self._interactive_pause(i + 1, n, result)

        except StopIteration:
            show(f"\n   Loop stopped early at step {len(self.history)}/{n}")

        faulthandler.cancel_dump_traceback_later()
        self._print_failure_summary(n)
        self._save_snapshots()

    def _arm_hang_watchdog(self, seconds: int = 120) -> None:
        """Diagnostic: if one step takes longer than `seconds`, dump every thread's stack to hang_traceback.log in the run folder."""
        try:
            if getattr(self, "_hang_file", None) is None:
                run_dir = os.path.join(RUNS_DIR, self.run_id)
                os.makedirs(run_dir, exist_ok=True)
                self._hang_file = open(os.path.join(run_dir, "hang_traceback.log"), "w")
            faulthandler.cancel_dump_traceback_later()
            faulthandler.dump_traceback_later(seconds, repeat=False, file=self._hang_file)
        except Exception:
            pass

    def _run_is_stuck(self, report) -> bool:
        """Count consecutive failing steps that share a failing invariant with the step before; True once that reaches
        limits.max_stuck_steps. A step whose action failed (no report) neither counts nor resets."""
        if report is None:
            return False
        names = {r.name for r in report.failures}
        skipped = self._known_bugs.no_repair_ids(names)   # known bugs marked do_not_repair do not count as "stuck"
        names = {n for n in names if invariant_id(n) not in skipped}
        if not names:
            self._stuck_steps = 0
        elif names & self._stuck_names:
            self._stuck_steps += 1
        else:
            self._stuck_steps = 1
        self._stuck_names = names
        return self._limits.max_stuck_steps > 0 and self._stuck_steps >= self._limits.max_stuck_steps

    def _print_report(self, report, step_no: int, total: int, label: str, action_id: str = "") -> None:
        """Print an invariant report. Quiet console: nothing for a passing report; a failing one is printed only when
        a failure is new or changed (same rule as a quiet replay), under a header naming the step and action.
        A report whose failures are all known bugs is treated like a passing one: the full report reaches the log files
        only; a verbose console (interactive, watch) gets one "[known bug]" line instead of a failure block."""
        checked = [r.name for r in report.results if not getattr(r, "skipped", False)]
        known, other = self._known_bugs.observe(action_id, [r.name for r in report.failures], checked)
        if known:
            self._steps_with_known.add(step_no)
        if other:
            self._steps_with_other.add(step_no)
        if known and not other:
            with log_only():
                report.print()   # the log files only, never the terminal
            if not run_console.is_quiet():   # verbose: say so in one line instead of showing a failure
                print("  [known bug] " + ", ".join(f"{inv} ({bug})" for inv, bug in sorted(known.items()))
                      + " - not counted as a failure (details in console.log)")
            return
        if not run_console.is_quiet():
            report.print()
            return
        changed = False
        for r in report.failures:
            if r.name.partition(" ")[0] in known:
                continue   # a known bug in a step that also fails something else: counted apart, not in the summary
            signature = (r.message, repr(r.details))
            if self._failure_signature.get(r.name) != signature:
                changed = True
            self._failure_signature[r.name] = signature
            self._failure_count[r.name] = self._failure_count.get(r.name, 0) + 1
            self._failure_first_step.setdefault(r.name, step_no)
            self._failure_last_step[r.name] = step_no
        if not report.passed:
            marker(f"FAILS {len([r for r in report.failures if r.name.partition(' ')[0] not in known])}")
        if report.passed or not changed:
            report.print()   # quiet console: this reaches only the log files
            return
        show(f"\n--- Step {step_no}/{total} - FAILURE ---")
        show(f"   {label}")
        with surfaced():
            report.print()

    def known_bug_only_steps(self) -> int:
        """Steps whose only failures were known bugs."""
        return len(self._steps_with_known - self._steps_with_other)

    def _print_failure_summary(self, planned_steps: int) -> None:
        """End of run, quiet console only (print_summary() covers the verbose one): totals and the failures with their
        repeats collapsed."""
        if not run_console.is_quiet():
            return
        failing = sum(1 for s in self.history if s.action_succeeded and s.report and not s.report.passed)
        failing -= self.known_bug_only_steps()
        action_errors = sum(1 for s in self.history if not s.action_succeeded)
        show(f"\n{'=' * 60}")
        steps_run = len(self.history)
        show(f"Run finished: {steps_run}/{planned_steps} steps, {failing} with invariant failures, "
             f"{action_errors} action error(s)")
        if self.known_bug_only_steps():
            show(f"Known-bug steps: {self.known_bug_only_steps()} (not counted as failures)")
        if self._failure_count:
            show("Failure summary (repeats collapsed above):")
            for name, count in self._failure_count.items():
                first, last = self._failure_first_step[name], self._failure_last_step[name]
                span = f"step {first}" if first == last else f"steps {first}-{last}"
                show(f"  {name}: {count} occurrence(s), {span}")


def run_demo():
    import sys

    run_id = time.strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join(RUNS_DIR, run_id)   # everything of this run lives here; see run_layout.py
    os.makedirs(log_dir, exist_ok=True)

    # Everything printed goes to tests/output/runs/<run_id>/console.log (kept with the run). The terminal only gets what
    # loop/console.py lets through,
    # unless the run is verbose (--verbose, MT_VERBOSE=1 or "verbose": true in run_config.json).
    terminal = sys.stdout
    run_log_path = os.path.join(log_dir, "console.log")
    run_log = open(run_log_path, "w", encoding="utf-8")
    console = RunConsole(terminal, [run_log], verbose=True)   # verbose until the settings are known
    run_console.install(console)
    sys.stdout = console

    from tests.mutation_testing.loop.profile_config import (
        load_run_config,
        apply_configured_profile,
        apply_configured_valid_probability,
        resolve_locality_config,
        resolve_loop_limits,
        resolve_schema_choice,
        resolve_run_mode,
        resolve_num_steps,
        resolve_screenshots,
        resolve_screenshots_failures_only,
        resolve_verbose,
    )

    run_config = load_run_config()
    wants_verbose = resolve_verbose(run_config)

    # run_mode from the config if valid, otherwise ask at the terminal
    configured_mode = resolve_run_mode(run_config)
    if os.environ.get("MT_BATCH") == "1":
        # A batch run has nobody at the keyboard: whatever the config says, it runs hands-free
        if configured_mode != "background":
            print(f"[batch] run_mode '{configured_mode or 'ask'}' ignored in a batch run - using 'background'")
        configured_mode = "background"
    if configured_mode is not None:
        choice = {"background": "1", "watch_failures": "2", "interactive": "3"}[configured_mode]
        print(f"[config] run_mode fixed to '{configured_mode}' via run_config.json — skipping prompt")
    else:
        console.verbose = True   # the prompt must be visible
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
    # Interactive and watch-failures runs need the full output in front of them
    console.verbose = wants_verbose or interactive or pause_on_failure

    schema_option_name, data_file = resolve_schema_choice(SCHEMA_DATA_PAIRS, run_config)
    n_steps = resolve_num_steps(run_config)
    screenshots_enabled = resolve_screenshots(run_config)
    # The batch runner finds the run folder from the "tests/output/runs/<run_id>" in this line
    show(f"Run {run_id}: {schema_option_name}, {n_steps} steps  "
         f"({'verbose' if console.verbose else 'quiet'} console; full output: {run_log_path})")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
        page = browser.new_page(no_viewport=True)

        # Shared with the replay runner so both set the app up identically
        prepare_app_page(page, schema_option_name, data_file.name)

        print("\n=== Baseline invariant check ===")
        checker = InvariantChecker(page, SessionMode.DataEditor)
        baseline = checker.run_all()
        if baseline.passed:
            baseline.print()
        else:
            with surfaced():   # a failing baseline is shown even on a quiet console
                baseline.print()

        loop = MutationLoop(page, SessionMode.DataEditor)

        print("\n=== DOM Discovery (before loop) ===")
        discoverer = DOMDiscoverer(page)
        elements = discoverer.discover()
        from collections import Counter
        counts = Counter(e.category for e in elements)
        print(f"   {len(elements)} actions discovered:")
        for cat, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"     {CATEGORY_LABELS.get(cat, cat):<25} {count}")
        profile_name = apply_configured_profile(run_config)
        apply_configured_valid_probability(run_config)
        locality = resolve_locality_config(run_config)
        limits = resolve_loop_limits(run_config)
        if run_console.is_quiet():
            show(f"   action-weights profile: {profile_name}")

        from tests.mutation_testing.inference.rule_activity import reset_rule_activity
        reset_rule_activity()
        loop.run_random(n=n_steps, interactive=interactive, pause_on_failure=pause_on_failure,
                        screenshots=screenshots_enabled,
                        run_id=run_id, schema_option_name=schema_option_name, data_file=data_file.name,
                        locality=locality, limits=limits)
        loop.print_summary()

        from tests.mutation_testing.reports.log_exporter import export_html_log
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
            "screenshots_failures_only": resolve_screenshots_failures_only(run_config),
            "weights_profile_file": get_active_weights_filename(),
            "valid_value_probability": get_valid_probability("gui"),
            "text_view_valid_value_probability": get_valid_probability("text_view"),
            "locality_bias": asdict(locality),
            "loop_limits": asdict(limits),
            "stopped_early": loop.stopped_early,
        }

        history_dir = log_dir
        os.makedirs(history_dir, exist_ok=True)
        loop.export_history(os.path.join(history_dir, "history.json"),
                            schema_option_name=schema_option_name,
                            data_file=data_file.name,
                            run_config=run_config,
                            resolved_settings=resolved_settings)
        from tests.mutation_testing.inference.rule_activity import write_rule_activity
        write_rule_activity(history_dir)
        from tests.mutation_testing.runs.run_status import write_run_status
        write_run_status(os.path.join(history_dir, "history.json"))

        # Close every log file before the status file is written
        sys.stdout = terminal
        run_console.uninstall()
        run_log.close()
        mark_run_dir(Path(history_dir))   # writes _STATUS__clean.json | _STATUS__failures.json | _STATUS__incomplete.json into tests/output/runs/<run_id>

        input("\nPress Enter to close browser...")
        browser.close()


if __name__ == "__main__":
    if any(a in ("-h", "--help") for a in os.sys.argv[1:]):   # this script has no options: do not start a run for --help
        print("Starts one mutation-loop run (settings: tests/mutation_testing/config/run_config.json). "
              "Use: python -m tests.cli run --help")
        raise SystemExit(0)
    run_demo()