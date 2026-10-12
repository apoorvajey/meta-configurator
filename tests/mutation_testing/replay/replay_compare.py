"""Original run vs replay: how the two differ.

Called by replay_runner after a replay. It compares the original history.json with the replay and prints

    steps, failing steps, per-invariant failure counts (original -> replay), how many recorded states are identical
    on replay, where the replay diverged, and what happened to the run's snapshots in tests/output/traces/

and saves the same as comparison.json in the replay's own folder (tests/output/runs/<run_id>/replays/<replay_id>/). The summary is
also appended to the corpus log
(python -m tests.cli archive --log) and shown by the corpus report (python -m tests.cli corpus).
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

COMPARISON_FILE = "comparison.json"


def _ids(names) -> set[str]:
    """Invariant ids of a step's failed invariants ('INV-D6 some message' -> 'INV-D6')."""
    return {str(n).partition(" ")[0] for n in (names or [])}


def _original(history: list[dict]) -> dict:
    failing = 0
    counts: Counter = Counter()
    snapshots = 0
    for rec in history:
        if rec.get("state_hash"):
            snapshots += 1
        if rec.get("action_succeeded") and rec.get("passed") is False:
            failing += 1
            for i in _ids(rec.get("failed_invariants")):
                counts[i] += 1
    return {"steps": len(history), "failing_steps": failing, "failures": dict(counts), "snapshots": snapshots}


def _replay(runner, outcome) -> dict:
    counts: Counter = Counter()
    for entry in getattr(runner, "replay_history", []) or []:
        report = getattr(entry, "report", None)
        if report is not None and not report.passed:
            for i in _ids(r.name for r in report.failures):
                counts[i] += 1
    return {"steps": outcome.steps_run, "failing_steps": outcome.failing_steps, "failures": dict(counts),
            "snapshots": len(getattr(runner, "replayed_snapshots", []) or [])}


def _identical_states(history: list[dict], runner) -> tuple[int, int]:
    """(replayed states whose hash equals the original's, replayed states that have an original hash to compare to)."""
    from tests.mutation_testing.runs.reproducibility import _state_hash
    expected = {rec.get("step"): rec.get("state_hash") for rec in history if rec.get("state_hash")}
    same = total = 0
    for snap in getattr(runner, "replayed_snapshots", []) or []:
        want = expected.get(snap.get("step"))
        if want is None:
            continue
        total += 1
        same += _state_hash(snap) == want
    return same, total


def compare_and_report(history: list[dict], runner, outcome, run_id: str | None, dataset: str | None,
                       trace_effect: dict, out_dir: str | Path = ".", traces_dir: str | None = None) -> dict:
    """Print and save the comparison to <out_dir>/comparison.json (out_dir = the replay's folder).
    trace_effect is {"removed": n, "added": m} or {"skipped": reason}."""
    orig, new = _original(history), _replay(runner, outcome)
    same, compared = _identical_states(history, runner)
    result = {
        "run_id": run_id, "dataset": dataset, "compared_at": datetime.now().isoformat(timespec="seconds"),
        "original": orig, "replay": new,
        "diverged_at": outcome.diverged_at, "identical_states": same, "compared_states": compared,
        "complete": not outcome.stopped_early, "trace": trace_effect,
    }

    ids = sorted(set(orig["failures"]) | set(new["failures"]),
                 key=lambda i: -(orig["failures"].get(i, 0) + new["failures"].get(i, 0)))
    w = 34
    print(f"\n{'=' * 60}\nReplay vs original run {run_id or '?'}\n{'=' * 60}")
    print(f"{'':{w}}{'original':>10}{'replay':>10}")
    print(f"{'steps':{w}}{orig['steps']:>10}{new['steps']:>10}")
    print(f"{'steps with a failing invariant':{w}}{orig['failing_steps']:>10}{new['failing_steps']:>10}")
    print(f"{'states recorded as snapshots':{w}}{orig['snapshots']:>10}{new['snapshots']:>10}")
    if ids:
        print("\nSteps on which each invariant failed:")
        for i in ids:
            a, b = orig["failures"].get(i, 0), new["failures"].get(i, 0)
            note = "gone" if a and not b else "new" if b and not a else "fewer" if b < a else "more" if b > a else ""
            print(f"  {i:{w - 2}}{a:>10}{b:>10}   {note}")
    else:
        print("\nNo invariant failed in either run.")
    if compared:
        print(f"\nStates identical to the original: {same} of {compared}")
    print("Divergence: " + (f"replay left the original run at step {outcome.diverged_at}" if outcome.diverged_at
                           else "none - the replay tracked the original"))
    if "removed" in trace_effect:
        print(f"Snapshots in the trace file: {trace_effect['removed']} replaced by {trace_effect['added']}")
    else:
        print(f"Snapshots in the trace file: unchanged ({trace_effect.get('skipped', 'not updated')})")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / COMPARISON_FILE
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Comparison saved: {path}")
    try:
        from tests.mutation_testing.traces.trace_store import TRACES_DIR, log_event
        log_event(traces_dir or TRACES_DIR, "replay", run_id=run_id, dataset=dataset,
                  failing_steps=f"{orig['failing_steps']}->{new['failing_steps']}",
                  snapshots=f"{orig['snapshots']}->{new['snapshots']}", diverged_at=outcome.diverged_at,
                  trace=("replaced" if "removed" in trace_effect else "unchanged"))
    except Exception as e:  # the comparison is already printed and saved
        print(f"(could not add the comparison to the corpus log: {e})")
    return result