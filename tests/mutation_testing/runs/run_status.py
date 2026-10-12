"""Marks a finished run folder as clean or not, so good and bad runs can be told apart without opening them.

write_run_status(history_json) reads the history.json that a loop run or replay just wrote and puts one marker file
next to it, replacing any earlier marker:

    RUN_CLEAN.json               every step was checked and no invariant failed
    RUN_FAILURES.json            at least one step failed an invariant
    RUN_DIVERGED_CLEAN.json      a replay left the original run at some step (e.g. a tool fix made a failed action
    RUN_DIVERGED_FAILURES.json   work) and saw no failure / at least one failure; later steps are not a
                                 step-for-step re-check
    RUN_INCOMPLETE.json          no failure seen, but some steps were never reached

The marker also decides the _STATUS__<tag>.json file (run_layout.mark_run_dir). A run folder without a marker did
not finish. A step counts as failed when passed is False and its action succeeded (the report's rule); actions the
tool could not perform are counted in the marker but do not make a run unclean.
"""
from __future__ import annotations

import json
from pathlib import Path

MARKERS = {"clean": "RUN_CLEAN.json", "failures": "RUN_FAILURES.json",
           "diverged-clean": "RUN_DIVERGED_CLEAN.json", "diverged-failures": "RUN_DIVERGED_FAILURES.json",
           "incomplete": "RUN_INCOMPLETE.json"}
MAX_LISTED_STEPS = 20


def _invariant_id(entry) -> str:
    text = entry.get("id") if isinstance(entry, dict) else str(entry)
    return (text or "").split(" ", 1)[0]


def write_run_status(history_json: str | Path) -> Path:
    history_json = Path(history_json)
    with history_json.open(encoding="utf-8") as f:
        data = json.load(f)
    steps = data["steps"] if isinstance(data, dict) else data
    meta = (data.get("meta") or {}) if isinstance(data, dict) else {}
    replay = meta.get("replay")

    failing = [i for i, s in enumerate(steps) if s.get("passed") is False and s.get("action_succeeded") is not False]
    failed_ids = sorted({_invariant_id(e) for i in failing for e in (steps[i].get("failed_invariants") or [])})
    not_reached = sum(1 for s in steps if s.get("replayed") is False)
    unfaithful = (replay or {}).get("steps_unfaithful", 0)

    if failing:
        status = "diverged-failures" if unfaithful else "failures"
    elif not_reached:
        status = "incomplete"
    elif unfaithful:
        status = "diverged-clean"
    else:
        status = "clean"

    summary = {
        "status": status,
        "kind": "replay" if replay else "loop",
        # steps_total / failing_steps count every record, repair and recovery clicks included. loop_steps is the number
        # of steps the loop itself took (the run's num_steps); repair_clicks are the undo/redo/mode-switch clicks it added.
        "steps_total": len(steps),
        "loop_steps": len({s.get("step", i) for i, s in enumerate(steps)}),
        "repair_clicks": sum(1 for s in steps if str(s.get("action_id", "")).startswith("repair:")),
        "failing_steps": len(failing),
        "failing_step_numbers": [i + 1 for i in failing[:MAX_LISTED_STEPS]],   # 1-based, as in the logs
        "invariants_failed": failed_ids,
        "failed_actions": sum(1 for s in steps if s.get("action_succeeded") is False),
    }
    if replay:
        summary.update(steps_not_reached=not_reached, steps_diverged=unfaithful,
                       source_run_id=replay.get("source_run_id"))

    folder = history_json.parent
    for name in MARKERS.values():
        (folder / name).unlink(missing_ok=True)
    out = folder / MARKERS[status]
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nRun status: {status.upper()} -> {out}")
    return out


if __name__ == "__main__":
    # python -m tests.mutation_testing.runs.run_status [runs]
    # Re-marks every run and replay under the folder and writes the _STATUS__ file of each run folder
    # (an old __<tag> in a folder name is removed once).
    import sys
    from tests.mutation_testing.runs.run_layout import iter_run_dirs, mark_run_dir
    roots = [Path(a) for a in sys.argv[1:]] or [Path("tests/output/runs")]
    for root in roots:
        for hist in sorted(root.rglob("history.json")):
            write_run_status(hist)
        for run_dir in iter_run_dirs(root):
            mark_run_dir(run_dir)