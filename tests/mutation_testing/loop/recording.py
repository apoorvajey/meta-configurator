"""Recording of a run: history export, screenshots and state snapshots."""
from __future__ import annotations

import json
import os
from pathlib import Path
from tests.mutation_testing.loop.app_setup import build_snapshot, scroll_to_validation_error


class RecordingMixin:
    """History, screenshot and snapshot recording."""

    def export_history(self, path: str = "history.json",
                        schema_option_name: str | None = None,
                        data_file: str | None = None,
                        run_config: dict | None = None,
                        resolved_settings: dict | None = None) -> None:
        """Export the history to JSON for replay and reports.

        schema_option_name and data_file (a bare file name) record the dataset so replay loads the same pair.
        run_config and resolved_settings are passed through to replay_runner.export_history().
        """
        from tests.mutation_testing.runs.history_io import export_history
        export_history(self.history, path,
                        schema_option_name=schema_option_name, data_file=data_file,
                        run_config=run_config, resolved_settings=resolved_settings)

    def _capture_screenshot(self, filename: str, invariants_failed: bool) -> str | None:
        """Screenshot of a logged step; None if screenshots are off.

        If invariants failed, the page first scrolls to the validation error. That changes page state, so replay
        repeats it using resolved_settings.screenshots_enabled from history.json.
        """
        if not self._screenshots_enabled:
            return None
        if invariants_failed:
            self._scroll_to_validation_error()
        # A step that opened the app's 'Rename conflict' dialog: the executor clicked it away before this call, so a
        # picture taken now would not show it. If the step failed, its screenshot is the one taken with the dialog open.
        dialog_shot = (getattr(getattr(self, "_executor", None), "rename_conflict_shot", None)
                       if invariants_failed else None)
        if dialog_shot:
            screenshot_path = os.path.join(self._screenshots_dir, filename)
            os.makedirs(self._screenshots_dir, exist_ok=True)
            with open(screenshot_path, "wb") as f:
                f.write(dialog_shot)
            print(f"   {screenshot_path}  (taken while the Rename conflict dialog was open)")
            if self._screenshots_failures_only:   # keep the in-memory "previous step" picture as it would have been
                data = self.page.screenshot()
                self._shot_before, self._last_shot = self._last_shot, data
            return screenshot_path
        if self._screenshots_failures_only:
            # The screenshot is taken exactly as with failures-only off, but a passing step's is only kept in
            # memory. If the next logged step fails, it is written out as that step's "previous step" picture.
            # self._shot_before is the one that preceded this call.
            data = self.page.screenshot()
            self._shot_before, self._last_shot = self._last_shot, data
            if not invariants_failed:
                return None
            screenshot_path = os.path.join(self._screenshots_dir, filename)
            os.makedirs(self._screenshots_dir, exist_ok=True)
            with open(screenshot_path, "wb") as f:
                f.write(data)
            print(f"   {screenshot_path}")
            return screenshot_path
        screenshot_path = os.path.join(self._screenshots_dir, filename)
        os.makedirs(self._screenshots_dir, exist_ok=True)
        self.page.screenshot(path=screenshot_path)
        print(f"   {screenshot_path}")
        return screenshot_path

    def _save_pre_step_screenshot(self, filename: str) -> str | None:
        """Failures-only mode: write the screenshot of the previous logged step to disk (called when this step failed).

        It is the picture _capture_screenshot() took for that step (same scroll rule, same call), kept in memory
        because that step passed and so was not written. None if there is no previous step or nothing was kept.
        """
        data, self._shot_before = self._shot_before, None
        if not data:
            return None
        path = os.path.join(self._screenshots_dir, filename)
        os.makedirs(self._screenshots_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        print(f"   {path}  (previous step)")
        return path

    def _build_snapshot(self, action: str, step: int, count_use: bool = True) -> dict:
        """Snapshot via build_snapshot(), tagged with this run's dataset and run_id (dataset is None if no schema/data pair was given).

        count_use=False for the baseline a repair leaves behind: it is not a step of the loop, so it is not a use of its
        action category."""
        snapshot = build_snapshot(self.page, action, step, self.run_id, self._dataset)
        snapshot["nth_use"] = self._count_use(snapshot) if count_use else None
        return snapshot

    def _count_use(self, snapshot: dict) -> int | None:
        """How many steps of this snapshot's action category (with tab) the run has had up to and including this one:
        1 for the first. The inference engine mines the first and the later uses apart, and a rule limited to one of
        them is checked with this number. None for the first snapshot of a run, which has no previous one to pair with."""
        if self._prev_snapshot is None:
            return None
        from tests.mutation_testing.inference.categories import _tabbed_category
        category = _tabbed_category(self._prev_snapshot, snapshot)
        self._use_counts[category] = self._use_counts.get(category, 0) + 1
        return self._use_counts[category]

    def _append_baseline(self, snapshot: dict) -> None:
        """Write the state a repair left behind to the trace, tagged "baseline": true, under the step number of the action
        it followed. It is no step of the loop (no use count, never the 'after' of a pair), but the next step is paired with it,
        so mining sees the state the loop really compared that step with."""
        self._append_snapshot({**snapshot, "baseline": True})

    def _append_snapshot(self, snapshot: dict) -> None:
        """Append one snapshot as a JSON line to tests/output/traces/<dataset>.jsonl (_untagged.jsonl without a dataset).

        Written per snapshot so a run killed by the batch timeout keeps its trace; only the last line can be cut off,
        which the loader skips.
        """
        slug = self._dataset or "_untagged"
        from tests.mutation_testing.common.paths import TRACES_DIR
        out = TRACES_DIR / f"{slug}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, default=str))
            f.write("\n")
            f.flush()

    def _save_snapshots(self) -> None:
        """Print how many snapshots this run recorded (they were written by _append_snapshot())."""
        n = sum(1 for s in self.history if s.snapshot is not None)
        print(f"[SnapshotCollector] {n} snapshot(s) written this run "
              f"(appended incrementally to tests/output/traces/"
              f"{self._dataset or '_untagged'}.jsonl as each was recorded)")

    def _scroll_to_validation_error(self) -> None:
        scroll_to_validation_error(self.page)