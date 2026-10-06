"""Console summary of a run."""
from __future__ import annotations




class ReportingMixin:
    """Summary printed at the end of a run."""

    def print_summary(self):
        total = len(self.history)
        action_failures = sum(1 for s in self.history if not s.action_succeeded)
        invariant_failures = sum(
            1 for s in self.history
            if s.action_succeeded and s.report and not s.report.passed
        )
        print(f"\n{'=' * 60}")
        print("Mutation loop summary")
        print(f"  Steps executed : {total}")
        print(f"  Action errors  : {action_failures}")
        print(f"  Invariant fails: {invariant_failures}")

        no_toggle_count = getattr(getattr(self, "_executor", None), "_silent_no_toggle_count", 0)
        if no_toggle_count:
            print(f"  Ensure-visible : {no_toggle_count} ancestor(s) had no toggle button "
                  f"this run (composition keywords already excluded — see discovery/discoverer.py)")

        if invariant_failures > 0:
            print("\nFailed steps (grouped — full step-by-step detail is in the HTML "
                  "log and history.json, not repeated here):")
            self._print_failure_groups()

    def _print_failure_groups(self):
        """Print one line per run of consecutive steps that fail with the same (invariant, message) signature.

        A new signature always gets its own line; repeats after repair gave up are collapsed into an "Nx" count.
        """
        run_start = None
        run_len = 0
        run_sig = None  # frozenset of (name, message) for the current run

        def flush():
            if run_sig is None:
                return
            first = self.history[run_start]
            if run_len == 1:
                print(f"\n  Step {run_start + 1}: {first.action_id}")
            else:
                last = self.history[run_start + run_len - 1]
                print(f"\n  Steps {run_start + 1}\u2013{run_start + run_len} "
                      f"({run_len}x, first: {first.action_id}, last: {last.action_id}):")
            for name, message in sorted(run_sig):
                print(f"    \u2717 {name}: {message}")

        for i, s in enumerate(self.history):
            if not (s.action_succeeded and s.report and not s.report.passed):
                flush()
                run_sig = None
                continue
            sig = frozenset((f.name, f.message) for f in s.report.failures)
            if sig == run_sig:
                run_len += 1
            else:
                flush()
                run_start, run_len, run_sig = i, 1, sig
        flush()
