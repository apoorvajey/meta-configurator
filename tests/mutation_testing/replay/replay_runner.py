"""Replay a recorded mutation-loop history to reproduce and diagnose failures.

By default the whole history is replayed in one quiet pass; when a step's invariants fail, the full evidence is
printed (with a before/after diff for Text View mismatches) and repeats of an identical failure are collapsed into
a summary at the end.

--history PATH (a history.json or a run folder; default and --latest: the newest run). --since and --folder replay
several histories in a row. --until N (or --pause-at N) runs quietly through step N, then switches to manual
stepping ([Enter], [N], [a], [q]) with the browser kept open; --interactive is manual from the first step and
writes replay_log.html. Schema and data come from the history's meta, so histories without it cannot be replayed
(--schema is disabled).

State snapshots: the full before/after state (the build_snapshot() dict) of every failing step is written to
<replay folder>/states/step_{n}_before.json and step_{n}_after.json, n being the step's 0-based position in "steps".
The live loop writes the same files into runs/<run_id>/states, and the report uses them for the exact diff.
Disable with --no-state-snapshots.

Fidelity: replay uses the loop's viewport, zoom and setup, re-applies each step's recorded seed and exact input
value, mirrors the loop's scroll before failing-step screenshots and its checking rules (no invariant check after a
failed action, no temporal check right after an undo/redo that ended in a state the run had not been in before, which a repair leaves behind). The first step where the
replay provably leaves the original run is reported as "REPLAY DIVERGED". Histories without recorded seeds are
replayed best-effort, with a warning.

Repairs: a step that triggered a repair in the original run ("repairs" in its record) is repaired again on replay
(_rerun_repairs), against the failure replay sees. The recorded clicks are not replayed. If the failure or the invalid
schema does not occur on replay, the repair is "not_needed"; if it ends in another state than the original's, the
next step's state check reports the divergence. The replay's own repairs are written to its history.json.

Each replay writes into runs/<run_id>/replays/<replay_id>/ (see README, Output) and never changes the run's own
history.json. Only a COMPLETE replay (not stopped early, no --until) replaces the original in `report` and sets the
run's _STATUS__replayed-* file.
"""

from __future__ import annotations

import contextlib
import io
import json
import argparse
import os
import random
import shutil
import sys
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
    document_diffs,
    effect_console_lines,
    effect_records,
    read_documents,
    build_snapshot,
    prepare_app_page,
    undo_redo_label,
    scroll_to_validation_error,
)
from tests.mutation_testing.loop.action_weights import _MODE_SWITCH_TESTID, _UNDO_REDO_TESTIDS
from tests.mutation_testing.loop.mutation_loop import _resolve_governing_schema
from tests.mutation_testing.loop.loop_limits import LoopLimits
from tests.mutation_testing.loop.repair_engine import (
    INVERSE_KEY, KIND_CLEAR_TEXT, KIND_FAILURE, KIND_INVALID_SCHEMA, RepairEngine, capture_inverse, repair_block,
)
from tests.invariants.schema_validity import schema_meta_violations
from tests.mutation_testing.runs.step_result import StepResult
from tests.shared.python.test_panel import TestPanel
from tests.invariants.invariants_core import InvariantChecker, _diff_nested
from tests.invariants.temporal_invariants import TemporalInvariantChecker
from tests.mutation_testing.runs.run_layout import (
    REPLAYS,
    RUNS_DIR,
    has_only_known_bugs,
    is_clean,
    original_history,
    resolve_history_path,
    run_dir_of,
    run_id_of,
    mark_run_dir,
)
from tests.mutation_testing.runs.history_io import (
    SCHEMA_CHOICES,  # noqa: F401  (unused while --schema is disabled)
    _cause_fields,
    _derive_mutation_log_path,
    _facet_hashes,
    export_replay_history,
    find_histories,
    find_latest_history,
    load_history,
    parse_since,
    resolve_schema_for_replay,
)
from tests.mutation_testing.runs.known_bugs import KnownBugMatcher, failure_details, invariant_id
from tests.mutation_testing.runs.reproducibility import (
    ReplayOutcome,
    _dump_state_snapshot,
    _state_hash,
    check_reproducibility,
    reproducibility_status,
)

class _Tee:
    """File-like object that writes to several streams (terminal and console.log)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text: str) -> int:
        for s in self.streams:
            s.write(text)
        return len(text)

    def flush(self) -> None:
        for s in self.streams:
            s.flush()

    def isatty(self) -> bool:
        return False


@contextlib.contextmanager
def _console_tee(path):
    """Everything printed inside the block also goes to `path`; stdout is restored afterwards, even on an error."""
    original = sys.stdout
    with open(path, "w", encoding="utf-8") as f:
        sys.stdout = _Tee(original, f)
        try:
            yield
        finally:
            sys.stdout = original


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


class _BarStdout:
    """Stands in for sys.stdout while a replay bar is shown: clears the bar before anything else is written and
    redraws it after every complete line, so the bar always sits on the last terminal line."""

    def __init__(self, real, bar: "_ReplayBar"):
        self._real = real
        self._bar = bar

    def write(self, text: str) -> int:
        self._bar.clear()
        n = self._real.write(text)
        if text.endswith("\n"):
            self._bar.draw()
        return n

    def flush(self) -> None:
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _ReplayBar:
    """Progress bar of a quiet replay, drawn like the loop's bar: '[replay] [████░░░░] 7/20  ·  1 failing step(s)'.

    Shown only on a real terminal, in a quiet replay (not verbose, not interactive, no --until pause) and not under
    the batch runner (MT_STEP_MARKERS), which draws its own bar. Everything the replay prints still appears; the bar
    is cleared before each write and redrawn after it. maybe_start() swaps sys.stdout, stop() restores it."""

    WIDTH = 20

    def __init__(self, real, term):
        self.real = real      # what sys.stdout was (the terminal, or a _Tee of terminal and console.log)
        self.term = term      # the terminal itself: the bar is drawn here only, never into console.log
        self.proxy = _BarStdout(real, self)
        self.step = self.total = self.failing = self.known = 0
        self._len = 0

    @staticmethod
    def maybe_start(verbose: bool, interactive: bool, pause_at) -> "_ReplayBar | None":
        if verbose or interactive or pause_at or os.environ.get("MT_STEP_MARKERS"):
            return None
        real = sys.stdout
        term = real.streams[0] if isinstance(real, _Tee) and real.streams else real
        try:
            if not term.isatty():
                return None
        except Exception:
            return None
        bar = _ReplayBar(real, term)
        sys.stdout = bar.proxy
        return bar

    def text(self) -> str:
        if not self.total:
            return ""
        filled = min(self.WIDTH, round(self.WIDTH * self.step / self.total))
        text = (f"  [replay] [{'█' * filled}{'░' * (self.WIDTH - filled)}] {self.step}/{self.total}"
                f"  ·  {self.failing} failing step(s)")
        if self.known:
            text += f", {self.known} known bug step(s)"
        return text[:max(20, shutil.get_terminal_size((100, 20)).columns - 1)]

    def draw(self) -> None:
        text = self.text()
        if text:
            self.term.write("\r" + text.ljust(self._len))
            self.term.flush()
            self._len = len(text)

    def clear(self) -> None:
        if self._len:
            self.term.write("\r" + " " * self._len + "\r")
            self.term.flush()
            self._len = 0

    def update(self, step: int, total: int, failing: int, known: int) -> None:
        self.step, self.total, self.failing, self.known = step, total, failing, known
        self.draw()

    def stop(self) -> None:
        self.clear()
        if sys.stdout is self.proxy:
            sys.stdout = self.real


class ReplayRunner:
    """Re-executes a recorded history against the app and checks the invariants after each step."""

    def __init__(self, page, history: list[dict], interactive: bool = False, pause_at: int | None = None,
                 dataset: str | None = None, run_id: str | None = None,
                 mirror_screenshot_scroll: bool = True, limits: LoopLimits | None = None):
        """dataset and run_id are stamped onto replayed snapshots, as the loop does for its own.

        limits: the original run's repair limits (resolved_settings["loop_limits"]); the repairs run again with them.

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
        self._pre_edit_snapshot: dict | None = None   # snapshot from before the edit that _prev_snapshot records (see the loop)
        self._seen_state_hashes: set = set()          # as in the loop: states after non-undo/redo actions
        self._use_counts: dict[str, int] = {}         # as in the loop: uses of each action category so far (nth_use)
        self.replayed_snapshots: list[dict] = []
        self.limits = limits or LoopLimits()
        # The replayed steps in the loop's form, which the repair reads to find the last known-good state
        self._repair_history: list[StepResult] = []

    def _rerun_repairs(self, step: dict, el: ElementDescriptor, report, action_succeeded: bool,
                       snapshot_step: int, show: bool) -> list[dict]:
        """Run again the repairs the original run made on this step, against what replay sees now.

        The recorded clicks are not replayed: the repair is decided again from the live state, as in the loop.
        - failure repair: only if one of the recorded persisting failures fails now; otherwise "not_needed".
        - clear-text: the undo always follows a successful clear-text action.
        - invalid schema: only if the schema is invalid now; otherwise "not_needed".
        Returns the replay's own repair blocks, one per recorded block, and leaves self._prev_snapshot at the post-repair
        state, as the loop does. If the repair ends in a different state than the original's, the next step's state
        check reports the divergence; this only prints a note.
        """
        engine = RepairEngine(self.page, lambda: InvariantChecker(self.page, SessionMode.DataEditor).run_all(),
                              self.limits, self._repair_history,
                              say=(print if show else (lambda _text: None)))
        testid = step["testid"]
        out: list[dict] = []
        for rec in step.get("repairs") or []:
            kind = rec.get("kind")
            block, last_testid = None, testid
            if kind == KIND_FAILURE:
                wanted = {invariant_id(n) for n in rec.get("persisting_failures", [])}
                persisting = {r.name for r in report.failures if invariant_id(r.name) in wanted} if report else set()
                if action_succeeded and persisting:
                    info = engine.repair(persisting, testid)
                    clicks = info["steps"]
                    extra = {} if info["succeeded"] else {"still_failing": info["still_failing"]}
                    block = repair_block(KIND_FAILURE, clicks, "succeeded" if info["succeeded"] else "gave_up",
                                         persisting_failures=sorted(persisting), **extra)
                    if clicks:
                        last_testid = clicks[-1]["testid"]
                    else:
                        engine.run_checks()   # as the loop: the zero-click case checks once more
            elif kind == KIND_CLEAR_TEXT:
                if action_succeeded:
                    info = engine.undo_clear_text(el.mode)
                    clicks = info["steps"]
                    block = repair_block(KIND_CLEAR_TEXT, clicks, "done" if info["succeeded"] else "failed",
                                         description_prefix=testid)
                    last_testid = clicks[-1]["testid"]
            elif kind == KIND_INVALID_SCHEMA:
                try:
                    invalid = bool(schema_meta_violations(self.page, TestPanel(self.page, SessionMode.DataEditor).get_schema()))
                except Exception:
                    invalid = False
                if action_succeeded and invalid:
                    info = engine.recover_invalid_schema(rec.get("reason", ""))
                    block = repair_block(KIND_INVALID_SCHEMA, info["steps"], "recovered" if info["recovered"] else "gave_up",
                                         reason=rec.get("reason", ""), mode_switches=info["mode_switches"])
                    last_testid = info["last_testid"]
            else:
                print(f"   Unknown repair kind {kind!r} in the history - skipped")
                continue

            if block is None:
                block = {"kind": kind, "method": "undo", "outcome": "not_needed", "clicks": [], "undos": 0, "redos": 0}
                if kind == KIND_FAILURE:
                    block["persisting_failures"] = rec.get("persisting_failures", [])
                print(f"   Repair ({kind}) not needed on replay: the original run's "
                      + {"failure": "persisting failure", "clear_text": "clear-text action",
                         "invalid_schema": "invalid schema"}.get(kind, kind)
                      + " did not occur here. Later steps may differ from the original run.")
            else:
                self._prev_snapshot = build_snapshot(self.page, last_testid, snapshot_step, self.run_id, self.dataset)
                self.replayed_snapshots.append({**self._prev_snapshot, "baseline": True})   # as the loop writes it to the trace
                block["state_hash_after"] = _state_hash(self._prev_snapshot)
                block["facet_hashes"] = _facet_hashes(self._prev_snapshot)
                if show:
                    print(f"   Repair ({kind}) run again: {block['outcome']} ({block['undos']} undo, {block['redos']} redo, {block.get('reversals', 0)} reversal)")
                want = rec.get("state_hash_after")
                if want and want != block["state_hash_after"]:
                    print(f"   Note: the state after the {kind} repair differs from the original run "
                          f"(original: {rec.get('outcome')}, {rec.get('undos', 0)} undo, {rec.get('redos', 0)} redo; "
                          f"replay: {block['outcome']}, {block['undos']} undo, {block['redos']} redo). "
                          f"Steps after it are compared with the original run's states.")
            out.append(block)
        return out

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

    def _capture_step_screenshot(self, screenshot_dir: str, orig_idx: int) -> str | None:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        shot_path = f"{screenshot_dir}/step_{orig_idx + 1:03d}.png"
        try:
            dialog_shot = getattr(self.executor, "rename_conflict_shot", None)   # taken while the dialog was open
            if dialog_shot:
                Path(shot_path).write_bytes(dialog_shot)
            else:
                self.page.screenshot(path=shot_path, full_page=True)
            return shot_path
        except Exception as e:
            print(f"   Screenshot failed: {e}")
            return None

    def run(self, *args, **kwargs) -> "ReplayOutcome":
        """Replay the history; see _run_impl for the arguments. A quiet replay on a terminal shows a progress bar."""
        bar = _ReplayBar.maybe_start(kwargs.get("verbose", True), self.interactive, self.pause_at)
        self._bar = bar
        try:
            return self._run_impl(*args, **kwargs)
        finally:
            self._bar = None
            if bar:
                bar.stop()

    def _run_impl(self, until: int | None = None,
            pause_on_failure: bool = True, pause_at_end: bool = True,
            export_log: bool = True, log_path: str = "replay_log.html", verbose: bool = True,
            analyze_failures: bool = True,
            capture_screenshots: bool = True, screenshot_dir: str = "replay_screenshots/unnamed",
            progress_every: int = 10,
            source_history_path: str | None = None,
            source_mutation_log_path: str | None = None,
            schema_option_name: str | None = None,
            data_file_name: str | None = None,
            save_state_snapshots: bool = True,
            state_snapshot_dir: str = "states",
            collect_cause_records: dict[int, dict] | None = None,
            include: list[int] | None = None,
            target_invariant_name: str | None = None,
            stop_on_reproduction: bool = False,
            target_bug_id: str | None = None) -> ReplayOutcome:
        """Replay the history and return a ReplayOutcome.

        include / target_invariant_name / stop_on_reproduction (used by the test case reducer): include is a list of
        0-based positions in the history; only those steps run (ignores `until`) and a failed action that succeeded
        originally marks the outcome invalid and ends the replay. target_invariant_name is an invariant id
        (e.g. "INV-S1"): the outcome records whether and where it first failed (reproduced, reproduced_at_step), and
        stop_on_reproduction ends the replay at that step. target_bug_id (e.g. "BUG-09"): the target invariant only
        counts as reproduced when the known-bug matcher assigns that bug to its failure, so a candidate that fails the
        same invariant for another bug (same invariant, different action that started the failure) does not count.

        analyze_failures: when not verbose, the full report of a failure is printed only if it is new or changed
        since that invariant last failed; repeats are tallied and summarised at the end. This affects the console only.
        capture_screenshots: one full-page screenshot per step, whatever the outcome (this does not change page state).
        log_path: where the HTML log is written when export_log is True.
        source_history_path / source_mutation_log_path: shown in the HTML log header (optional).
        save_state_snapshots / state_snapshot_dir: for each step that fails during this replay, write the full
        before/after state to state_snapshot_dir/step_{orig_idx}_{before,after}.json, orig_idx being the step's
        0-based position in the original "steps" list. "before" is None for the first step or after a failed action.
        collect_cause_records: if a dict is passed, it is filled with {orig_idx: cause fields} for every replayed
        step (see _cause_fields, plus replay_passed and replay_faithful).

        self.pause_at (set at construction or typed at a prompt) turns on manual mode once display_i + 1 reaches it,
        even mid-run; all per-step output is gated on `verbose or self.interactive` so full detail starts from there.

        Divergence: the first step where replay provably left the original run is printed as "REPLAY DIVERGED" and
        returned as ReplayOutcome.diverged_at. It is detected from the recorded state hash, from an action that
        succeeded in only one of the two runs, and from add-property generating a different name.
        """
        from tests.mutation_testing.runs.step_result import StepResult
        from tests.mutation_testing.reports.log_exporter import export_html_log

        if include is not None:
            keep = set(include)
            indexed_steps = [(i, s) for i, s in enumerate(self.history) if i in keep]
        else:
            end = until if until else len(self.history)
            indexed_steps = list(enumerate(self.history[:end]))

        total = len(indexed_steps)
        if verbose:
            print(f"\n=== Replaying {total} steps ===")
            if self.interactive:
                print("   [Enter] Next step   [N] Run automatically until step N, then pause   [a] Auto to the end   [q] Quit\n")

        repro = check_reproducibility([s for _, s in indexed_steps])
        if not repro.ok:
            print(f"   Not reproducible from this log: {repro.reason}")

        replay_history = []
        self.replay_history = replay_history  # read by replay_compare after the run
        failing_steps = 0
        known_bug_steps = 0   # steps that failed only on known bugs: not counted in failing_steps, not printed
        known_matcher = KnownBugMatcher()
        state_snapshots_saved = 0
        last_signature: dict[str, tuple[str, str]] = {}
        first_seen_step: dict[str, int] = {}
        last_seen_step: dict[str, int] = {}
        occurrence_count: dict[str, int] = {}
        choice = ""
        diverged_at: int | None = None
        quit_early = False
        resumed_auto = False  # set by [a] at the manual prompt
        steps_completed = 0
        reproduced = False
        reproduced_at_step: int | None = None
        invalid_reason: str | None = None
        repairs_rerun = repairs_not_needed = 0

        # Starting state, as in the loop: step 1 is checked against it, and an undo back to the start is a known state
        try:
            start = build_snapshot(self.page, "baseline", 0, self.run_id, self.dataset)
            self.replayed_snapshots.append({**start, "baseline": True})   # as the loop writes it to the trace
            self._prev_snapshot = start
            self._seen_state_hashes.add(_state_hash(start))
        except Exception as e:
            print(f"   Could not record the starting state ({e}) - the first step gets no temporal check")

        for display_i, (orig_idx, step) in enumerate(indexed_steps):
            # Set before any `continue`/`break`, so it also counts a step skipped for having no testid
            steps_completed = display_i + 1
            testid = step["testid"]
            value = _replay_value(step)
            mode = step["mode"]
            desc = step["description"]
            orig_succeeded = step["action_succeeded"]
            passed = step["passed"]

            # State before this step, kept because self._prev_snapshot is overwritten below
            step_before_snapshot = self._prev_snapshot
            curr_snapshot = None

            orig_status = "✓" if passed else "✗" if orig_succeeded else "!"

            # Switch to manual mode before printing, so the step that reaches pause_at already gets full detail
            if self.pause_at and display_i + 1 >= self.pause_at:
                self.interactive = True

            show = verbose or self.interactive

            bar = getattr(self, "_bar", None)
            if bar:
                if self.interactive:   # manual stepping starts: the prompts need a plain terminal
                    bar.stop()
                    self._bar = bar = None
                else:
                    bar.update(display_i + 1, total, failing_steps, known_bug_steps)

            if show:
                print(f"\n--- Step {display_i + 1}/{total} (orig index {orig_idx}) [orig: {orig_status}] ---")
                print(f"   {desc}")
                print(f"   testid: {testid}  value: {repr(value)}  mode: {mode}"
                      + (f"  seed: {step['step_seed']}" if step.get("step_seed") is not None else ""))
            elif progress_every and not bar and (display_i + 1) % progress_every == 0:
                print(f"   ... step {display_i + 1}/{total} ...")

            if not testid:
                if show:
                    print("   No testid — skipping")
                continue

            el = self._element_for_step(step)

            # Document before the action, to report what it changed (same rule as the loop: not for
            # expand/collapse, undo/redo or the tab switch). Kept in the replay log; printed when detail is shown.
            element_mode = SessionMode.SchemaEditor if el.mode == "schemaEditor" else SessionMode.DataEditor
            doc_before = None
            replay_effect = None
            if not testid.startswith(("expand-", "collapse-")) and testid != _MODE_SWITCH_TESTID:
                try:
                    doc_before = read_documents(self.page)
                except Exception:
                    doc_before = None

            # Execute
            action_succeeded = True
            action_error = None
            try:
                inverse = capture_inverse(self.page, testid)   # text from before a Text View edit, as in the loop
            except Exception:
                inverse = None
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
                if include is not None and orig_succeeded:
                    invalid_reason = f"step {display_i + 1} ({testid}) failed: {action_error}"
                    break   # a candidate whose action cannot run does not test what it was meant to test

            if action_succeeded and doc_before is not None:
                try:
                    limit = 5 if testid in _UNDO_REDO_TESTIDS else 3
                    diffs = document_diffs(doc_before, read_documents(self.page),
                                           "schema" if el.mode == "schemaEditor" else "data")
                    replay_effect = effect_records(diffs, limit) or None
                    if show:
                        print("\n".join(effect_console_lines(diffs, limit=limit, label=undo_redo_label(testid))))
                except Exception:
                    pass


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
            shot_path = self._capture_step_screenshot(screenshot_dir, orig_idx) if capture_screenshots else None

            # Snapshot, divergence check, temporal check
            if action_succeeded:
                try:
                    curr_snapshot = build_snapshot(
                        self.page, testid, step.get("step") or orig_idx + 1,
                        self.run_id, self.dataset,
                    )
                    curr_snapshot["nth_use"] = self._count_use(curr_snapshot)   # as the loop: '#first' / '#later' rules need it
                    self.replayed_snapshots.append(curr_snapshot)
                    expected_hash = step.get("state_hash")
                    expected_facets = step.get("facet_hashes")
                    if expected_facets:
                        now_facets = _facet_hashes(curr_snapshot)
                        state_differs = any(now_facets.get(k) != v for k, v in expected_facets.items()
                                            if k != "se_schema")
                    else:
                        state_differs = bool(expected_hash) and _state_hash(curr_snapshot) != expected_hash
                    if diverged_at is None and include is None and state_differs:
                        diverged_at = display_i + 1
                        print(f"\n   REPLAY DIVERGED at step {display_i + 1} (orig index {orig_idx}, "
                              f"testid={testid}): state differs from the original run. "
                              f"Everything after this is not a faithful replay.")

                    prev_action = (self._prev_snapshot or {}).get("action", "")
                    undo_settled = (prev_action in _UNDO_REDO_TESTIDS
                                    and _state_hash(self._prev_snapshot) in self._seen_state_hashes)
                    if prev_action in _UNDO_REDO_TESTIDS and not undo_settled:
                        if show:
                            print(f"   Temporal check suppressed — previous action was '{prev_action}'")
                    elif self._prev_snapshot is not None and report is not None:
                        temporal_report = TemporalInvariantChecker(
                            prev=self._prev_snapshot, curr=curr_snapshot, action=testid,
                            pre_edit=self._pre_edit_snapshot).run_all()
                        if temporal_report.results:
                            report.results.extend(temporal_report.results)
                    self._pre_edit_snapshot = self._prev_snapshot
                    self._prev_snapshot = curr_snapshot
                    if testid not in _UNDO_REDO_TESTIDS:
                        self._seen_state_hashes.add(_state_hash(curr_snapshot))
                except Exception as e:
                    print(f"   Snapshot/temporal check error at step {display_i + 1}: {e}")
                    self._pre_edit_snapshot = self._prev_snapshot
                    self._prev_snapshot = None
            else:
                # As in the loop: no snapshot after a failed step, so the next step skips its temporal check
                self._pre_edit_snapshot = self._prev_snapshot
                self._prev_snapshot = None

            # The repairs this step triggered in the original run, run again against what replay sees. The step goes
            # into the repair history first: the walk-back looks for the last known-good state in it, as in the loop.
            self._repair_history.append(StepResult(
                action_id=f"dynamic:{testid}", description=desc,
                kwargs={"testid": testid, "el_mode": el.mode, INVERSE_KEY: inverse}, action_succeeded=action_succeeded,
                action_error=action_error, report=report, snapshot=curr_snapshot))
            replayed_repairs: list[dict] = []
            if step.get("repairs"):
                replayed_repairs = self._rerun_repairs(step, el, report, action_succeeded,
                                                       step.get("step") or orig_idx + 1, show)
                for b in replayed_repairs:
                    if b["outcome"] == "not_needed":
                        repairs_not_needed += 1
                    else:
                        repairs_rerun += 1

            # Cause-tracking record, written to the replay history (the report is final here)
            if collect_cause_records is not None:
                cause = _cause_fields(report, curr_snapshot)
                cause["repairs"] = replayed_repairs or None   # the replay's own repairs replace the original's in its history
                cause["replay_passed"] = None if report is None else report.passed
                cause["replay_faithful"] = diverged_at is None or display_i + 1 < diverged_at
                collect_cause_records[orig_idx] = cause

            # Reporting and pausing
            changed = True
            known: dict[str, str] = {}
            if report is not None:
                known, other = known_matcher.observe(
                    step.get("action_id", ""), [r.name for r in report.failures],
                    [r.name for r in report.results if not getattr(r, "skipped", False)],
                    failure_details(report.failures))
                known_only = not report.passed and not other
                if not report.passed:
                    if known_only:
                        known_bug_steps += 1
                    else:
                        failing_steps += 1

                    # Keyed on orig_idx, the numbering the report uses
                    if save_state_snapshots:
                        snap_dir = Path(state_snapshot_dir)
                        _dump_state_snapshot(step_before_snapshot, snap_dir / f"step_{orig_idx}_before.json")
                        _dump_state_snapshot(curr_snapshot, snap_dir / f"step_{orig_idx}_after.json")
                        if curr_snapshot is not None:
                            state_snapshots_saved += 1

                if show:
                    report.print()
                    if shot_path:
                        print(f"   Screenshot: {shot_path}")
                elif analyze_failures and not report.passed and not known_only:
                    changed = False
                    for r in report.failures:
                        if r.name.partition(" ")[0] in known:
                            continue   # known bug in a step that also fails something else
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

                if not report.passed and pause_on_failure and changed and not known_only:
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

            if target_invariant_name and report is not None and not reproduced:
                if any(r.name.partition(" ")[0] == target_invariant_name
                       and (target_bug_id is None or known.get(target_invariant_name) == target_bug_id)
                       for r in report.failures):
                    reproduced = True
                    reproduced_at_step = orig_idx + 1   # 1-based, in the original history
                    if stop_on_reproduction:
                        break

            replay_history.append(StepResult(
                action_id=f"replay:{testid}", description=desc, known_bugs=known,
                # The log escapes "value", so it must be a str; value_repr keeps the typed value
                kwargs={"testid": testid,
                        "value": "" if value is None else str(value),
                        "value_repr": repr(value) if value is not None else None,
                        "observed_value": step.get("observed_value"),
                        "observed_effect": replay_effect,
                        "repairs": replayed_repairs or None},
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
                    elif choice in ("a", "auto"):
                        # Back to automatic: no further pause point, run quietly to the end
                        self.pause_at = None
                        self.interactive = False
                        resumed_auto = True
                        print(f"   Running automatically to the end (step {total})...")
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
                        print("   [Enter] Next step   [N] Run until step N   [a] Auto to the end   [q] Quit")
                if choice == "q":
                    break

            if display_i == total - 1:
                if verbose or self.interactive or resumed_auto:
                    print("\nReplay complete.")
                if pause_at_end and (verbose or self.interactive or resumed_auto):
                    input("Press Enter to close...")

        if not verbose and analyze_failures and occurrence_count:
            print(f"\n{'=' * 60}\nFailure summary (repeats collapsed above):")
            for name, count in occurrence_count.items():
                first, last = first_seen_step[name], last_seen_step[name]
                span = f"step {first}" if first == last else f"steps {first}–{last}"
                print(f"  {name}: {count} occurrence(s), {span}")

        if repairs_rerun or repairs_not_needed:
            print(f"\nRepairs the original run made: {repairs_rerun + repairs_not_needed}; run again on replay: "
                  f"{repairs_rerun}; not needed on replay (the failure or invalid schema did not occur): "
                  f"{repairs_not_needed}. Steps after a repair that ended in a different state show up as a divergence.")

        if save_state_snapshots and state_snapshots_saved:
            print(f"\nState snapshots for {state_snapshots_saved} failing step(s) → "
                  f"{Path(state_snapshot_dir)}")

        if export_log:
            export_html_log(replay_history, path=log_path,
                            source_history_path=source_history_path,
                            source_mutation_log_path=source_mutation_log_path,
                            schema_option_name=schema_option_name,
                            data_file=data_file_name,
                            render_state_diffs=save_state_snapshots)

        return ReplayOutcome(
            steps_run=total,
            failing_steps=failing_steps, known_bug_steps=known_bug_steps, diverged_at=diverged_at,
            state_snapshots_saved=state_snapshots_saved,
            quit_early=quit_early, steps_completed=steps_completed,
            repairs_rerun=repairs_rerun, repairs_not_needed=repairs_not_needed,
            reproduced=reproduced, reproduced_at_step=reproduced_at_step,
            invalid=invalid_reason is not None, invalid_reason=invalid_reason,
        )

    def _count_use(self, snapshot: dict) -> int | None:
        """The loop's nth_use (RecordingMixin._count_use): how many steps of this snapshot's action category (with tab)
        the replay has had up to and including this one; None for the first snapshot, which has no previous one.
        Rules limited to the first or to the later uses of a category ('...#first') only apply with this number."""
        if self._prev_snapshot is None:
            return None
        from tests.mutation_testing.inference.categories import _tabbed_category
        category = _tabbed_category(self._prev_snapshot, snapshot)
        self._use_counts[category] = self._use_counts.get(category, 0) + 1
        return self._use_counts[category]

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
                       help="Replay the newest tests/output/runs/{run_id}/history.json (the default without --history).")
    parser.add_argument("--until", type=int,
                        help="Run quietly through this step, then switch to manual stepping ([Enter]/[N]/[q]) with the "
                             "browser kept open. Ignored with --interactive.")
    parser.add_argument("--interactive", action="store_true",
                        help="Manual step-through from the first step; prints every step and writes replay_log.html.")
    parser.add_argument("--pause-at", type=int,
                        help="Same as --until; if both are given, the earlier one wins.")
    # --schema is disabled because it would override the schema stored in every history of a batch.
    parser.set_defaults(schema=None)
    parser.add_argument("--html", action="store_true", help=argparse.SUPPRESS)  # no-op: the log is always written
    parser.add_argument("--pause-on-failure", action="store_true",
                        help="Pause (Enter/q) whenever a step's invariants fail; the browser stays interactive.")
    parser.add_argument("--no-update-traces", dest="update_traces", action="store_false",
                        help="Do not replace this run's snapshots in tests/output/traces/ with the replayed ones "
                             "(by default a complete replay replaces them; the original file is backed up first)")
    parser.add_argument("--clean-runs", choices=("ask", "skip", "include"), default=None,
                        help="What to do with a run that is already clean (original or its replay has no failures). "
                             "ask: prompt before replaying it (default for one replay). skip: leave it out. "
                             "include: replay it anyway. In batch mode (--since/--folder) the default is skip.")
    parser.add_argument("--known-bug-runs", choices=("ask", "skip", "include"), default=None,
                        help="What to do with a run that only contains known bugs (no other failures). "
                             "ask: prompt before replaying it (default for one replay). skip: leave it out. "
                             "include: replay it anyway (default in batch mode, --since/--folder).")
    parser.add_argument("--no-state-snapshots", action="store_true",
                        help="Do not write before/after app state of failing steps to the replay's states/ folder.")
    parser.add_argument("--since", metavar="TIME",
                        help="Batch mode: replay every run that started at or after TIME, oldest first. TIME is a "
                             "run_id (20260921_121528), a date/time (2026-09-21, '2026-09-21 12:15') or an age "
                             "(90m, 6h, 2d). Searches runs/ unless --folder is given.")
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
        history_path = resolve_history_path(Path(args.history))
        if history_path is None:
            print(f"History file not found: {args.history}")
            return
        if original_history(history_path) != history_path:
            print(f"{history_path} is the result of a replay; replaying the run it came from instead.")
            history_path = original_history(history_path)
    else:
        history_path = find_latest_history()
        if history_path is None:
            print(f"No mutation loop run found — looked for "
                  f"{RUNS_DIR.resolve()}\\<run_id>\\history.json. Run this from the "
                  f"same folder you run the mutation loop from, or pass --history PATH.")
            return
        print(f"Latest run: {history_path.parent.name}")

    if is_clean(history_path):   # nothing left to confirm; ask unless --clean-runs says otherwise
        mode = getattr(args, "clean_runs", None) or "ask"
        folder = history_path.resolve().parent.name
        if mode == "skip":
            print(f"{folder} is already clean - skipped (use --clean-runs include to replay it anyway).")
            return
        if mode == "ask":
            try:
                answer = input(f"{folder} is already clean. Replay it anyway? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("Skipped.")
                return

    if has_only_known_bugs(history_path):   # not clean, not failures: ask unless --known-bug-runs says otherwise
        mode = getattr(args, "known_bug_runs", None) or "ask"
        folder = history_path.resolve().parent.name
        if mode == "skip":
            print(f"{folder} only contains known bugs - skipped (use --known-bug-runs include to replay it anyway).")
            return
        if mode == "ask":
            try:
                answer = input(f"{folder}: Run only contains known bugs. Continue? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("Skipped.")
                return

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
    # Match the original run's valid/invalid mix; older histories without these keys keep the defaults
    from tests.mutation_testing.discovery.values import set_valid_probability
    set_valid_probability(
        gui=resolved_settings.get("valid_value_probability", 0.5),
        text_view=resolved_settings.get("text_view_valid_value_probability", 0.8),
    )
    # run_random() takes screenshots by default, so assume they were on if not recorded
    orig_screenshots = resolved_settings.get("screenshots_enabled", True)
    # Without a stored run_id, use the history's folder name
    folder_name = history_path.resolve().parent.name
    orig_run_id = resolved_settings.get("run_id") or run_id_of(folder_name) or folder_name
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
        print("   State snapshots for failing steps → states/ in the replay folder "
              "(disable with --no-state-snapshots)")

    # The replay lives inside the run it replays: tests/output/runs/<run_id>/replays/<replay_id>/
    replay_id = time.strftime("%Y%m%d_%H%M%S")
    loc = run_dir_of(history_abs_path)
    run_dir = loc[0] if loc else None
    replay_dir = (run_dir / REPLAYS if run_dir else RUNS_DIR / "_loose_replays") / replay_id
    replay_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(replay_dir / "replay_log.html")
    print(f"   Replay session → {replay_dir}")

    with sync_playwright() as pw, _console_tee(replay_dir / "console.log"):
        # Same viewport, zoom and setup order as the loop (prepare_app_page)
        headless = bool(getattr(args, "headless", False))
        browser = pw.chromium.launch(headless=headless, args=[] if headless else ["--start-maximized"])
        page = browser.new_page(viewport=VIEWPORT) if headless else browser.new_page(no_viewport=True)
        prepare_app_page(page, schema_option_name, data_file_name)

        # The loop runs a baseline check first, which hovers over invalid rows; repeat it for the same page state
        with contextlib.redirect_stdout(io.StringIO()):
            InvariantChecker(page, SessionMode.DataEditor).run_all()

        runner = ReplayRunner(page, history, interactive=args.interactive, pause_at=effective_pause_at,
                              dataset=dataset, run_id=orig_run_id,
                              mirror_screenshot_scroll=orig_screenshots,
                              limits=LoopLimits(**{k: v for k, v in (resolved_settings.get("loop_limits") or {}).items()
                                                   if k in LoopLimits.__dataclass_fields__}))

        cause_records: dict[int, dict] = {}  # always collected: it is the replay's own history
        common_snapshot_kwargs = dict(
            screenshot_dir=str(replay_dir / "screenshots"),
            save_state_snapshots=not args.no_state_snapshots,
            state_snapshot_dir=str(replay_dir / "states"),
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
                                 export_log=True, log_path=log_path, verbose=False, analyze_failures=True,
                                 source_history_path=str(history_abs_path),
                                 source_mutation_log_path=source_mutation_log_path,
                                 schema_option_name=schema_option_name,
                                 data_file_name=data_file_name,
                                 **common_snapshot_kwargs)

        partial = outcome.stopped_early   # complete = every step ran, however the replay was driven
        replay_hist_path = export_replay_history(history_path, cause_records, replay_dir,
                                                 replay_id, orig_run_id, complete=not partial)
        from tests.mutation_testing.runs.run_status import write_run_status
        write_run_status(replay_hist_path)
        print(f"\nReplay history → {replay_hist_path}")
        print("   Partial replay: the report keeps reading the original run." if partial else
              "   Next: python -m tests.cli report   (this replay now stands in for the original run)")

        trace_effect: dict = {"skipped": "--no-update-traces"}
        if args.update_traces:
            if partial:
                trace_effect = {"skipped": "partial replay"}
                print("Traces not updated: a partial replay would cut the run's trace short.")
            elif not dataset or not orig_run_id or not runner.replayed_snapshots:
                trace_effect = {"skipped": "no dataset, run id or snapshots"}
                print("Traces not updated: no dataset, run id or snapshots to write.")
            else:
                from tests.mutation_testing.traces.trace_store import replace_run
                if outcome.diverged_at is not None:
                    print(f"Replay diverged at step {outcome.diverged_at}; the whole replay replaces the original trace.")
                removed = replace_run(dataset, orig_run_id, runner.replayed_snapshots)
                trace_effect = {"removed": removed, "added": len(runner.replayed_snapshots)}
        if not partial:
            from tests.mutation_testing.replay.replay_compare import compare_and_report
            compare_and_report(history, runner, outcome, orig_run_id, dataset, trace_effect, out_dir=replay_dir)

        print(f"\n{'=' * 60}")
        status = reproducibility_status(repro, outcome)
        if status == "not_reproducible":
            print(f"Not reproducible from this log — {repro.reason}. Divergence results "
                  f"below are best-effort only.")

        if outcome.stopped_early:
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
            if outcome.known_bug_steps:
                print(f"Known-bug steps: {outcome.known_bug_steps} (not counted as failures)")
            if outcome.failing_steps == 0 and outcome.known_bug_steps:
                print(f"No invariant failures apart from known bugs in the {outcome.steps_run} steps.")
            elif outcome.failing_steps == 0:
                print(f"All {outcome.steps_run} steps passed — no invariant failures.")
            else:
                print(f"{outcome.failing_steps}/{outcome.steps_run} step(s) failed — see analysis above.")

        browser.close()

        if run_dir is not None:
            mark_run_dir(run_dir)   # _STATUS__replayed-clean.json etc. in the run folder; the folder is not renamed

        return outcome


def run_batch(args) -> None:
    since = parse_since(args.since) if args.since else None
    root = Path(args.folder) if args.folder else RUNS_DIR
    paths = find_histories(root, since, explicit_folder=bool(args.folder))

    if not paths:
        print(f"No history.json found under {root.resolve()}"
              + (f" at or after {since:%Y-%m-%d %H:%M:%S}" if since else "") + ".")
        return

    clean_skipped = []
    if (args.clean_runs or "skip") == "skip":
        clean_skipped = [p for p in paths if is_clean(p)]
        paths = [p for p in paths if p not in clean_skipped]
        if clean_skipped:
            print(f"Skipping {len(clean_skipped)} clean run(s) (--clean-runs include replays them too).")
        if not paths:
            print("Nothing to replay: all runs are clean.")
            return

    known_skipped = []
    if args.known_bug_runs == "skip":   # in batch mode the default is include
        known_skipped = [p for p in paths if has_only_known_bugs(p)]
        paths = [p for p in paths if p not in known_skipped]
        if known_skipped:
            print(f"Skipping {len(known_skipped)} run(s) that only contain known bugs.")
        if not paths:
            print("Nothing to replay: all runs are clean or only contain known bugs.")
            return

    print(f"Batch replay: {len(paths)} history file(s) from {root.resolve()}"
          + (f", since {since:%Y-%m-%d %H:%M:%S}" if since else ""))

    results = []
    for i, path in enumerate(paths, 1):
        print(f"\n{'#' * 60}\n# [{i}/{len(paths)}] {path}\n{'#' * 60}")
        run_args = argparse.Namespace(**{**vars(args), "history": str(path), "latest": False, "clean_runs": "include",
                                                 "known_bug_runs": "include"})
        try:
            results.append((path, replay_one(run_args), None))
        except KeyboardInterrupt:
            print("\nBatch interrupted by user.")
            break
        except Exception as e:  # one broken history must not stop the batch
            print(f"{path.parent.name}: {type(e).__name__}: {e}")
            results.append((path, None, f"{type(e).__name__}: {e}"))

    print(f"\n{'=' * 60}\nBatch summary ({len(results)}/{len(paths)} replayed"
          + (f", {len(clean_skipped)} clean skipped" if clean_skipped else "") + ")")
    for path, outcome, err in results:
        if err:
            status = f"error — {err}"
        elif outcome is None:
            status = "skipped (could not load or resolve schema — see above)"
        elif outcome.failing_steps == 0 and outcome.known_bug_steps:
            status = f"known bugs only ({outcome.known_bug_steps}/{outcome.steps_run} steps)"
        elif outcome.failing_steps == 0:
            status = f"clean ({outcome.steps_run} steps)"
        else:
            status = f"{outcome.failing_steps}/{outcome.steps_run} step(s) failed"
        if outcome is not None and outcome.diverged_at is not None:
            status += f", diverged at step {outcome.diverged_at}"
        print(f"  {path.parent.name}: {status}")


if __name__ == "__main__":
    main()