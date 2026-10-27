"""Repair in the mutation loop: when to repair (persisting failures, clear-text, invalid schema) and how it is recorded.

The repair itself is in repair_engine.RepairEngine, which replay uses too. A repair is not a step of its own: it is
recorded on the step that triggered it (StepResult.kwargs["repairs"]), so replay reruns the repair against the failure
it sees, instead of clicking recorded undos.
"""
from __future__ import annotations

from tests.shared.python.utils import SessionMode
from tests.shared.python.test_panel import TestPanel
from tests.invariants.invariants_core import InvariantReport
from tests.invariants.schema_validity import schema_meta_violations
from tests.mutation_testing.runs.known_bugs import invariant_id
from tests.mutation_testing.runs.history_io import _facet_hashes
from tests.mutation_testing.runs.reproducibility import _state_hash
from tests.mutation_testing.loop.console import show
from tests.mutation_testing.loop.repair_engine import (
    KIND_CLEAR_TEXT, KIND_FAILURE, KIND_INVALID_SCHEMA, RepairEngine, repair_block,
)


# Invariants whose failures never start a repair, although they are reported like any other failure. INV-D7 compares the
# Text View markers with the schema; the app's markers are not reliable (missing after a mode switch, never shown for
# the blank key), so undoing edits to "fix" it only wastes steps.
NO_REPAIR_INVARIANTS = frozenset({"INV-D7", "INV-D8"})


class RepairMixin:
    """Repair of persisting failures, clear-text actions and invalid schemas by undo. The limits come from
    self._limits (loop_limits.LoopLimits)."""

    def _repair_engine(self) -> RepairEngine:
        return RepairEngine(self.page, self.checker.run_all, self._limits, self.history)

    def _note_clicks(self, clicks: list[dict]) -> None:
        """Remember the repair's clicks for the undo/redo chain cap (the last two action testids)."""
        self._recent_action_testids = (self._recent_action_testids + [c["testid"] for c in clicks])[-2:]

    def _record_repair(self, result, block: dict, last_testid: str, step_num: int) -> None:
        """Attach `block` to the step that triggered the repair and make the post-repair state the baseline for the next step.

        The baseline snapshot is written to the trace file tagged "baseline" (the next step is paired with it) but not
        counted as a use of its action category: a repair is not an action the loop chose.
        """
        self._prev_snapshot = self._build_snapshot(last_testid, step_num, count_use=False)
        self._append_baseline(self._prev_snapshot)   # the trace needs it: the next step is paired with this state
        block["state_hash_after"] = _state_hash(self._prev_snapshot)
        block["facet_hashes"] = _facet_hashes(self._prev_snapshot)   # for the reports' "what changed" view
        result.kwargs.setdefault("repairs", []).append(block)

    def _auto_undo_clear_text(self, el, result, step_num: int) -> InvariantReport:
        """clear-text empties the whole document and starves later steps of anything to target, so it gets an
        unconditional one-shot repair: the caller has checked once, this always undoes. Returns the report after the undo."""
        info = self._repair_engine().undo_clear_text(el.mode)
        clicks = info["steps"]
        undo_report = clicks[-1]["report"]
        self._note_clicks(clicks)
        block = repair_block(KIND_CLEAR_TEXT, clicks, "done" if info["succeeded"] else "failed",
                             description_prefix=el.testid)
        self._record_repair(result, block, clicks[-1]["testid"], step_num)
        # Same bookkeeping as the end of _check_and_repair_persisting_failures, which this step bypasses:
        # the next step compares against the post-undo state.
        self._repair_already_attempted &= {r.name for r in undo_report.failures}
        self._prev_failure_names = {r.name for r in undo_report.failures}
        self._prev_checked_names = {r.name for r in undo_report.results if not getattr(r, "skipped", False)}
        return undo_report

    def _recover_invalid_schema(self, step_num: int, reason: str, result) -> bool:
        """Undo in the Schema Editor until the schema is valid against the meta-schema again (see
        RepairEngine.recover_invalid_schema). If that does not get there, forced recovery is switched off until the
        schema is valid again, so a stuck case cannot loop. Returns True if recovered."""
        info = self._repair_engine().recover_invalid_schema(reason)
        clicks = info["steps"]
        self._note_clicks(clicks)
        block = repair_block(KIND_INVALID_SCHEMA, clicks, "recovered" if info["recovered"] else "gave_up",
                             reason=reason, mode_switches=info["mode_switches"])
        if clicks:
            report = clicks[-1]["report"]
            self._prev_failure_names = {r.name for r in report.failures}
            self._prev_checked_names = {r.name for r in report.results if not getattr(r, "skipped", False)}
        self._record_repair(result, block, info["last_testid"], step_num)
        if not info["recovered"]:
            self._recovery_disabled = True
        return info["recovered"]

    def _update_schema_validity_and_maybe_recover(self, step_num: int, result: "StepResult") -> None:
        """Track valid <-> invalid transitions of the edited schema and enforce the invalid-schema budget.

        Users do produce invalid schemas, so they stay in the mutation space - but a single invalid edit
        otherwise contaminates the rest of the run. Each invalid episode is observed (INV-S4 and the
        skipped INV-D6 evidence run in the step that caused it), then undone: after
        max_steps_in_invalid_schema steps for the first max_lingering_invalid_episodes episodes, at once
        for later ones.
        """
        try:
            schema = TestPanel(self.page, SessionMode.DataEditor).get_schema()
        except Exception:
            return
        violations = schema_meta_violations(self.page, schema)
        if violations is None:
            return
        result.kwargs["schema_valid_after"] = not violations

        if not violations:
            if self._schema_valid is False:
                show("   ✓ Schema is valid again")
            self._schema_valid = True
            self._steps_in_invalid = 0
            self._recovery_disabled = False   # a failed recovery only pauses forced recovery until the schema is valid again
            return

        if self._schema_valid is not False:
            self._invalid_episodes += 1
            self._steps_in_invalid = 0
            result.kwargs["schema_became_invalid"] = True
            result.kwargs["schema_meta_violations"] = violations[:3]
            show(f"   Schema became invalid (episode {self._invalid_episodes}): "
                  f"{violations[0]['deep_path']}: {violations[0]['message']}")
        else:
            self._steps_in_invalid += 1
        self._schema_valid = False

        if self._recovery_disabled:
            return
        if self._invalid_episodes > self._limits.max_lingering_invalid_episodes:
            reason = "invalid-schema budget used up"
        elif self._steps_in_invalid >= self._limits.max_steps_in_invalid_schema:
            reason = f"schema invalid for {self._steps_in_invalid + 1} steps"
        else:
            return
        if self._recover_invalid_schema(step_num, reason, result):
            self._schema_valid = True
            self._steps_in_invalid = 0

    def _check_and_repair_persisting_failures(
        self, report: InvariantReport | None, last_action_testid: str,
        action_succeeded: bool, step_num: int,
    ) -> InvariantReport | None:
        """Repair failures that appear in two consecutive steps' merged (single-snapshot + temporal) reports.

        A failure's first appearance is left untouched; only its second consecutive appearance triggers repair.
        clear-text actions never reach this method: the loop undoes them right away.

        A failure that first appeared on a mode-switch step is never repaired (see _failures_from_mode_switch).

        The repair never switches mode: undo/redo act on the current mode's stack only, so a failure whose cause is an
        edit in the other mode is not repaired (the walk-back finds nothing to undo and gives up).

        The repair is recorded on the step that triggered it (the last entry of self.history) and updates
        self._prev_snapshot, self._prev_failure_names and self._repair_already_attempted. Replay reruns it from that
        record. Known gap: the zero-click case runs one extra check that is not recorded.
        Returns the report to continue with: the one passed in if nothing was repaired, else the final post-repair
        report.
        """
        current_failure_names = {r.name for r in report.failures} if report else set()
        # A known bug marked "do_not_repair" (triage_config.json) never starts a repair.
        skipped = self._known_bugs.no_repair_ids(current_failure_names)
        if skipped - self._no_repair_noted:
            show(f"   Not repairing known bug failure(s) marked do_not_repair: {', '.join(sorted(skipped))}")
            self._no_repair_noted |= skipped
        current_failure_names = {n for n in current_failure_names if invariant_id(n) not in skipped}
        # A failure that first appeared on a mode-switch step was not caused by an action in the mode we are in now
        # (the switch only changed what is shown), so undoing this mode's edits cannot fix it. Never repair it.
        if (last_action_testid or "").startswith("mode-active-"):
            self._failures_from_mode_switch |= current_failure_names - self._prev_failure_names
        persisting = ((current_failure_names & self._prev_failure_names)
                      - self._repair_already_attempted - self._failures_from_mode_switch)
        persisting = {n for n in persisting if invariant_id(n) not in NO_REPAIR_INVARIANTS}

        engine = self._repair_engine()
        too_far = engine.walk_back_too_far(persisting) if persisting and action_succeeded else None
        if too_far:
            show(too_far)
            self._repair_already_attempted |= persisting

        if persisting and action_succeeded and not too_far:
            info = engine.repair(persisting, last_action_testid)
            clicks = info["steps"]
            self._note_clicks(clicks)
            extra = {} if info["succeeded"] else {"still_failing": info["still_failing"]}
            block = repair_block(KIND_FAILURE, clicks, "succeeded" if info["succeeded"] else "gave_up",
                                 persisting_failures=sorted(persisting), **extra)
            if clicks:
                # The next step's temporal check and persistence comparison use the final post-repair
                # (or post-revert) state as baseline; the pre-repair state stays on the recorded StepResult.
                report = clicks[-1]["report"]
                last_testid = clicks[-1]["testid"]
            else:
                # Zero-click match: no click to record, but report and self._prev_snapshot must reflect the
                # current state, or the same "already matches" check would repeat every step.
                report = self.checker.run_all()
                last_testid = last_action_testid
            self._record_repair(self.history[-1], block, last_testid, step_num)

            if not info["succeeded"]:
                self._repair_already_attempted |= set(info["still_failing"])

        # Forget failures that cleared, so a later occurrence gets a new repair attempt
        final_failure_names = {r.name for r in report.failures} if report else set()
        self._repair_already_attempted &= final_failure_names
        self._failures_from_mode_switch &= final_failure_names
        self._prev_failure_names = final_failure_names
        self._prev_checked_names = ({r.name for r in report.results if not getattr(r, "skipped", False)}
                                    if report else set())
        return report