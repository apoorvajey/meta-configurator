"""Replay a recorded mutation-loop history to reproduce and diagnose failures.

By default the whole history is replayed in one quiet pass. Whenever a step's invariants fail, the full
evidence is printed, with a before/after diff for Text View mismatches. Repeats of an identical failure are
collapsed into a summary at the end.

Which history: --history PATH, or the newest mutation_history/{run_id}/history.json (also --latest).
--since and --folder replay several histories in a row.

--until N (or --pause-at N) runs quietly through step N, then switches to manual stepping ([Enter], [N], [q])
with the browser kept open. --interactive is manual from the first step and writes replay_log.html.

Schema and data come from the history's meta; histories without it cannot be replayed (--schema is disabled).

State snapshots: for every failing step the full before/after app state (the build_snapshot() dict) is written
to replay_snapshots/{run_id}/step_{n}_before.json and step_{n}_after.json, where n is the step's 0-based position
in "steps". The live loop writes the same files when it finishes, and the report uses them to show the exact
diff. Disable with --no-state-snapshots.

Fidelity: replay uses the same viewport, zoom and setup as the loop, re-applies each step's recorded seed and
exact input value, mirrors the loop's scroll before failing-step screenshots, and applies the same checking rules
(no invariant check after a failed action, no temporal check after repair steps or undo/redo). The first step
where the replay provably leaves the original run is reported as "REPLAY DIVERGED". Histories without recorded
seeds are replayed on a best-effort basis, with a warning.

    python -m tests.mutation_testing.replay_runner                       # latest run
    python -m tests.mutation_testing.replay_runner --history history.json
    python -m tests.mutation_testing.replay_runner --latest --until 50   # quiet to step 50, then manual
    python -m tests.mutation_testing.replay_runner --latest --interactive
    python -m tests.mutation_testing.replay_runner --since 6h --headless

--export-cause-history writes a copy of the history enriched with the cause-tracking fields the report needs
for "where it likely started" (checked_invariants, failed_invariants, failure_evidence, facet_hashes); the
original file is not modified:

    python -m tests.mutation_testing.replay_runner --history mutation_history/20260921_121528/history.json --export-cause-history
"""

from __future__ import annotations

import contextlib
import io
import json
import argparse
import random
import shutil
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
from tests.shared.python.utils import SessionMode
from tests.mutation_testing.actions.executor import InteractionExecutor
from tests.mutation_testing.discovery.element import ElementDescriptor
from tests.mutation_testing.discovery.page_capture import js_click_for_testid
from tests.mutation_testing.loop.app_setup import (
    VIEWPORT,
    _dataset_slug,
    build_snapshot,
    prepare_app_page,
    scroll_to_validation_error,
)
from tests.mutation_testing.loop.action_weights import _UNDO_REDO_TESTIDS
from tests.mutation_testing.mutation_loop import _resolve_governing_schema
from tests.invariants.invariants_core import InvariantChecker
from tests.invariants.temporal_invariants import TemporalInvariantChecker
from tests.mutation_testing.replay.history_io import (
    DEFAULT_CAUSE_HISTORY_DIR,
    DEFAULT_STATE_SNAPSHOT_DIR,
    MUTATION_HISTORY_DIR,
    SCHEMA_CHOICES,  # noqa: F401  (needed to re-enable --schema below)
    _cause_fields,
    _derive_mutation_log_path,
    export_enriched_history,
    find_histories,
    find_latest_history,
    load_history,
    parse_since,
    resolve_schema_for_replay,
)
from tests.mutation_testing.replay.reproducibility import (
    ReplayOutcome,
    _dump_state_snapshot,
    _is_repair_record,
    _state_hash,
    check_reproducibility,
    reproducibility_status,
)

def _replay_value(step: dict):
    """The value to pass to executor.execute() for this record.

    "input_value" holds the exact value the loop passed (None included). A rename is the exception: the
    executor generated the new name and recorded it in "value", which replay must reuse. Records without
    "input_value" only have the stringified "value", where "" means no value.
    """
    if "input_value" in step:
        value = step["input_value"]
        if value is None and step.get("testid", "").startswith("rename-") and step.get("value"):
            return step["value"]
        return value
    return step["value"] if step.get("value") else None


class ReplayRunner:
    """Re-executes a recorded history against the app and checks the invariants after each step."""

    def __init__(self, page, history: list[dict], interactive: bool = False, pause_at: int | None = None,
                 dataset: str | None = None, run_id: str | None = None,
                 mirror_screenshot_scroll: bool = True):
        """dataset and run_id are stamped onto replayed snapshots, as the loop does for its own.

        mirror_screenshot_scroll: the loop scrolls to the first validation-error icon before screenshotting a failing
        step, but only if screenshots were enabled. Pass the original run's resolved_settings.screenshots_enabled so
        replay scrolls at the same points.
        """
        self.page = page
        self.history = history
        self.interactive = interactive
        self.pause_at = pause_at
        self.dataset = dataset
        self.run_id = run_id
        self.mirror_screenshot_scroll = mirror_screenshot_scroll
        self.executor = InteractionExecutor(page)
        self._prev_snapshot: dict | None = None

    def _element_for_step(self, step: dict) -> ElementDescriptor:
        """Rebuild the ElementDescriptor from the record's el_* fields, or infer it from the testid.

        `view` is a read-only property derived from the element, so it matches the original run once testid and mode do.
        """
        testid = step["testid"]
        el = ElementDescriptor(
            testid=testid,
            tag=step.get("el_tag") or "",
            role=step.get("el_role") or "",
            interaction=step.get("el_interaction") or self._infer_interaction(testid),
            label=testid,
        )
        el.mode = step.get("el_mode") or el.mode
        el.js_click = js_click_for_testid(testid)
        return el

    def _capture_step_screenshot(self, screenshot_dir: str, display_i: int) -> str | None:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        shot_path = f"{screenshot_dir}/step_{display_i + 1:03d}.png"
        try:
            self.page.screenshot(path=shot_path, full_page=True)
            return shot_path
        except Exception as e:
            print(f"   Screenshot failed: {e}")
            return None

    def run(self, until: int | None = None,
            pause_on_failure: bool = True, pause_at_end: bool = True,
            export_log: bool = True, log_path: str = "replay_log.html", verbose: bool = True,
            analyze_failures: bool = True,
            capture_screenshots: bool = True, screenshot_dir: str = "replay_screenshots",
            progress_every: int = 10,
            source_history_path: str | None = None,
            source_mutation_log_path: str | None = None,
            schema_option_name: str | None = None,
            data_file_name: str | None = None,
            save_state_snapshots: bool = True,
            state_snapshot_dir: str = DEFAULT_STATE_SNAPSHOT_DIR,
            collect_cause_records: dict[int, dict] | None = None) -> ReplayOutcome:
        """Replay the history and return a ReplayOutcome.

        analyze_failures: when not verbose, the full report of a failure is printed only if it is new or changed
        since that invariant last failed; repeats are tallied and summarised at the end. This affects the console only.
        capture_screenshots: one full-page screenshot per step, whatever the outcome (this does not change page state).
        log_path: where the HTML log is written when export_log is True.
        source_history_path / source_mutation_log_path: shown in the HTML log header (optional).
        save_state_snapshots / state_snapshot_dir: for each step that fails during this replay, write the full
        before/after state to state_snapshot_dir/{run_id}/step_{orig_idx}_{before,after}.json, orig_idx being the step's
        0-based position in the original "steps" list. "before" is None for the first step or after a failed action.
        collect_cause_records: if a dict is passed, it is filled with {orig_idx: cause fields} for every replayed
        step (see _cause_fields, plus replay_passed and replay_faithful).

        self.pause_at (set at construction or typed at a prompt) turns on manual mode once display_i + 1 reaches it,
        even mid-run; all per-step output is gated on `verbose or self.interactive` so full detail starts from there.

        Divergence: the first step where replay provably left the original run is printed as "REPLAY DIVERGED" and
        returned as ReplayOutcome.diverged_at. It is detected from the recorded state hash, from an action that
        succeeded in only one of the two runs, and from add-property generating a different name.
        """
        from tests.mutation_testing.loop.step_result import StepResult
        from tests.mutation_testing.log_exporter import export_html_log

        end = until if until else len(self.history)
        indexed_steps = list(enumerate(self.history[:end]))

        total = len(indexed_steps)
        if verbose:
            print(f"\n=== Replaying {total} steps ===")
            if self.interactive:
                print("   [Enter] Next step   [N] Run automatically until step N, then pause   [q] Quit\n")

        repro = check_reproducibility([s for _, s in indexed_steps])
        if not repro.ok:
            print(f"   Not reproducible from this log: {repro.reason}")

        replay_history = []
        failing_steps = 0
        state_snapshots_saved = 0
        last_signature: dict[str, tuple[str, str]] = {}
        first_seen_step: dict[str, int] = {}
        last_seen_step: dict[str, int] = {}
        occurrence_count: dict[str, int] = {}
        choice = ""
        diverged_at: int | None = None
        quit_early = False
        steps_completed = 0

        for display_i, (orig_idx, step) in enumerate(indexed_steps):
            # Set before any `continue`/`break`, so it also counts a step skipped for having no testid
            steps_completed = display_i + 1
            testid = step["testid"]
            value = _replay_value(step)
            mode = step["mode"]
            desc = step["description"]
            orig_succeeded = step["action_succeeded"]
            passed = step["passed"]
            is_repair = _is_repair_record(step)

            # State before this step, kept because self._prev_snapshot is overwritten below
            step_before_snapshot = self._prev_snapshot
            curr_snapshot = None

            orig_status = "✓" if passed else "✗" if orig_succeeded else "!"

            # Switch to manual mode before printing, so the step that reaches pause_at already gets full detail
            if self.pause_at and display_i + 1 >= self.pause_at:
                self.interactive = True

            show = verbose or self.interactive

            if show:
                print(f"\n--- Step {display_i + 1}/{total} (orig index {orig_idx}) [orig: {orig_status}] ---")
                print(f"   {desc}")
                print(f"   testid: {testid}  value: {repr(value)}  mode: {mode}"
                      + (f"  seed: {step['step_seed']}" if step.get("step_seed") is not None else ""))
            elif progress_every and (display_i + 1) % progress_every == 0:
                print(f"   ... step {display_i + 1}/{total} ...")

            if not testid:
                if show:
                    print("   No testid — skipping")
                continue

            el = self._element_for_step(step)

            # Execute
            action_succeeded = True
            action_error = None
            try:
                # Resolve the schema before seeding, as the loop does
                governing_schema = _resolve_governing_schema(self.page, el.mode)
                step_seed = step.get("step_seed")
                if step_seed is not None:
                    random.seed(step_seed)
                # The executor prints its own diagnostics; silence them unless showing detail
                if show:
                    self.executor.execute(el, value, schema=governing_schema)
                    print("   ✓ Executed")
                else:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.executor.execute(el, value, schema=governing_schema)
            except Exception as e:
                action_succeeded = False
                action_error = str(e)
                print(f"   Execution error at step {display_i + 1} (testid={testid}): {e}")

            # Early divergence signals
            if diverged_at is None and action_succeeded != orig_succeeded:
                diverged_at = display_i + 1
                print(f"\n   REPLAY DIVERGED at step {display_i + 1} (orig index {orig_idx}, "
                      f"testid={testid}): action {'succeeded' if orig_succeeded else 'failed'} "
                      f"in the original run but {'succeeded' if action_succeeded else 'failed'} "
                      f"on replay. Everything after this is not a faithful replay.")

            expected_name = step.get("resolved_property_name")
            if action_succeeded and expected_name and testid.startswith("add-property-"):
                actual_name = getattr(self.executor, "_last_added_property_name", None)
                if actual_name != expected_name:
                    print(f"   add-property created {actual_name!r} on replay, but the original "
                          f"run created {expected_name!r} — later steps that target it will miss.")
                    if diverged_at is None:
                        diverged_at = display_i + 1
                        print(f"\n   REPLAY DIVERGED at step {display_i + 1} (orig index {orig_idx}, "
                              f"testid={testid}): generated property name differs.")

            # Invariant check, only after a successful action, as in the loop
            report = None
            if action_succeeded:
                try:
                    report = InvariantChecker(self.page, SessionMode.DataEditor).run_all()
                    # Mirror the loop's scroll before the screenshot (decided before temporal results are merged)
                    if self.mirror_screenshot_scroll and not report.passed:
                        scroll_to_validation_error(self.page)
                except Exception as e:
                    print(f"   Invariant check error at step {display_i + 1}: {e}")
                    report = None
            elif show:
                print("   Invariant check skipped — action failed (the original run skips it too)")

            # Screenshot (before the snapshot, as in the loop)
            shot_path = self._capture_step_screenshot(screenshot_dir, display_i) if capture_screenshots else None

            # Snapshot, divergence check, temporal check
            if action_succeeded:
                try:
                    curr_snapshot = build_snapshot(
                        self.page, testid, step.get("step") or orig_idx + 1,
                        self.run_id, self.dataset,
                    )
                    expected_hash = step.get("state_hash")
                    if expected_hash and diverged_at is None and _state_hash(curr_snapshot) != expected_hash:
                        diverged_at = display_i + 1
                        print(f"\n   REPLAY DIVERGED at step {display_i + 1} (orig index {orig_idx}, "
                              f"testid={testid}): state differs from the original run. "
                              f"Everything after this is not a faithful replay.")

                    prev_action = (self._prev_snapshot or {}).get("action", "")
                    if is_repair:
                        if show:
                            print("   Temporal check skipped — repair/auto-undo step "
                                  "(the original run never checks these)")
                    elif prev_action in _UNDO_REDO_TESTIDS:
                        if show:
                            print(f"   Temporal check suppressed — previous action was '{prev_action}'")
                    elif self._prev_snapshot is not None and report is not None:
                        temporal_report = TemporalInvariantChecker(
                            prev=self._prev_snapshot, curr=curr_snapshot, action=testid).run_all()
                        if temporal_report.results:
                            report.results.extend(temporal_report.results)
                    self._prev_snapshot = curr_snapshot
                except Exception as e:
                    print(f"   Snapshot/temporal check error at step {display_i + 1}: {e}")
                    self._prev_snapshot = None
            else:
                # As in the loop: no snapshot after a failed step, so the next step skips its temporal check
                self._prev_snapshot = None

            # Cause-tracking record (opt-in; the report is final here)
            if collect_cause_records is not None:
                cause = _cause_fields(report, curr_snapshot)
                cause["replay_passed"] = None if report is None else report.passed
                cause["replay_faithful"] = diverged_at is None or display_i + 1 < diverged_at
                collect_cause_records[orig_idx] = cause

            # Reporting and pausing
            changed = True
            if report is not None:
                if not report.passed:
                    failing_steps += 1

                    # Keyed on orig_idx, the numbering the report uses
                    if save_state_snapshots:
                        snap_dir = Path(state_snapshot_dir) / (self.run_id or "unknown_run")
                        _dump_state_snapshot(step_before_snapshot, snap_dir / f"step_{orig_idx}_before.json")
                        _dump_state_snapshot(curr_snapshot, snap_dir / f"step_{orig_idx}_after.json")
                        if curr_snapshot is not None:
                            state_snapshots_saved += 1

                if show:
                    report.print()
                    if shot_path:
                        print(f"   Screenshot: {shot_path}")
                elif analyze_failures and not report.passed:
                    changed = False
                    for r in report.failures:
                        sig = (r.message, repr(r.details))
                        if last_signature.get(r.name) != sig:
                            changed = True
                        last_signature[r.name] = sig
                        occurrence_count[r.name] = occurrence_count.get(r.name, 0) + 1
                        first_seen_step.setdefault(r.name, display_i + 1)
                        last_seen_step[r.name] = display_i + 1

                    # Reprint only when the failure is new or changed
                    if changed:
                        print(f"\n--- Step {display_i + 1}/{total} (orig index {orig_idx}) — FAILURE ---")
                        print(f"   {desc}")
                        report.print()
                        if shot_path:
                            print(f"   Screenshot: {shot_path}")

                if not report.passed and pause_on_failure and changed:
                    print("\n    Invariant failure — browser is paused.")
                    print("   Inspect the app, then [Enter] to continue or [q] to quit.")
                    try:
                        choice = input("   > ").strip().lower()
                        if choice == "q":
                            quit_early = True
                            steps_completed = display_i + 1
                            print(f"   Stopping replay at step {display_i + 1}/{total} "
                                  f"— {failing_steps} failing step(s) seen so far. This is NOT "
                                  f"a complete run: steps after this one were never checked.")
                            break
                    except EOFError:
                        pass

            replay_history.append(StepResult(
                action_id=f"replay:{testid}", description=desc,
                # The log escapes "value", so it must be a str; value_repr keeps the typed value
                kwargs={"testid": testid,
                        "value": "" if value is None else str(value),
                        "value_repr": repr(value) if value is not None else None,
                        "observed_value": step.get("observed_value")},
                action_succeeded=action_succeeded, action_error=action_error, report=report,
                screenshot_path=shot_path,
                # The log diffs this against the previous entry's snapshot for failing steps
                # (None if the action failed or the check errored)
                snapshot=curr_snapshot,
            ))

            if self.interactive:
                while True:
                    choice = input("   > ").strip().lower()
                    if choice == "q":
                        quit_early = True
                        print(f"   Stopping replay at step {display_i + 1}/{total} "
                              f"— {failing_steps} failing step(s) seen so far. This is NOT "
                              f"a complete run: steps after this one were never checked.")
                        break
                    elif choice == "":
                        break
                    elif choice.isdigit():
                        target = int(choice)
                        if target <= display_i + 1:
                            print(f"   Step {target} is not after the current step "
                                  f"({display_i + 1}) — enter a later step number, [Enter], or [q].")
                            continue
                        self.pause_at = target
                        self.interactive = False
                        print(f"   Running automatically until step {target}...")
                        break
                    else:
                        print("   [Enter] Next step   [N] Run until step N   [q] Quit")
                if choice == "q":
                    break

            if display_i == total - 1:
                if verbose or self.interactive:
                    print("\nReplay complete.")
                if pause_at_end and (verbose or self.interactive):
                    input("Press Enter to close...")

        if not verbose and analyze_failures and occurrence_count:
            print(f"\n{'=' * 60}\nFailure summary (repeats collapsed above):")
            for name, count in occurrence_count.items():
                first, last = first_seen_step[name], last_seen_step[name]
                span = f"step {first}" if first == last else f"steps {first}–{last}"
                print(f"  {name}: {count} occurrence(s), {span}")

        if save_state_snapshots and state_snapshots_saved:
            print(f"\nState snapshots for {state_snapshots_saved} failing step(s) → "
                  f"{Path(state_snapshot_dir) / (self.run_id or 'unknown_run')}")

        if export_log:
            export_html_log(replay_history, path=log_path,
                            source_history_path=source_history_path,
                            source_mutation_log_path=source_mutation_log_path,
                            schema_option_name=schema_option_name,
                            data_file=data_file_name,
                            render_state_diffs=save_state_snapshots)

        return ReplayOutcome(
            steps_run=total,
            failing_steps=failing_steps, diverged_at=diverged_at,
            state_snapshots_saved=state_snapshots_saved,
            quit_early=quit_early, steps_completed=steps_completed,
        )

    def _infer_interaction(self, testid: str) -> str:
        """Guess the interaction from the testid prefix when a record has no el_interaction."""
        fill_prefixes = ("input-string-", "input-number-", "input-boolean-")
        select_prefixes = ("format-selector", "input-select-")
        click_prefixes = ("add-item-", "add-property-", "remove-property-",
                          "rename-property-", "expand-", "collapse-", "mode-active-")
        for p in fill_prefixes:
            if testid.startswith(p):
                return "fill"
        for p in select_prefixes:
            if testid.startswith(p) or testid == p:
                return "select"
        for p in click_prefixes:
            if testid.startswith(p):
                return "click"
        return "click"


# CLI entrypoint
def main():
    parser = argparse.ArgumentParser(description="Replay a mutation loop history and analyze its failures")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--history", help="History JSON file to replay.")
    which.add_argument("--latest", action="store_true",
                       help="Replay the newest mutation_history/{run_id}/history.json (the default without --history).")
    parser.add_argument("--until", type=int,
                        help="Run quietly through this step, then switch to manual stepping ([Enter]/[N]/[q]) with the "
                             "browser kept open. Ignored with --interactive.")
    parser.add_argument("--interactive", action="store_true",
                        help="Manual step-through from the first step; prints every step and writes replay_log.html.")
    parser.add_argument("--pause-at", type=int,
                        help="Same as --until; if both are given, the earlier one wins.")
    # --schema is disabled because it would override the schema stored in every history of a batch.
    # To re-enable: uncomment the add_argument below and remove set_defaults.
    # parser.add_argument("--schema", choices=SCHEMA_CHOICES, default=None,
    #                     help="Required if the history file predates schema/data metadata.")
    parser.set_defaults(schema=None)
    parser.add_argument("--html", action="store_true",
                        help="Also write replay_log.html with screenshots, evidence and the step-by-step history.")
    parser.add_argument("--pause-on-failure", action="store_true",
                        help="Pause (Enter/q) whenever a step's invariants fail; the browser stays interactive.")
    parser.add_argument("--no-state-snapshots", action="store_true",
                        help="Do not write before/after app state of failing steps to replay_snapshots/.")
    parser.add_argument("--state-snapshot-dir", default=DEFAULT_STATE_SNAPSHOT_DIR,
                        help="Where failing-step snapshots are written, as <dir>/<run_id>/step_<n>_{before,after}.json. "
                             f"Default: {DEFAULT_STATE_SNAPSHOT_DIR!r}; it must match state_snapshots.root in "
                             "triage_config.json for the report to find them.")
    parser.add_argument("--export-cause-history", action="store_true",
                        help="Write a copy of the history with cause-tracking fields to "
                             "<cause-history-dir>/<run_id>/history.json, for the report's 'where it likely started' "
                             "section. The original file is not modified.")
    parser.add_argument("--cause-history-dir", default=DEFAULT_CAUSE_HISTORY_DIR,
                        help=f"Where --export-cause-history writes. Default: {DEFAULT_CAUSE_HISTORY_DIR!r}.")
    parser.add_argument("--since", metavar="TIME",
                        help="Batch mode: replay every run that started at or after TIME, oldest first. TIME is a "
                             "run_id (20260921_121528), a date/time (2026-09-21, '2026-09-21 12:15') or an age "
                             "(90m, 6h, 2d). Searches mutation_history/ unless --folder is given.")
    parser.add_argument("--folder", metavar="DIR",
                        help="Batch mode: replay every history.json under DIR. Combine with --since for newer ones only.")
    parser.add_argument("--headless", action="store_true",
                        help="Run without a visible browser window. Not combinable with the manual modes "
                             "(--interactive, --until, --pause-at, --pause-on-failure).")
    args = parser.parse_args()

    if args.headless:
        manual_flags = [f for f, on in (("--interactive", args.interactive), ("--until", args.until),
                                        ("--pause-at", args.pause_at),
                                        ("--pause-on-failure", args.pause_on_failure)) if on]
        if manual_flags:
            parser.error(f"--headless cannot be combined with {', '.join(manual_flags)} (no window to inspect).")

    if args.since or args.folder:
        if args.history or args.latest:
            parser.error("--since/--folder cannot be combined with --history or --latest.")
        manual = [flag for flag, on in (("--interactive", args.interactive),
                                        ("--until", args.until),
                                        ("--pause-at", args.pause_at),
                                        ("--pause-on-failure", args.pause_on_failure)) if on]
        if manual:
            parser.error(f"{', '.join(manual)} would stop a batch waiting for input; "
                         f"use them on a single replay instead.")
        if args.since:
            try:
                parse_since(args.since)
            except ValueError as e:
                parser.error(str(e))
        run_batch(args)
        return

    replay_one(args)


def replay_one(args):
    """Replay a single history (args.history, or the latest run). Returns the ReplayOutcome, or None if it could not start."""
    if args.history:
        history_path = Path(args.history)
        if not history_path.exists():
            print(f"History file not found: {history_path}")
            return
    else:
        history_path = find_latest_history()
        if history_path is None:
            print(f"No mutation loop run found — looked for "
                  f"{MUTATION_HISTORY_DIR.resolve()}\\<run_id>\\history.json. Run this from the "
                  f"same folder you run the mutation loop from, or pass --history PATH.")
            return
        print(f"Latest run: {history_path.parent.name}")

    try:
        history, meta = load_history(history_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read {history_path}: {e}")
        return

    repro = check_reproducibility(history)

    try:
        schema_option_name, data_file_name = resolve_schema_for_replay(meta, args.schema)
    except ValueError as e:
        print(f"{e}")
        return

    resolved_settings = meta.get("resolved_settings") or {}
    # run_random() takes screenshots by default, so assume they were on if not recorded
    orig_screenshots = resolved_settings.get("screenshots_enabled", True)
    # Without a stored run_id, use the history's folder name
    orig_run_id = resolved_settings.get("run_id") or history_path.resolve().parent.name
    dataset = _dataset_slug(schema_option_name, data_file_name)

    history_abs_path = history_path.resolve()
    source_mutation_log_path = _derive_mutation_log_path(history_abs_path)

    # --until and --pause-at both mean "switch to manual mode at this step"; the earlier one wins
    switch_points = [n for n in (args.until, args.pause_at) if n]
    effective_pause_at = min(switch_points) if switch_points else None

    print(f"Loaded {len(history)} steps from {history_path}")
    print(f"   Full path: {history_abs_path}")
    if source_mutation_log_path:
        print(f"   Source mutation log: {source_mutation_log_path}")
    print(f"   Schema/data pair: {schema_option_name} / {data_file_name}")
    print(f"   Reproducibility: {'✓ ' if repro.ok else '! '}{repro.reason}")
    print(f"   Original run: run_id={orig_run_id or '?'}, screenshots_enabled={orig_screenshots}"
          + ("" if resolved_settings else " (not recorded — assumed)"))
    if effective_pause_at and not args.interactive:
        print(f"   Running quietly through step {effective_pause_at}, then switching to manual "
              f"exploration — browser stays open until you quit or confirm at the end.")
    if not args.interactive:
        print("   Running quietly — full analysis will print at each failing step.")
    if not args.no_state_snapshots:
        print(f"   State snapshots for failing steps → {args.state_snapshot_dir}/{orig_run_id or '?'}/ "
              f"(disable with --no-state-snapshots)")

    will_export_log = args.interactive or args.html
    log_path = "replay_log.html"  # only used if the log is exported
    if will_export_log:
        replay_id = time.strftime("%Y%m%d_%H%M%S")
        replay_dir = Path("replay_logs") / replay_id
        replay_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(history_path, replay_dir / "history.json")
        log_path = str(replay_dir / "replay_log.html")
        print(f"   Replay session → {replay_dir}")

    with sync_playwright() as pw:
        # Same viewport, zoom and setup order as the loop (prepare_app_page)
        browser = pw.chromium.launch(headless=bool(getattr(args, "headless", False)))
        page = browser.new_page()
        page.set_viewport_size(VIEWPORT)
        prepare_app_page(page, schema_option_name, data_file_name)

        # The loop runs a baseline check first, which hovers over invalid rows; repeat it for the same page state
        with contextlib.redirect_stdout(io.StringIO()):
            InvariantChecker(page, SessionMode.DataEditor).run_all()

        runner = ReplayRunner(page, history, interactive=args.interactive, pause_at=effective_pause_at,
                              dataset=dataset, run_id=orig_run_id,
                              mirror_screenshot_scroll=orig_screenshots)

        cause_records: dict[int, dict] | None = {} if args.export_cause_history else None
        common_snapshot_kwargs = dict(
            save_state_snapshots=not args.no_state_snapshots,
            state_snapshot_dir=args.state_snapshot_dir,
            collect_cause_records=cause_records,
        )

        if args.interactive:
            outcome = runner.run(log_path=log_path,
                                 source_history_path=str(history_abs_path),
                                 source_mutation_log_path=source_mutation_log_path,
                                 schema_option_name=schema_option_name,
                                 data_file_name=data_file_name,
                                 **common_snapshot_kwargs)
        else:
            outcome = runner.run(pause_on_failure=args.pause_on_failure,
                                 pause_at_end=effective_pause_at is not None,
                                 export_log=args.html, log_path=log_path, verbose=False, analyze_failures=True,
                                 source_history_path=str(history_abs_path),
                                 source_mutation_log_path=source_mutation_log_path,
                                 schema_option_name=schema_option_name,
                                 data_file_name=data_file_name,
                                 **common_snapshot_kwargs)

        if cause_records is not None:
            out_path = export_enriched_history(history_path, cause_records,
                                               out_root=args.cause_history_dir, run_id=orig_run_id)
            unfaithful = sum(1 for r in cause_records.values() if r.get("replay_faithful") is False)
            print(f"\nCause-tracking history → {out_path}")
            print(f"   {len(cause_records)} of {len(history)} steps enriched"
                  + (f"; {unfaithful} step(s) at/after the point where the replay diverged are "
                     f"flagged replay_faithful=false" if unfaithful else ""))
            print(f"   Next: python -m tests.cli report {args.cause_history_dir}")

        print(f"\n{'=' * 60}")
        status = reproducibility_status(repro, outcome)
        if status == "not_reproducible":
            print(f"Not reproducible from this log — {repro.reason}. Divergence results "
                  f"below are best-effort only.")

        if outcome.quit_early:
            # Claims are scoped to steps_completed: a replay stopped early says nothing about later steps
            print(f"Replay stopped early by user at step {outcome.steps_completed}/{outcome.steps_run} "
                  f"— this is a PARTIAL result, not a completed replay.")
            if outcome.diverged_at is None:
                print(f"No divergence detected in the {outcome.steps_completed} step(s) actually replayed.")
            else:
                print(f"Replay diverged from the original run at step {outcome.diverged_at} — "
                      f"results after that step are not a faithful reproduction.")
            if outcome.failing_steps == 0:
                print(f"No invariant failures in the {outcome.steps_completed} step(s) replayed so far "
                      f"— the remaining {outcome.steps_run - outcome.steps_completed} step(s) were never "
                      f"checked. Re-run without quitting early (or use --pause-on-failure instead of "
                      f"--interactive) to get a real answer for the full run.")
            else:
                print(f"{outcome.failing_steps} failing step(s) seen in the "
                      f"{outcome.steps_completed} step(s) replayed so far — see analysis above. "
                      f"({outcome.steps_run - outcome.steps_completed} step(s) after that were never checked.)")
        else:
            if outcome.diverged_at is None:
                print("No divergence detected — replay tracked the original run throughout.")
            else:
                print(f"Replay diverged from the original run at step {outcome.diverged_at} — "
                      f"results after that step are not a faithful reproduction.")
            if outcome.failing_steps == 0:
                print(f"All {outcome.steps_run} steps passed — no invariant failures.")
            else:
                print(f"{outcome.failing_steps}/{outcome.steps_run} step(s) failed — see analysis above.")

        browser.close()

        return outcome


def run_batch(args) -> None:
    since = parse_since(args.since) if args.since else None
    root = Path(args.folder) if args.folder else MUTATION_HISTORY_DIR
    paths = find_histories(root, since, explicit_folder=bool(args.folder))

    if not paths:
        print(f"No history.json found under {root.resolve()}"
              + (f" at or after {since:%Y-%m-%d %H:%M:%S}" if since else "") + ".")
        return

    print(f"Batch replay: {len(paths)} history file(s) from {root.resolve()}"
          + (f", since {since:%Y-%m-%d %H:%M:%S}" if since else ""))

    results = []
    for i, path in enumerate(paths, 1):
        print(f"\n{'#' * 60}\n# [{i}/{len(paths)}] {path}\n{'#' * 60}")
        run_args = argparse.Namespace(**{**vars(args), "history": str(path), "latest": False})
        try:
            results.append((path, replay_one(run_args), None))
        except KeyboardInterrupt:
            print("\nBatch interrupted by user.")
            break
        except Exception as e:  # one broken history must not stop the batch
            print(f"{path.parent.name}: {type(e).__name__}: {e}")
            results.append((path, None, f"{type(e).__name__}: {e}"))

    print(f"\n{'=' * 60}\nBatch summary ({len(results)}/{len(paths)} replayed)")
    for path, outcome, err in results:
        if err:
            status = f"error — {err}"
        elif outcome is None:
            status = "skipped (could not load or resolve schema — see above)"
        elif outcome.failing_steps == 0:
            status = f"clean ({outcome.steps_run} steps)"
        else:
            status = f"{outcome.failing_steps}/{outcome.steps_run} step(s) failed"
        if outcome is not None and outcome.diverged_at is not None:
            status += f", diverged at step {outcome.diverged_at}"
        print(f"  {path.parent.name}: {status}")


if __name__ == "__main__":
    main()