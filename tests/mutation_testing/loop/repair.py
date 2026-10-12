"""Undo-based repair of failures that persist over consecutive steps."""
from __future__ import annotations

from tests.shared.python.utils import SessionMode
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.discovery.page_capture import _get_current_active_mode, js_click_for_testid
from tests.invariants.invariants_core import InvariantReport
from tests.invariants.schema_validity import schema_meta_violations
from tests.mutation_testing.runs.step_result import StepResult
from tests.mutation_testing.loop.action_weights import _MODE_SWITCH_TESTID
from tests.mutation_testing.loop.app_setup import _safe_filename_part
from tests.mutation_testing.loop.console import show


class RepairMixin:
    """Repair of persisting failures by undo."""

    MAX_UNDO_ATTEMPTS = 15   # undos of one walk-back; the known-good state must be reachable within this many edits

    @staticmethod
    def _clean_for(report: InvariantReport, persisting_names: set[str]) -> bool:
        """True if `report` is evidence that none of `persisting_names` was broken.

        Single-snapshot invariants (INV-*) must have been evaluated and passed in that report. A step in
        the other mode, where the invariant does not run, proves nothing (INV-D6 is not evaluated in
        Schema mode). Temporal and inferred invariants only appear when they apply, so for them it is
        enough that they are absent from the failure list.
        """
        if {r.name for r in report.failures} & persisting_names:
            return False
        evaluated_ok = {r.name for r in report.results
                        if r.passed and not getattr(r, "skipped", False)}
        return all(n in evaluated_ok for n in persisting_names if n.startswith("INV-"))

    def _find_last_known_good_snapshot(self, persisting_names: set[str]) -> tuple[dict | None, int | None]:
        """Most recent recorded step whose report has none of the persisting failures, as (snapshot, step_num).

        (None, None) if there is none, meaning there is nothing safe to walk back to.
        """
        for step_result in reversed(self.history):
            if step_result.report is None or step_result.snapshot is None:
                continue
            if self._clean_for(step_result.report, persisting_names):
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
        """Undo one click at a time until the live state equals the last known-good snapshot.

        The state is re-read after each click, so no fixed undo count is assumed (one step can take several undo
        entries). MAX_UNDO_ATTEMPTS only guards against a runaway loop; hitting it is not success.

        No click is made when the known-good state is further back than MAX_UNDO_ATTEMPTS edits in the current mode
        (e.g. INV-D6 is only evaluated in Data mode, so after a long stretch of Schema edits the last state where it
        passed is the one before all of them). Clicking stops after UNCHANGED_UNDO_LIMIT undos in a row that
        change nothing (empty undo stack in that mode), and only the undos that changed something are redone.

        Matching the snapshot is necessary but not sufficient: it covers only de_data, de_schema, se_schema and se_data,
        not the raw Text View content, which some failures depend on. Success is therefore confirmed by a fresh
        invariant check of the persisting failures. Without a known-good snapshot this falls back to
        _repair_policy_single_undo_or_revert.

        Returns {"succeeded": bool, "steps": [...]} with one real click per entry. "steps" may be empty if no click
        was needed.
        """
        target_snapshot, target_step = self._find_last_known_good_snapshot(persisting_names)
        if target_snapshot is None:
            show(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
            show("      No known-good state on record to walk back to — "
                  "falling back to the single-undo policy")
            return self._repair_policy_single_undo_or_revert(persisting_names, last_action_testid)

        current_mode = _get_current_active_mode(self.page)  # after a mode switch the caller has already switched back to the prior mode
        mode_undo = "schema_undo" if current_mode == "schemaEditor" else "undo"
        mode_redo = "schema_redo" if current_mode == "schemaEditor" else "redo"
        MAX_UNDO_ATTEMPTS = self.MAX_UNDO_ATTEMPTS
        UNCHANGED_UNDO_LIMIT = 2

        show(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
        edits_back = self._edits_since(target_step, current_mode)
        if edits_back > MAX_UNDO_ATTEMPTS:
            show(f"      The last state where it passed is step {target_step}, {edits_back} {current_mode} edits back "
                 f"(more than {MAX_UNDO_ATTEMPTS} undos) - not walking back, giving up on auto-repair for this failure")
            return {"succeeded": False, "still_failing": sorted(persisting_names), "steps": []}
        show(f"      Walking back toward the recorded state from step {target_step}")

        def _cleared(report: InvariantReport) -> bool:
            return not ({r.name for r in report.failures} & persisting_names)

        target_fields = {k: target_snapshot[k] for k in ("de_data", "de_schema", "se_schema", "se_data")}
        steps = []
        clicks_done = 0
        effective_clicks = 0      # undos that changed the state; only these are redone when giving up
        unchanged_in_a_row = 0
        resolved = False

        # Check before clicking: some failures never change the state (e.g. clear-text leaves an unparseable
        # document), so an undo would overshoot and revert an unrelated, valid action.
        if self._read_full_snapshot() == target_fields:
            fresh = self.checker.run_all()
            if _cleared(fresh):
                resolved = True
                show(f"      ✓ Live state already matches step {target_step}'s recorded state, "
                      f"and the failure is confirmed resolved (0 undos needed)")
            else:
                show(f"      Live state matches step {target_step}'s recorded snapshot, but "
                      f"{sorted(persisting_names)} still fails there too — undo/redo can't fix "
                      f"this (it depends on something the snapshot doesn't track, e.g. raw Text "
                      f"View editor content) — giving up without clicking anything")
        else:
            state_before_click = self._read_full_snapshot()
            for attempt in range(1, MAX_UNDO_ATTEMPTS + 1):
                self._click_testid_direct(mode_undo)
                self._recent_action_testids.append(mode_undo)
                clicks_done += 1
                report_now = self.checker.run_all()
                steps.append({"testid": mode_undo, "report": report_now})

                state_now = self._read_full_snapshot()
                if state_now == state_before_click:
                    unchanged_in_a_row += 1
                else:
                    unchanged_in_a_row = 0
                    effective_clicks += 1
                state_before_click = state_now

                if state_now == target_fields:
                    if _cleared(report_now):
                        resolved = True
                        show(f"      ✓ Reached step {target_step}'s recorded state after "
                              f"{attempt} undo(s), and the failure is confirmed resolved")
                    else:
                        show(f"      Reached step {target_step}'s recorded state after "
                              f"{attempt} undo(s), but {sorted(persisting_names)} still fails "
                              f"there too — undo/redo can't fix this one")
                    break

                if unchanged_in_a_row >= UNCHANGED_UNDO_LIMIT:
                    show(f"      {mode_undo} changed nothing {unchanged_in_a_row} times in a row (nothing left to undo in "
                         f"this mode) - stopping the walk-back")
                    break

            self._recent_action_testids = self._recent_action_testids[-2:]

        if resolved:
            return {"succeeded": True, "cleared": sorted(persisting_names), "steps": steps}

        show(f"      ✗ Reverting {effective_clicks} undo(s) and giving up on auto-repair for this failure")
        for _ in range(effective_clicks):
            self._click_testid_direct(mode_redo)
            self._recent_action_testids.append(mode_redo)
            report_now = self.checker.run_all()
            steps.append({"testid": mode_redo, "report": report_now})
        self._recent_action_testids = self._recent_action_testids[-2:]

        return {"succeeded": False, "still_failing": sorted(persisting_names), "steps": steps}

    def _repair_policy_single_undo_or_revert(self, persisting_names: set[str], last_action_testid: str) -> dict:
        """Click undo once (or the opposite, if the last action was itself undo/redo).

        If every persisting failure clears, keep it. Otherwise click the opposite to restore the previous state and
        stop repairing that failure while it persists.

        Returns {"succeeded": bool, "steps": [...]}. Each entry is one real click with its testid and report, so
        replay can reproduce it; a bundled record has no single testid and would be skipped.
        """
        current_mode = _get_current_active_mode(self.page)  # after a mode switch the caller has already switched back to the prior mode
        mode_undo = "schema_undo" if current_mode == "schemaEditor" else "undo"
        mode_redo = "schema_redo" if current_mode == "schemaEditor" else "redo"

        if last_action_testid == mode_undo:
            repair_click, revert_click = mode_redo, mode_undo
        elif last_action_testid == mode_redo:
            repair_click, revert_click = mode_undo, mode_redo
        else:
            repair_click, revert_click = mode_undo, mode_redo

        show(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
        show(f"      Attempting repair: {repair_click}")
        self._click_testid_direct(repair_click)
        self._recent_action_testids.append(repair_click)

        post_repair_report = self.checker.run_all()
        still_failing = {r.name for r in post_repair_report.failures} & persisting_names

        if not still_failing:
            show("      ✓ Repair succeeded — persisting failure(s) cleared")
            return {
                "succeeded": True,
                "cleared": sorted(persisting_names),
                "steps": [{"testid": repair_click, "report": post_repair_report}],
            }

        show(f"      ✗ Repair did not clear {sorted(still_failing)} — reverting "
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
        """Click a fixed control (undo, redo, schema_undo, schema_redo, mode-active-false) without re-discovering actions."""
        js = js_click_for_testid(testid)
        if js:
            self.page.evaluate(js)
        else:
            self.page.get_by_test_id(testid).first.click()
        self.page.wait_for_timeout(400)

    def _click_mode_switch_for_repair(self) -> InvariantReport:
        """One click of the Data/Schema toggle followed by a fresh invariant check, like every other repair click."""
        self._click_testid_direct(_MODE_SWITCH_TESTID)
        self._recent_action_testids.append(_MODE_SWITCH_TESTID)
        self._recent_action_testids = self._recent_action_testids[-2:]
        return self.checker.run_all()

    def _log_mode_switch_step(self, report: InvariantReport, step_num: int, role: str, desc: str) -> None:
        """Log one mode-switch click around a repair as its own replayable step.

        Kept apart from the repair loop so the loop's succeeded/attempt/revert labels cover the undo/redo clicks only.
        Updates self._prev_snapshot, which then reflects the state the run continues from.
        """
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

    def _choose_repair_mode(self, persisting: set[str], last_action_testid: str,
                            current_mode: str | None) -> str | None:
        """Which mode's undo stack the repair should use.

        Compares the live state with the last state where the failing invariant passed: the mode whose state differs
        holds the culprit edit. If both differ, the mode edited most recently since then wins. Falls back to the mode
        before a mode switch, then to the current mode.
        """
        target, target_step = self._find_last_known_good_snapshot(persisting)
        if target is not None:
            try:
                live = self._read_full_snapshot()
                candidates = []
                # de_schema is the schema being edited (the same as se_data). se_schema is the meta-schema the Schema
                # Editor is governed by, which never changes, so it can never point at the Schema Editor.
                if live["de_schema"] != target["de_schema"]:
                    candidates.append("schemaEditor")
                if live["de_data"] != target["de_data"]:
                    candidates.append("dataEditor")
                if len(candidates) == 1:
                    return candidates[0]
                if len(candidates) > 1:
                    mode = self._most_recent_edit_mode(target_step, candidates)
                    if mode:
                        return mode
            except Exception as e:
                print(f"      could not compare live state with the known-good snapshot: {e}")
        if last_action_testid == _MODE_SWITCH_TESTID:
            return "dataEditor" if current_mode == "schemaEditor" else "schemaEditor"
        return current_mode

    def _walk_back_too_far(self, persisting: set[str], last_action_testid: str) -> str | None:
        """A message if the last known-good state is further back than a walk-back can reach, else None.

        Checked before any click, mode switch included, so a hopeless repair costs nothing.
        """
        target, target_step = self._find_last_known_good_snapshot(persisting)
        if target is None:
            return None
        mode = self._choose_repair_mode(persisting, last_action_testid, _get_current_active_mode(self.page))
        edits_back = self._edits_since(target_step, mode)
        if edits_back <= self.MAX_UNDO_ATTEMPTS:
            return None
        return (f"\n   Persisting failure(s) detected: {sorted(persisting)}\n"
                f"      The last state where they passed is step {target_step}, {edits_back} {mode} edits back "
                f"(more than {self.MAX_UNDO_ATTEMPTS} undos) - not walking back, giving up on auto-repair for this failure")

    def _edits_since(self, target_step: int | None, mode: str | None) -> int:
        """Number of successful edits made in `mode` after step `target_step` (what undo would have to take back).

        Expand/collapse, format changes and mode switches do not create undo entries and are not counted.
        """
        if target_step is None:
            return 0
        not_edits = ("expand-", "collapse-", "format-selector", _MODE_SWITCH_TESTID)
        count = 0
        for sr in self.history:
            snap = sr.snapshot
            if snap is not None and (snap.get("step") or 0) <= target_step:
                continue
            if not sr.action_id.startswith("dynamic:") or not sr.action_succeeded:
                continue
            testid = sr.kwargs.get("testid", "")
            if testid.startswith(not_edits) or testid in ("undo", "redo", "schema_undo", "schema_redo"):
                continue
            if sr.kwargs.get("el_mode") == mode:
                count += 1
        return count

    def _most_recent_edit_mode(self, target_step: int | None, candidates: list[str]) -> str | None:
        """Mode of the latest successful action after `target_step` that ran in one of `candidates`."""
        for sr in reversed(self.history):
            snap = sr.snapshot
            if snap is not None and target_step is not None and (snap.get("step") or 0) <= target_step:
                break
            if not sr.action_id.startswith("dynamic:") or not sr.action_succeeded:
                continue
            if sr.kwargs.get("testid") == _MODE_SWITCH_TESTID:
                continue
            m = sr.kwargs.get("el_mode")
            if m in candidates:
                return m
        return None

    def _log_recovery_click(self, testid: str, report: InvariantReport, step_num: int, idx: int,
                            role: str, ok: bool, reason: str) -> None:
        """One recovery click as an ordinary, replayable step (same shape as every repair click)."""
        screenshot_path = self._capture_screenshot(
            filename=f"step_{step_num:02d}_recover{idx}_{_safe_filename_part(testid)}.png",
            invariants_failed=bool(report and not report.passed),
        )
        sub = StepResult(
            action_id=f"repair:{testid}",
            description=f"invalid-schema recovery ({reason})",
            kwargs={"testid": testid, "value": "", "persisting_failures": [],
                    "succeeded": ok, "role": role, "recovery_reason": reason},
            action_succeeded=True,
            action_error=None,
            report=report,
            screenshot_path=screenshot_path,
        )
        sub.snapshot = self._build_snapshot(testid, step_num)
        self._append_snapshot(sub.snapshot)
        self.history.append(sub)
        self._prev_snapshot = sub.snapshot
        self._prev_failure_names = {r.name for r in report.failures}
        self._prev_checked_names = {r.name for r in report.results if not getattr(r, "skipped", False)}

    def _recover_invalid_schema(self, step_num: int, reason: str) -> bool:
        """Undo in the Schema Editor until the schema is valid against the meta-schema again.

        Switches to Schema mode first if needed and back afterwards (both logged as replayable steps).
        If MAX_RECOVERY_UNDOS undos do not get there, the undos are redone and forced recovery is
        switched off until the schema is valid again (so a stuck case cannot loop). Returns True if recovered.
        """
        start_mode = _get_current_active_mode(self.page)
        show(f"\n   Invalid schema ({reason}) - undoing in the Schema Editor until it is valid again")
        if start_mode != "schemaEditor":
            rep = self._click_mode_switch_for_repair()
            self._log_mode_switch_step(rep, step_num, "mode_switch_back",
                                       "switching to Schema mode to recover from an invalid schema")

        tp = TestPanel(self.page, SessionMode.DataEditor)
        recovered = False
        clicks = 0
        for attempt in range(1, self.MAX_RECOVERY_UNDOS + 1):
            self._click_testid_direct("schema_undo")
            clicks += 1
            self._recent_action_testids = (self._recent_action_testids + ["schema_undo"])[-2:]
            rep = self.checker.run_all()
            violations = schema_meta_violations(self.page, tp.get_schema())
            recovered = violations == []
            self._log_recovery_click("schema_undo", rep, step_num, attempt,
                                     "invalid_schema_recovery", recovered, reason)
            if recovered:
                show(f"      ✓ Schema valid again after {attempt} undo(s)")
                break

        if not recovered:
            show(f"      ✗ Still invalid after {clicks} undo(s) - reverting and not forcing recovery again until the schema is valid")
            for k in range(clicks):
                self._click_testid_direct("schema_redo")
                rep = self.checker.run_all()
                self._log_recovery_click("schema_redo", rep, step_num, clicks + k + 1,
                                         "invalid_schema_recovery_revert", False, reason)
            self._recovery_disabled = True

        if start_mode != "schemaEditor":
            rep = self._click_mode_switch_for_repair()
            self._log_mode_switch_step(rep, step_num, "mode_switch_forward",
                                       "switching forward again after schema recovery")
        return recovered

    def _update_schema_validity_and_maybe_recover(self, step_num: int, result: "StepResult") -> None:
        """Track valid <-> invalid transitions of the edited schema and enforce the invalid-schema budget.

        Users do produce invalid schemas, so they stay in the mutation space - but a single invalid edit
        otherwise contaminates the rest of the run. Each invalid episode is observed (INV-S4 and the
        skipped INV-D6 evidence run in the step that caused it), then undone: after
        MAX_STEPS_IN_INVALID_SCHEMA steps for the first MAX_LINGERING_INVALID_EPISODES episodes, at once
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
        if self._invalid_episodes > self.MAX_LINGERING_INVALID_EPISODES:
            reason = "invalid-schema budget used up"
        elif self._steps_in_invalid >= self.MAX_STEPS_IN_INVALID_SCHEMA:
            reason = f"schema invalid for {self._steps_in_invalid + 1} steps"
        else:
            return
        if self._recover_invalid_schema(step_num, reason):
            self._schema_valid = True
            self._steps_in_invalid = 0

    def _attempt_repair(self, persisting_names: set[str], last_action_testid: str) -> dict:
        """Repair the persisting failures with the walk-back policy (mode-switch handling is in the caller)."""
        return self._repair_policy_walk_back_to_known_good(persisting_names, last_action_testid)

    def _check_and_repair_persisting_failures(
        self, report: InvariantReport | None, last_action_testid: str,
        action_succeeded: bool, step_num: int,
    ) -> InvariantReport | None:
        """Repair failures that appear in two consecutive steps' merged (single-snapshot + temporal) reports.

        A failure's first appearance is left untouched; only its second consecutive appearance triggers repair.
        clear-text actions never reach this method: the loop undoes them right away.

        If the last action was a mode switch, the responsible edit is in the previous mode and undo/redo act on the
        current mode's stack only. The repair therefore switches back first and forward again afterwards; both
        clicks are logged as separate replayable steps.

        Known gaps:
        - only one preceding mode switch is handled;
        - the zero-click branches run extra checker.run_all() calls that are not recorded, so replay cannot mirror
          them (no divergence observed so far).

        Appends one StepResult per repair click to self.history and updates self._prev_snapshot,
        self._prev_failure_names and self._repair_already_attempted.
        Returns the report to continue with: the one passed in if nothing was repaired, else the final post-repair
        report.
        """
        current_failure_names = {r.name for r in report.failures} if report else set()
        persisting = (current_failure_names & self._prev_failure_names) - self._repair_already_attempted
        # First-appearance rule: right after a mode switch, an INV-* failure whose invariant was NOT
        # evaluated on the previous step (it only runs in this mode) is first observable now, so its
        # cause lies in the other mode's edits. Waiting one more step would let the switch fall out
        # of last_action_testid and the repair would target the wrong undo stack.
        if last_action_testid == _MODE_SWITCH_TESTID:
            persisting |= {n for n in current_failure_names
                           if n.startswith("INV-") and n not in self._prev_checked_names
                           } - self._repair_already_attempted

        too_far = self._walk_back_too_far(persisting, last_action_testid) if persisting and action_succeeded else None
        if too_far:
            show(too_far)
            self._repair_already_attempted |= persisting

        if persisting and action_succeeded and not too_far:
            mode_before_repair = _get_current_active_mode(self.page)
            repair_mode = self._choose_repair_mode(persisting, last_action_testid, mode_before_repair)
            switched_for_repair = repair_mode is not None and repair_mode != mode_before_repair
            if switched_for_repair:
                show("\n    Persisting failure follows a mode switch — "
                      "switching back to the prior mode before repair "
                      "(undo/redo only touch the currently active mode's stack)")
                switch_back_report = self._click_mode_switch_for_repair()
                self._log_mode_switch_step(
                    switch_back_report, step_num, "mode_switch_back",
                    "switching back to the prior mode before repair",
                )

            repair_info = self._attempt_repair(persisting, last_action_testid)
            repair_steps = repair_info["steps"]

            for step_idx, sub_step in enumerate(repair_steps):
                is_last = step_idx == len(repair_steps) - 1
                if repair_info["succeeded"]:
                    role = "succeeded"
                    desc = f"auto-repair cleared persisting failure(s) {sorted(persisting)}"
                elif is_last:
                    role = "revert"
                    desc = (f"auto-repair did not clear {repair_info['still_failing']} "
                            f"— reverted, giving up on further repair for this failure")
                else:
                    role = "attempt"
                    desc = f"attempting auto-repair for persisting failure(s) {sorted(persisting)}"

                sub_report = sub_step["report"]
                # One screenshot per repair click; step_idx keeps names unique within a step
                sub_screenshot_path = self._capture_screenshot(
                    filename=(f"step_{step_num:02d}_repair{step_idx + 1}_"
                              f"{_safe_filename_part(sub_step['testid'])}.png"),
                    invariants_failed=bool(sub_report and not sub_report.passed),
                )

                sub_result = StepResult(
                    action_id=f"repair:{sub_step['testid']}",
                    description=desc,
                    # A real testid makes the click replayable. "role" tells the repair attempt from its revert
                    # click, because the log renders from kwargs, not from the description.
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
                    # The next step's temporal check and persistence comparison use the final post-repair
                    # (or post-revert) state as baseline; the pre-repair state stays on the recorded StepResult.
                    self._prev_snapshot = sub_result.snapshot
                    report = sub_step["report"]

            if not repair_steps:
                # Zero-click match: no click to record, but report and self._prev_snapshot must reflect the
                # current state, or the same "already matches" check would repeat every step.
                report = self.checker.run_all()
                self._prev_snapshot = self._build_snapshot(last_action_testid, step_num)
                self._append_snapshot(self._prev_snapshot)

            if not repair_info["succeeded"]:
                self._repair_already_attempted |= set(repair_info["still_failing"])

            if switched_for_repair:
                show("    Switching forward again to restore the mode "
                      "the triggering step left us in")
                switch_forward_report = self._click_mode_switch_for_repair()
                self._log_mode_switch_step(
                    switch_forward_report, step_num, "mode_switch_forward",
                    "switching forward again after repair",
                )
                # The run continues from the state AFTER the forward switch, so that report (not the
                # last repair click's) is the baseline. It is also the first place a failure that is
                # only evaluated in the original mode (INV-D6 in Data mode) can be confirmed cleared.
                report = switch_forward_report
                still_failing_after_switch = {r.name for r in report.failures} & persisting
                if still_failing_after_switch:
                    self._repair_already_attempted |= still_failing_after_switch
                    show(f"      ✗ {sorted(still_failing_after_switch)} still fails after switching "
                          f"forward - repair did not fix it, giving up on this failure")

        # Forget failures that cleared, so a later occurrence gets a new repair attempt
        final_failure_names = {r.name for r in report.failures} if report else set()
        self._repair_already_attempted &= final_failure_names
        self._prev_failure_names = final_failure_names
        self._prev_checked_names = ({r.name for r in report.results if not getattr(r, "skipped", False)}
                                    if report else set())

        return report