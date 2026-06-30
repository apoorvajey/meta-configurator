"""
replay_runner.py
================
Located at: tests/mutation_testing/replay_runner.py

Replays a saved mutation loop history to reproduce bugs.

The history is exported from a MutationLoop run as a JSON file,
then replayed step by step using the same testids and values.

Usage:
    # First export the history from a run:
    loop.export_history("history.json")

    # Then replay it:
    $env:PYTHONPATH = "."
    python -m tests.mutation_testing.replay_runner --history history.json

    # Replay only up to a specific step (e.g. to reproduce step 50):
    python -m tests.mutation_testing.replay_runner --history history.json --until 50

    # Replay with a pause before each step for inspection:
    python -m tests.mutation_testing.replay_runner --history history.json --interactive
"""

from __future__ import annotations

import json
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

from tests.shared.python.utils import (
    SessionMode,
    setup_test_panel_via_localstorage,
    dismiss_dialog_if_present,
)
from tests.mutation_testing.dom_discoverer import InteractionExecutor, ElementDescriptor
from tests.invariants.invariants_core import InvariantChecker

DATA_DIR = Path(__file__).parent / "test_data"
from tests.mutation_testing.log_exporter import export_html_log

# ---------------------------------------------------------------------------
# History export — add this method to MutationLoop
# ---------------------------------------------------------------------------

def export_history(history: list, path: str = "history.json") -> None:
    """
    Export a MutationLoop history to JSON for later replay.
    Call as: loop.export_history("history.json")
    or directly: export_history(loop.history, "history.json")
    """
    records = []
    for s in history:
        records.append({
            "action_id":       s.action_id,
            "description":     s.description,
            "testid":          s.kwargs.get("testid", ""),
            "value":           s.kwargs.get("value", ""),
            "mode":            s.report.mode if s.report else "",
            "action_succeeded": s.action_succeeded,
            "passed":          s.invariants_passed,
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    print(f"\n💾 History exported → {path}")
    print(f"   {len(records)} steps saved")
    print(f"   Replay with: python -m tests.mutation_testing.replay_runner --history {path}")


# ---------------------------------------------------------------------------
# ReplayRunner
# ---------------------------------------------------------------------------

class ReplayRunner:

    def __init__(self, page, history: list[dict], interactive: bool = False, pause_at: int | None = None):
        self.page = page
        self.history = history
        self.interactive = interactive
        self.pause_at = pause_at
        self.executor = InteractionExecutor(page)

    def run(self, until: int | None = None) -> None:
        from tests.mutation_testing.mutation_loop import StepResult
        from tests.mutation_testing.log_exporter import export_html_log
        from tests.invariants.invariants_core import InvariantReport

        steps = self.history[:until] if until else self.history
        total = len(steps)
        print(f"\n=== Replaying {total} steps ===")
        if self.interactive:
            print("   [Enter] Next step   [q] Quit\n")

        replay_history = []

        for i, step in enumerate(steps):
            testid = step["testid"]
            value = step["value"] if step["value"] else None
            mode = step["mode"]
            desc = step["description"]
            succeeded = step["action_succeeded"]
            passed = step["passed"]

            orig_status = "✓" if passed else "✗" if succeeded else "⚠"

            print(f"\n--- Step {i + 1}/{total} [orig: {orig_status}] ---")
            print(f"   {desc}")
            print(f"   testid: {testid}  value: {repr(value)}  mode: {mode}")

            # Switch to interactive mode at pause-at step  ← ADD HERE
            if self.pause_at and i + 1 >= self.pause_at:
                self.interactive = True

            if not testid:
                print("   ⏭  No testid — skipping")
                continue

            interaction = self._infer_interaction(testid)
            el = ElementDescriptor(
                testid=testid,
                tag="",
                role="",
                interaction=interaction,
                label=testid,
            )

            if testid.startswith("expand-") or testid.startswith("collapse-"):
                path = testid.split("-", 1)[1]
                el.js_click = f"""(function() {{
                    const label = document.getElementById('_label_' + '{path}');
                    if (!label) return;
                    const row = label.closest('tr');
                    if (!row) return;
                    const btn = row.querySelector('button.p-treetable-node-toggle-button');
                    if (btn) btn.click();
                }})()"""

            if testid.startswith("rename-property-"):
                print(f"   [replay-debug] forcing rename to value={value!r}")

            action_succeeded = True
            action_error = None
            try:
                self.executor.execute(el, value if value else None)
                print(f"   ✓ Executed")
            except Exception as e:
                action_succeeded = False
                action_error = str(e)
                print(f"   ⚠ Execution error: {e}")

            report = None
            try:
                checker = InvariantChecker(self.page, SessionMode.DataEditor)
                report = checker.run_all()
                report.print()

                if not report.passed:
                    print(f"\n   ⚠  Invariant failure — browser is paused.")
                    print(f"   Inspect the app, then [Enter] to continue or [q] to quit.")
                    try:
                        choice = input("   > ").strip().lower()
                        if choice == "q":
                            print("   Stopping replay.")
                            break
                    except EOFError:
                        pass

            except Exception as e:
                print(f"   ⚠ Invariant check error: {e}")

            # Record step for log
            replay_history.append(StepResult(
                action_id=f"replay:{testid}",
                description=desc,
                kwargs={"testid": testid, "value": value or ""},
                action_succeeded=action_succeeded,
                action_error=action_error,
                report=report,
            ))

            if self.interactive:
                choice = input("   > ").strip().lower()
                if choice == "q":
                    print("   Stopping replay.")
                    break

            if i == total - 1:
                print(f"\n✅ Replay complete — browser is open for inspection.")
                input("Press Enter to close...")

        # Export log
        export_html_log(replay_history, path="replay_log.html")

    def _infer_interaction(self, testid: str) -> str:
        """Infer the interaction type from the testid pattern."""
        fill_prefixes = ("input-string-", "input-number-", "input-boolean-")
        select_prefixes = ("format-selector", "input-select-")
        click_prefixes = (
            "add-item-", "add-property-", "remove-property-",
            "rename-property-", "expand-", "collapse-",
            "mode-active-",
        )
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


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Replay a mutation loop history")
    parser.add_argument("--history",     required=True, help="Path to history JSON file")
    parser.add_argument("--until",       type=int,      help="Replay only up to this step number")
    parser.add_argument("--interactive", action="store_true", help="Pause before each step")
    parser.add_argument("--pause-at", type=int, help="Switch to interactive mode at this step")
    args = parser.parse_args()

    history_path = Path(args.history)
    if not history_path.exists():
        print(f"❌ History file not found: {history_path}")
        return

    with open(history_path, encoding="utf-8") as f:
        history = json.load(f)

    print(f"📂 Loaded {len(history)} steps from {history_path}")
    if args.until:
        print(f"   Replaying up to step {args.until}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
        page    = browser.new_page(no_viewport=True)

        # Setup
        setup_test_panel_via_localstorage(page)
        page.wait_for_timeout(2000)
        dismiss_dialog_if_present(page)

        # Load schema + data (same as run_demo)
        page.get_by_role("button", name="Example Schemas").click()
        page.wait_for_timeout(1000)
        page.get_by_role("option", name="Autonomous Vehicle Schema").click()
        page.wait_for_timeout(1000)
        print("✓ Schema loaded")

        page.get_by_text("Data", exact=True).click()
        page.wait_for_timeout(1000)
        page.get_by_text("Text View", exact=True).click()
        page.wait_for_timeout(500)

        with page.expect_file_chooser() as fc_info:
            page.locator("[data-icon='folder-open']").click()
        fc_info.value.set_files(str(DATA_DIR / "self_driving_vehicle_data.json"))
        page.wait_for_timeout(1000)
        print("✓ Data loaded")

        page.evaluate("document.body.style.zoom = '0.75'")

        # Run replay
        runner = ReplayRunner(page, history, interactive=args.interactive, pause_at=args.pause_at)
        runner.run(until=args.until)

        browser.close()


if __name__ == "__main__":
    main()