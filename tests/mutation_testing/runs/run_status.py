"""Marks a finished run folder as clean or not, so good and bad runs can be told apart without opening them.

write_run_status(history_json) reads the history.json that a loop run or replay just wrote and puts one marker file
next to it, replacing any earlier marker:

    RUN_CLEAN.json               every step was checked and no invariant failed
    RUN_FAILURES.json            at least one step failed an invariant that is not a known bug
    RUN_KNOWN_BUGS.json          steps failed, but only on known bugs (see known_bugs.py); not clean, not failures
    RUN_DIVERGED_CLEAN.json      a replay left the original run at some step (e.g. a tool fix made a failed action
    RUN_DIVERGED_FAILURES.json   work) and saw no failure / at least one failure; later steps are not a
                                 step-for-step re-check
    RUN_INCOMPLETE.json          no failure seen, but some steps were never reached

The marker also decides the _STATUS__<tag>.json file (run_layout.mark_run_dir). A run folder without a marker did
not finish. A step counts as failed when passed is False and its action succeeded (the report's rule); actions the
tool could not perform are counted in the marker but do not make a run unclean. Failures of known bugs are
counted apart (known_bug_steps) and never make a run `failures`; a step that also fails something else is a failure.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.mutation_testing.runs.known_bugs import KnownBugMatcher, observe_history_step
from tests.mutation_testing.runs.repair_view import expand_repairs

MARKERS = {"clean": "RUN_CLEAN.json", "failures": "RUN_FAILURES.json", "known-bugs": "RUN_KNOWN_BUGS.json",
           "diverged-clean": "RUN_DIVERGED_CLEAN.json", "diverged-failures": "RUN_DIVERGED_FAILURES.json",
           "incomplete": "RUN_INCOMPLETE.json"}
MAX_LISTED_STEPS = 20


def summarise_steps(steps: list[dict], meta: dict | None = None) -> dict:
    """The status of a run from its step records (repair clicks already expanded, see expand_repairs).

    The single place that decides clean / known-bugs / failures. write_run_status writes it next to the history and
    the batch summary reads it, so the two cannot disagree. Only the loop's own steps decide the status: a repair
    click that left a rule failing is reported (repair_failing_*) but never makes a run unclean.
    """
    replay = (meta or {}).get("replay")

    matcher = KnownBugMatcher()
    failing, known_only = [], []          # step indexes: some failure that is not a known bug / only known-bug failures
    repair_failing: list[int] = []        # repair clicks that left a rule failing: reported, never decide the status
    repair_failed_ids: set[str] = set()
    failed_ids: set[str] = set()
    failing_by_invariant: dict[str, int] = {}   # invariant id -> number of loop steps it failed on (known bugs excluded)
    known_by_bug: dict[str, list[int]] = {}
    for i, s in enumerate(steps):
        known, other = observe_history_step(matcher, s)   # every step, so failure streaks start and end correctly
        if s.get("passed") is not False or s.get("action_succeeded") is False:
            continue
        if str(s.get("action_id", "")).startswith("repair:"):
            repair_failing.append(i)
            repair_failed_ids |= other | set(known)
            continue
        if other:
            failing.append(i)
            failed_ids |= other
            for name in other:
                failing_by_invariant[name] = failing_by_invariant.get(name, 0) + 1
        elif known:
            known_only.append(i)
        for bug in set(known.values()):
            known_by_bug.setdefault(bug, []).append(i + 1)
    failed_ids = sorted(failed_ids)
    not_reached = sum(1 for s in steps if s.get("replayed") is False)
    unfaithful = (replay or {}).get("steps_unfaithful", 0)

    if failing:
        status = "diverged-failures" if unfaithful else "failures"
    elif known_only:
        status = "known-bugs"
    elif not_reached:
        status = "incomplete"
    elif unfaithful:
        status = "diverged-clean"
    else:
        status = "clean"

    summary = {
        "status": status,
        "kind": "replay" if replay else "loop",
        # steps_total counts every record, repair clicks included. failing_steps / invariants_failed cover the loop's own
        # steps only; repair clicks that left a rule failing are listed apart (repair_failing_*) and never set the status.
        "steps_total": len(steps),
        "loop_steps": len({s.get("step", i) for i, s in enumerate(steps)}),
        "repair_clicks": sum(1 for s in steps if str(s.get("action_id", "")).startswith("repair:")),
        "failing_steps": len(failing),
        "failing_step_numbers": [i + 1 for i in failing[:MAX_LISTED_STEPS]],   # 1-based, as in the logs
        "invariants_failed": failed_ids,
        "known_bug_steps": len(known_only),   # steps that failed only on known bugs (not in failing_steps)
        "repair_failing_clicks": len(repair_failing),   # repair clicks that left a rule failing (not counted above)
        "repair_failing_invariants": sorted(repair_failed_ids),
        "known_bugs": {b: nums[:MAX_LISTED_STEPS] for b, nums in sorted(known_by_bug.items())},   # bug id -> 1-based step numbers
        "failed_actions": sum(1 for s in steps if s.get("action_succeeded") is False),
    }
    if replay:
        summary.update(steps_not_reached=not_reached, steps_diverged=unfaithful,
                       source_run_id=replay.get("source_run_id"))
    summary["_failing_by_invariant"] = failing_by_invariant   # for the batch summary; not written to the marker
    return summary


def write_run_status(history_json: str | Path) -> Path:
    history_json = Path(history_json)
    with history_json.open(encoding="utf-8") as f:
        data = json.load(f)
    steps = expand_repairs(data["steps"] if isinstance(data, dict) else data)   # repair clicks are records here
    meta = (data.get("meta") or {}) if isinstance(data, dict) else {}
    summary = summarise_steps(steps, meta)
    status = summary["status"]
    summary.pop("_failing_by_invariant", None)

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