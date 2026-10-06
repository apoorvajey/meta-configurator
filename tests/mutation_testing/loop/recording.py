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
        from tests.mutation_testing.replay.history_io import export_history
        export_history(self.history, path,
                        schema_option_name=schema_option_name, data_file=data_file,
                        run_config=run_config, resolved_settings=resolved_settings)

    def _capture_screenshot(self, filename: str, invariants_failed: bool) -> str | None:
        """Screenshot of a logged step (action, repair click or clear-text auto-undo); None if screenshots are off.

        If invariants failed, the page first scrolls to the validation error. That changes page state, so replay
        repeats it using resolved_settings.screenshots_enabled from history.json.
        """
        if not self._screenshots_enabled:
            return None
        if invariants_failed:
            self._scroll_to_validation_error()
        screenshot_path = os.path.join(self._screenshots_dir, filename)
        self.page.screenshot(path=screenshot_path)
        print(f"   {screenshot_path}")
        return screenshot_path

    def _build_snapshot(self, action: str, step: int) -> dict:
        """Snapshot via build_snapshot(), tagged with this run's dataset and run_id (dataset is None if no schema/data pair was given)."""
        return build_snapshot(self.page, action, step, self.run_id, self._dataset)

    def _append_snapshot(self, snapshot: dict) -> None:
        """Append one snapshot as a JSON line to invariant_miner_traces/<dataset>.jsonl (_untagged.jsonl without a dataset).

        Written per snapshot so a run killed by the batch timeout keeps its trace; only the last line can be cut off,
        which the loader skips.
        """
        slug = self._dataset or "_untagged"
        out = Path("invariant_miner_traces") / f"{slug}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, default=str))
            f.write("\n")
            f.flush()

    def _save_snapshots(self) -> None:
        """Print how many snapshots this run recorded (they were written by _append_snapshot())."""
        n = sum(1 for s in self.history if s.snapshot is not None)
        print(f"[SnapshotCollector] {n} snapshot(s) written this run "
              f"(appended incrementally to invariant_miner_traces/"
              f"{self._dataset or '_untagged'}.jsonl as each was recorded)")

    def _scroll_to_validation_error(self) -> None:
        scroll_to_validation_error(self.page)
