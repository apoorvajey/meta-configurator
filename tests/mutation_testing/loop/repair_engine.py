"""Undo-based repair, shared by the mutation loop and replay.

The loop decides WHEN to repair (a failure persisting over two steps, a clear-text action, an invalid schema). The
repair itself is a state-driven procedure: it undoes until the live state equals the last known-good snapshot (or the
schema is valid again) and redoes what it undid if that did not help. Replay runs the same procedure against the failure
it sees on replay, so the clicks are never replayed from a recording: how many undos are needed depends on the state.

A repair is recorded on the step that triggered it (StepResult.kwargs["repairs"], see repair_block), not as steps of
its own: no snapshot, no screenshot, no step number.

Dependencies are passed in (page, a callable that runs the invariant checks, the limits and the list of finished
steps), so nothing here depends on the loop's state.
"""
from __future__ import annotations

from typing import Callable

from tests.shared.python.utils import SessionMode
from tests.shared.python.test_panel import TestPanel
from tests.shared.python.utils_code_editor import read_text_editor_raw
from tests.mutation_testing.discovery.element import ElementDescriptor
from tests.mutation_testing.discovery.page_capture import _get_current_active_mode, js_click_for_testid
from tests.invariants.invariants_core import InvariantReport, _diff_nested
from tests.invariants.schema_validity import schema_meta_violations
from tests.mutation_testing.loop.action_weights import _MODE_SWITCH_TESTID
from tests.mutation_testing.loop.console import show
from tests.mutation_testing.loop.app_setup import (
    document_diffs, effect_console_lines, effect_records, read_documents, undo_redo_label,
)


UNDO_REDO_TESTIDS = ("undo", "redo", "schema_undo", "schema_redo")

KIND_FAILURE = "failure"            # persisting failure, walked back to the last known-good state
KIND_CLEAR_TEXT = "clear_text"      # the unconditional undo after a clear-text action
KIND_INVALID_SCHEMA = "invalid_schema"   # undo in the Schema Editor until the schema is valid again


REVERSE_TESTID = "text-reverse"            # the pseudo-click of a reverse-action repair (restoring the earlier text)
REVERSE_REVERT_TESTID = "text-reverse-revert"
INVERSE_KEY = "_inverse"                   # StepResult.kwargs key holding a step's inverse; kept in memory, not exported
_TEXT_ACTION_PREFIXES = ElementDescriptor._TEXT_PREFIXES + ("clear-text-",)


def capture_inverse(page, testid: str) -> dict | None:
    """The inverse of a Text View action, to be taken BEFORE the action runs: the document text as it is now.

    Every Text View edit is a change of the raw text of one editor, so restoring that text reverses it, whatever the
    edit was. Returns {"kind": "text_replace", "mode", "text"}, or None for actions that are not Text View edits (GUI
    actions have no inverse yet; repair then falls back to undo) or when the text cannot be read.
    """
    for prefix in _TEXT_ACTION_PREFIXES:
        if testid.startswith(prefix):
            mode = testid[len(prefix):].partition(":")[0]
            text = read_text_editor_raw(page, mode)
            return {"kind": "text_replace", "mode": mode, "text": text} if text is not None else None
    return None


def replace_editor_text(page, mode: str, text: str) -> bool:
    """Replace the whole Text View document of `mode` with `text` through Ace, as one undo entry (like a user pasting it
    over a select-all). False if the editor is not on the page. setValue() is avoided: it resets Ace's undo manager."""
    import json
    ok = page.evaluate(f"""
        () => {{
            const editorEl = [...document.querySelectorAll('[id^="code-editor-{mode}"]')][0];
            if (!editorEl) return false;
            const editor = window.ace?.edit(editorEl.id);
            if (!editor) return false;
            const doc = editor.session.doc;
            editor.session.replace({{start: doc.indexToPosition(0, 0), end: doc.indexToPosition(editor.getValue().length, 0)}},
                                   {json.dumps(text)});
            return true;
        }}
    """)
    if ok:
        page.wait_for_timeout(400)
    return bool(ok)


def undo_redo_for_mode(mode: str | None) -> tuple[str, str]:
    """(undo testid, redo testid) of the editor `mode` ("schemaEditor" or "dataEditor")."""
    return ("schema_undo", "schema_redo") if mode == "schemaEditor" else ("undo", "redo")


class RepairEngine:
    """The repair procedures. Each returns {"steps": [{"testid", "report"}, ...], ...}: one entry per real click, with the
    invariant report taken after it. `say` prints progress (the loop's `show`, or a quieter function in replay)."""

    def __init__(self, page, run_checks: Callable[[], InvariantReport], limits, history: list,
                 say: Callable[[str], None] = show):
        self.page = page
        self.run_checks = run_checks
        self.limits = limits
        self.history = history     # StepResult-like entries: action_id "dynamic:...", kwargs testid/el_mode, report, snapshot
        self.say = say

    # -- helpers ----------------------------------------------------------------------------------------------

    def click(self, testid: str) -> None:
        """Click a fixed control (undo, redo, schema_undo, schema_redo, mode-active-false) without re-discovering actions."""
        js = js_click_for_testid(testid)
        if js:
            self.page.evaluate(js)
        else:
            self.page.get_by_test_id(testid).first.click()

    def _document(self, mode: str):
        """Both edited documents ({"data", "schema"}, see read_documents), whichever editor is active. `mode` is the editor
        the repair works in; it only decides which document is listed first in the effect."""
        return read_documents(self.page)

    def _effect(self, mode: str, before, label: str) -> list[dict] | None:
        """Print and return what changed in the data and the schema since `before` (see effect_console_lines).

        The result is a list of {"document", "path", "before", "after"} (first 5 changes per document; "before" is what
        was taken away, "after" what came back) for the repair block; [] if nothing changed; None if nothing could be read.
        """
        diffs = document_diffs(before, self._document(mode), "schema" if mode == "schemaEditor" else "data")
        if diffs is None:
            return None
        for line in effect_console_lines(diffs, limit=5, label=label):
            self.say("   " + line)
        return effect_records(diffs, 5)

    def click_with_effect(self, testid: str) -> list[dict] | None:
        """click(testid) for an undo or redo, plus what it reverted or re-applied (see _effect)."""
        mode = "schemaEditor" if testid.startswith("schema_") else "dataEditor"
        before = self._document(mode)
        self.click(testid)
        return self._effect(mode, before, undo_redo_label(testid))

    @staticmethod
    def clean_for(report: InvariantReport, persisting_names: set[str]) -> bool:
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

    def find_last_known_good_snapshot(self, persisting_names: set[str]) -> tuple[dict | None, int | None]:
        """Most recent recorded step whose report has none of the persisting failures, as (snapshot, step_num).

        (None, None) if there is none, meaning there is nothing safe to walk back to.
        """
        for step_result in reversed(self.history):
            if step_result.report is None or step_result.snapshot is None:
                continue
            if self.clean_for(step_result.report, persisting_names):
                return step_result.snapshot, step_result.snapshot.get("step")
        return None, None

    def read_full_snapshot(self) -> dict:
        _tp_de = TestPanel(self.page, SessionMode.DataEditor)
        _tp_se = TestPanel(self.page, SessionMode.SchemaEditor)
        return {
            "de_data": _tp_de.get_data(),
            "de_schema": _tp_de.get_schema(),
            "se_schema": _tp_se.get_schema(),
            "se_data": _tp_se.get_data(),
        }

    def edits_since(self, target_step: int | None, mode: str | None) -> int:
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
            if testid.startswith(not_edits) or testid in UNDO_REDO_TESTIDS:
                continue
            if sr.kwargs.get("el_mode") == mode:
                count += 1
        return count

    def walk_back_too_far(self, persisting: set[str]) -> str | None:
        """A message if the last known-good state is further back than a walk-back can reach, else None.

        Checked before any click, so a hopeless repair costs nothing.
        """
        target, target_step = self.find_last_known_good_snapshot(persisting)
        if target is None or self.reverse_plan(persisting) is not None:
            return None   # a reverse-action repair does not depend on how many edits back the state was good
        mode = _get_current_active_mode(self.page)
        edits_back = self.edits_since(target_step, mode)
        if edits_back <= self.limits.max_undo_attempts:
            return None
        return (f"\n   Persisting failure(s) detected: {sorted(persisting)}\n"
                f"      The last state where they passed is step {target_step}, {edits_back} {mode} edits back "
                f"(more than {self.limits.max_undo_attempts} undos) - not walking back, giving up on auto-repair for this failure")

    # -- failure repair -----------------------------------------------------------------------------------------

    def repair(self, persisting_names: set[str], last_action_testid: str) -> dict:
        """Repair the persisting failures in the mode the run is in (never switches mode).

        First by reversing the action (reverse_text_edit) when the edit that led away from the last known-good state
        was a Text View edit; otherwise, or if that is not possible, by the undo walk-back."""
        plan = self.reverse_plan(persisting_names)
        if plan is not None:
            done = self.reverse_text_edit(persisting_names, plan)
            if done is not None:
                return done
        return self.walk_back_to_known_good(persisting_names, last_action_testid)

    def reverse_plan(self, persisting_names: set[str]) -> dict | None:
        """The inverse to apply, or None if the repair has to use undo.

        The oldest edit made in this mode after the last known-good state is the one the state has to be taken back
        past. If that is a Text View edit, its inverse (the text from before it) leads straight back to the known-good
        state, however many edits came after it. If it is any other kind of edit, or there is no known-good state or no
        edit since, there is nothing to reverse.
        """
        target, target_step = self.find_last_known_good_snapshot(persisting_names)
        if target is None:
            return None
        mode = _get_current_active_mode(self.page)
        not_edits = ("expand-", "collapse-", "format-selector", _MODE_SWITCH_TESTID)
        for sr in self.history:
            snap = sr.snapshot
            if snap is not None and (snap.get("step") or 0) <= target_step:
                continue
            if not sr.action_id.startswith("dynamic:") or not sr.action_succeeded:
                continue
            testid = sr.kwargs.get("testid", "")
            if testid.startswith(not_edits) or testid in UNDO_REDO_TESTIDS:
                continue
            if sr.kwargs.get("el_mode") != mode:
                continue
            inverse = sr.kwargs.get(INVERSE_KEY)
            if inverse and inverse.get("kind") == "text_replace" and inverse.get("mode") == mode:
                return inverse
            return None
        return None

    def reverse_text_edit(self, persisting_names: set[str], inverse: dict) -> dict | None:
        """Restore the Text View text from before the culprit edit; keep it if the failure clears, else put the text back.

        Returns {"succeeded", "steps", ...} like the other repairs, or None without changing anything if the editor is
        not available (the caller then falls back to undo).
        """
        say = self.say
        mode = inverse["mode"]
        before = read_text_editor_raw(self.page, mode)
        if before is None:
            return None
        say(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
        say("      Reversing the edit: restoring the Text View text from before it")
        doc_before = self._document(mode)
        if not replace_editor_text(self.page, mode, inverse["text"]):
            return None
        effect = self._effect(mode, doc_before, "edit reversed")
        report = self.run_checks()
        still_failing = {r.name for r in report.failures} & persisting_names
        if not still_failing:
            say("      ✓ Repair succeeded — persisting failure(s) cleared")
            return {"succeeded": True, "cleared": sorted(persisting_names),
                    "steps": [{"testid": REVERSE_TESTID, "report": report, "effect": effect}]}
        say(f"      ✗ Reversing did not clear {sorted(still_failing)} — putting the text back and giving up on "
            f"auto-repair for this failure")
        doc_reversed = self._document(mode)
        replace_editor_text(self.page, mode, before)
        revert_effect = self._effect(mode, doc_reversed, "reversal put back")
        final = self.run_checks()
        return {"succeeded": False, "still_failing": sorted(still_failing),
                "steps": [{"testid": REVERSE_TESTID, "report": report, "effect": effect},
                          {"testid": REVERSE_REVERT_TESTID, "report": final, "effect": revert_effect}]}

    def walk_back_to_known_good(self, persisting_names: set[str], last_action_testid: str) -> dict:
        """Undo one click at a time until the live state equals the last known-good snapshot.

        The state is re-read after each click, so no fixed undo count is assumed (one step can take several undo
        entries). max_undo_attempts only guards against a runaway loop; hitting it is not success.

        No click is made when the known-good state is further back than max_undo_attempts edits in the current mode
        (e.g. INV-D6 is only evaluated in Data mode, so after a long stretch of Schema edits the last state where it
        passed is the one before all of them). Clicking stops after unchanged_undo_limit undos in a row that
        change nothing (empty undo stack in that mode), and only the undos that changed something are redone.

        Matching the snapshot is necessary but not sufficient: it covers only de_data, de_schema, se_schema and se_data,
        not the raw Text View content, which some failures depend on. Success is therefore confirmed by a fresh
        invariant check of the persisting failures. Without a known-good snapshot this falls back to
        single_undo_or_revert.

        Returns {"succeeded": bool, "steps": [...]} with one real click per entry. "steps" may be empty if no click
        was needed.
        """
        say = self.say
        target_snapshot, target_step = self.find_last_known_good_snapshot(persisting_names)
        if target_snapshot is None:
            say(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
            say("      No known-good state on record to walk back to — "
                "falling back to the single-undo policy")
            return self.single_undo_or_revert(persisting_names, last_action_testid)

        current_mode = _get_current_active_mode(self.page)
        mode_undo, mode_redo = undo_redo_for_mode(current_mode)
        max_undo_attempts = self.limits.max_undo_attempts
        unchanged_undo_limit = self.limits.unchanged_undo_limit

        say(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
        edits_back = self.edits_since(target_step, current_mode)
        if edits_back > max_undo_attempts:
            say(f"      The last state where it passed is step {target_step}, {edits_back} {current_mode} edits back "
                f"(more than {max_undo_attempts} undos) - not walking back, giving up on auto-repair for this failure")
            return {"succeeded": False, "still_failing": sorted(persisting_names), "steps": []}
        say(f"      Walking back toward the recorded state from step {target_step}")

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
        if self.read_full_snapshot() == target_fields:
            fresh = self.run_checks()
            if _cleared(fresh):
                resolved = True
                say(f"      ✓ Live state already matches step {target_step}'s recorded state, "
                    f"and the failure is confirmed resolved (0 undos needed)")
            else:
                say(f"      Live state matches step {target_step}'s recorded snapshot, but "
                    f"{sorted(persisting_names)} still fails there too — undo/redo can't fix "
                    f"this (it depends on something the snapshot doesn't track, e.g. raw Text "
                    f"View editor content) — giving up without clicking anything")
        else:
            state_before_click = self.read_full_snapshot()
            for attempt in range(1, max_undo_attempts + 1):
                effect = self.click_with_effect(mode_undo)
                clicks_done += 1
                report_now = self.run_checks()
                steps.append({"testid": mode_undo, "report": report_now, "effect": effect})

                state_now = self.read_full_snapshot()
                if state_now == state_before_click:
                    unchanged_in_a_row += 1
                else:
                    unchanged_in_a_row = 0
                    effective_clicks += 1
                state_before_click = state_now

                if state_now == target_fields:
                    if _cleared(report_now):
                        resolved = True
                        say(f"      ✓ Reached step {target_step}'s recorded state after "
                            f"{attempt} undo(s), and the failure is confirmed resolved")
                    else:
                        say(f"      Reached step {target_step}'s recorded state after "
                            f"{attempt} undo(s), but {sorted(persisting_names)} still fails "
                            f"there too — undo/redo can't fix this one")
                    break

                if unchanged_in_a_row >= unchanged_undo_limit:
                    say(f"      {mode_undo} changed nothing {unchanged_in_a_row} times in a row (nothing left to undo in "
                        f"this mode) - stopping the walk-back")
                    break

        if resolved:
            return {"succeeded": True, "cleared": sorted(persisting_names), "steps": steps}

        say(f"      ✗ Reverting {effective_clicks} undo(s) and giving up on auto-repair for this failure")
        for _ in range(effective_clicks):
            effect = self.click_with_effect(mode_redo)
            report_now = self.run_checks()
            steps.append({"testid": mode_redo, "report": report_now, "effect": effect})

        return {"succeeded": False, "still_failing": sorted(persisting_names), "steps": steps}

    def single_undo_or_revert(self, persisting_names: set[str], last_action_testid: str) -> dict:
        """Click undo once (or the opposite, if the last action was itself undo/redo).

        If every persisting failure clears, keep it. Otherwise click the opposite to restore the previous state and
        stop repairing that failure while it persists.

        Returns {"succeeded": bool, "steps": [...]}, one real click per entry.
        """
        say = self.say
        current_mode = _get_current_active_mode(self.page)
        mode_undo, mode_redo = undo_redo_for_mode(current_mode)

        if last_action_testid == mode_undo:
            repair_click, revert_click = mode_redo, mode_undo
        elif last_action_testid == mode_redo:
            repair_click, revert_click = mode_undo, mode_redo
        else:
            repair_click, revert_click = mode_undo, mode_redo

        say(f"\n   Persisting failure(s) detected: {sorted(persisting_names)}")
        say(f"      Attempting repair: {repair_click}")
        repair_effect = self.click_with_effect(repair_click)

        post_repair_report = self.run_checks()
        still_failing = {r.name for r in post_repair_report.failures} & persisting_names

        if not still_failing:
            say("      ✓ Repair succeeded — persisting failure(s) cleared")
            return {
                "succeeded": True,
                "cleared": sorted(persisting_names),
                "steps": [{"testid": repair_click, "report": post_repair_report, "effect": repair_effect}],
            }

        say(f"      ✗ Repair did not clear {sorted(still_failing)} — reverting "
            f"({revert_click}) and giving up on auto-repair for this failure "
            f"(it will keep logging normally, just without further repair attempts)")
        revert_effect = self.click_with_effect(revert_click)
        final_report = self.run_checks()

        return {
            "succeeded": False,
            "still_failing": sorted(still_failing),
            "steps": [
                {"testid": repair_click, "report": post_repair_report, "effect": repair_effect},
                {"testid": revert_click, "report": final_report, "effect": revert_effect},
            ],
        }

    # -- clear-text -------------------------------------------------------------------------------------------

    def undo_clear_text(self, mode: str | None) -> dict:
        """The unconditional undo after a clear-text action in editor `mode`: one click, then a fresh check.

        Known gap: if clear-text was a no-op on an already empty document, the undo reverts the previous action instead.
        """
        undo_testid, _ = undo_redo_for_mode(mode)
        self.say(f"\n   Auto-undoing the clear-text action (it is always reverted after its check)")
        effect = self.click_with_effect(undo_testid)
        report = self.run_checks()
        return {"succeeded": report.passed, "steps": [{"testid": undo_testid, "report": report, "effect": effect}]}

    # -- invalid schema -----------------------------------------------------------------------------------------

    def recover_invalid_schema(self, reason: str) -> dict:
        """Undo in the Schema Editor until the schema is valid against the meta-schema again.

        Switches to Schema mode first if needed and back afterwards (the two switches are not steps; only their number
        is returned as "mode_switches"). If max_recovery_undos undos do not get there, the undos are redone, and the
        caller must stop forcing recovery until the schema is valid again ("recovered" False). Returns
        {"recovered": bool, "steps": [...], "mode_switches": int, "last_testid": the last control clicked}; each
        click's "role" tells undo from revert.
        """
        say = self.say
        start_mode = _get_current_active_mode(self.page)
        say(f"\n   Invalid schema ({reason}) - undoing in the Schema Editor until it is valid again")
        steps: list[dict] = []
        mode_switches = 0
        if start_mode != "schemaEditor":
            self.click(_MODE_SWITCH_TESTID)
            self.run_checks()
            mode_switches += 1

        tp = TestPanel(self.page, SessionMode.DataEditor)
        recovered = False
        clicks = 0
        for attempt in range(1, self.limits.max_recovery_undos + 1):
            effect = self.click_with_effect("schema_undo")
            clicks += 1
            rep = self.run_checks()
            violations = schema_meta_violations(self.page, tp.get_schema())
            recovered = violations == []
            steps.append({"testid": "schema_undo", "report": rep, "role": "invalid_schema_recovery", "ok": recovered,
                          "effect": effect})
            if recovered:
                say(f"      ✓ Schema valid again after {attempt} undo(s)")
                break

        if not recovered:
            say(f"      ✗ Still invalid after {clicks} undo(s) - reverting and not forcing recovery again until the schema is valid")
            for _ in range(clicks):
                effect = self.click_with_effect("schema_redo")
                rep = self.run_checks()
                steps.append({"testid": "schema_redo", "report": rep, "role": "invalid_schema_recovery_revert", "ok": False,
                              "effect": effect})

        if start_mode != "schemaEditor":
            self.click(_MODE_SWITCH_TESTID)
            self.run_checks()
            mode_switches += 1
        last_testid = _MODE_SWITCH_TESTID if start_mode != "schemaEditor" else steps[-1]["testid"]
        return {"recovered": recovered, "steps": steps, "mode_switches": mode_switches, "last_testid": last_testid}


# -- the record on the triggering step ------------------------------------------------------------------------------

def _click_record(testid: str, report: InvariantReport | None, role: str, description: str, effect=None, **extra) -> dict:
    """One click as stored in a repair block: what the report and triage need, not a replayable step."""
    failed = [r.name for r in report.failures] if report is not None else []
    checked = ([r.name.partition(" ")[0] for r in report.results if not getattr(r, "skipped", False)]
               if report is not None else [])
    return {"testid": testid, "role": role, "description": description,
            "passed": report.passed if report is not None else None,
            "failed_invariants": failed, "checked_invariants": checked,
            **({"effect": effect} if effect is not None else {}), **extra}


def repair_block(kind: str, steps: list[dict], outcome: str, *, description_prefix: str = "", **extra) -> dict:
    """The record of one repair, kept in StepResult.kwargs["repairs"] and written to history.json as "repairs".

    kind: KIND_FAILURE, KIND_CLEAR_TEXT or KIND_INVALID_SCHEMA. outcome: "succeeded" or "gave_up" (failure repair; with no
    clicks when the live state already was the known-good one, or when there was nothing to walk back to), "done" or
    "failed" (clear-text: the check after the undo passed or not), "recovered" or "gave_up" (invalid schema), or, in a
    replay only, "not_needed" (the failure or invalid schema the original run repaired did not occur). steps: the
    engine's click entries. Replay does not replay "clicks": it reruns the repair, so they only log what was clicked.
    """
    clicks = []
    for i, st in enumerate(steps):
        is_last = i == len(steps) - 1
        if kind == KIND_FAILURE:
            persisting = extra.get("persisting_failures", [])
            if outcome == "succeeded":
                role, desc = "succeeded", f"auto-repair cleared persisting failure(s) {persisting}"
            elif is_last:
                role, desc = "revert", (f"auto-repair did not clear {extra.get('still_failing', persisting)} "
                                        f"— reverted, giving up on further repair for this failure")
            else:
                role, desc = "attempt", f"attempting auto-repair for persisting failure(s) {persisting}"
        elif kind == KIND_CLEAR_TEXT:
            role, desc = "clear_text_auto_undo", f"auto-undo of {description_prefix} (unconditional, not failure-gated)"
        else:
            role = st.get("role", "invalid_schema_recovery")
            desc = f"invalid-schema recovery ({extra.get('reason', '')})"
        clicks.append(_click_record(st["testid"], st.get("report"), role, desc, effect=st.get("effect"),
                                    **({"recovery_reason": extra["reason"]} if kind == KIND_INVALID_SCHEMA else {})))
    testids = [c["testid"] for c in clicks]
    reverse = any(t in (REVERSE_TESTID, REVERSE_REVERT_TESTID) for t in testids)
    return {"kind": kind, "method": "reverse" if reverse else "undo", "outcome": outcome, "clicks": clicks,
            "undos": sum(t.endswith("undo") for t in testids), "redos": sum(t.endswith("redo") for t in testids),
            "reversals": sum(t == REVERSE_TESTID for t in testids),
            **extra}