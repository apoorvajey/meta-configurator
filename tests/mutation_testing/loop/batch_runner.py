"""Runs the mutation loop N times in a row, unattended, to collect snapshots.

Runs cycle through the schemas listed in "_schema_options" of run_config.json, so each lap covers every schema once.
Before each run the "schema" key of run_config.json is set to that run's dataset; the original content is restored
when the batch ends, also on Ctrl+C or a crash. A summary of attempted and successful runs per dataset is printed at
the end. The loop writes its snapshots to tests/output/traces/<dataset>.jsonl itself.

Before a long batch: archive old snapshots, run from the repo root, turn screenshots off and stop the machine from
sleeping (on Windows the runner also keeps it awake).

--max-minutes M stops the batch from starting a new run after M minutes (the run in progress finishes, at most
--timeout seconds more). Without --runs the time alone limits the number of runs; with both, whichever comes first.
Datasets go round in laps, so a batch stopped mid-lap leaves some datasets with one run less.

The console shows per run only what a quiet replay shows (new or changed invariant failures, repairs, progress every
10 steps, the failure summary); the full output is kept in tests/output/runs/<run_id>/console.log. --verbose prints
everything (same as MT_VERBOSE=1 or "verbose": true in run_config.json).
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

RUN_CONFIG_PATH = Path(__file__).parent.parent / "config" / "run_config.json"

# Batch record: tests/output/reports/batch/batch_<batch_id>.txt (summary), .json (same data), .log (full console output).
from tests.mutation_testing.common.paths import BATCH_DIR, TRACES_DIR
SLOWEST_STEPS_SHOWN = 3
ACTION_TEXT_CHARS = 60
DEFAULT_RUNS = 15
RUNS_CAP_WITH_TIME_BUDGET = 1000   # upper bound on planned runs when only --max-minutes is given

# What is read from a run's console output (the only source for a run that never wrote a history.json)
STEP_RE = re.compile(r"--- Step (\d+)/(\d+) ---")
RUN_ID_RE = re.compile(r"runs[\\/](\d{8}_\d{6})")
ACTION_RE = re.compile(r"▶\s+(.*\S)")
FAILED_CHECK_RE = re.compile(r"\d+/\d+ FAILED ✗")
# Machine-readable lines the child writes when MT_STEP_MARKERS=1 (see loop/console.py). Never shown or logged.
MARKER_RE = re.compile(r"^@@(STEP|ACTION|FAILS) ?(.*)$")
STEP_MARKER_RE = re.compile(r"(\d+)/(\d+)")
# The loop's "... step 10/60 (n failing so far) ..." line; the progress bar replaces it
PROGRESS_LINE_RE = re.compile(r"^\s*\.\.\. step \d+/\d+")
BAR_WIDTH = 20
CRASH_TAIL_LINES = 15   # lines of a failed run's output shown on the terminal when the bar hides the rest


def _listed_options(config: dict, key: str) -> list[str]:
    """The values in one of run_config.json's documentation notes, e.g. "_schema_options": "random | A | B"."""
    note = config.get(key)
    return [value.strip() for value in note.split("|") if value.strip()] if isinstance(note, str) else []


def configured_schemas(config: dict) -> list[str]:
    """The schemas the batch cycles over: the entries of "_schema_options" in run_config.json. "random" is not
    one of them; it is the loop's own way of picking a schema per run."""
    return [name for name in _listed_options(config, "_schema_options") if name != "random"]


def configured_profiles(config: dict) -> list[str]:
    """The action-weight profiles: the entries of "_action_weights_profile_options" in run_config.json."""
    return _listed_options(config, "_action_weights_profile_options")


def read_configured_options(path: Path = RUN_CONFIG_PATH) -> tuple[list[str], list[str]]:
    """(schemas, profiles) listed in run_config.json, without starting a batch. Used by the CLI menu."""
    config = json.loads(path.read_text(encoding="utf-8"))
    return configured_schemas(config), configured_profiles(config)


def _resolve_run_config_path(explicit: str | None) -> Path:
    """The run_config.json to use for this batch. --run-config wins if given and must exist. Otherwise
    the file next to this module is used.
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise FileNotFoundError(f"--run-config was given as {path}, but it doesn't exist.")
        print(f"[config] Using explicitly given run_config.json: {path}")
        return path

    if not RUN_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"run_config.json not found at {RUN_CONFIG_PATH}. Pass --run-config <path>."
        )
    print(f"[config] Using run_config.json: {RUN_CONFIG_PATH}")
    return RUN_CONFIG_PATH


def _child_env(verbose: bool = False) -> dict:
    """Force UTF-8 in the child process (piped output on Windows otherwise uses the system codepage). MT_STEP_MARKERS
    makes the child write the lines the summary needs (step timing, failed checks) although its console is quiet;
    --verbose sets MT_VERBOSE so it prints everything.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["MT_STEP_MARKERS"] = "1"
    env["MT_BATCH"] = "1"
    if verbose:
        env["MT_VERBOSE"] = "1"
    return env


# run_config.json patching: dataset override per run, restored on exit

def _load_run_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found - round-robin needs to read/patch it. "
            f"Run this from the repo root."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _write_run_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _set_config_schema(path: Path, schema_name: str, base_config: dict, profile: str | None = None,
                       num_steps: int | None = None) -> None:
    """Write base_config back with "schema" set to schema_name, "action_weights_profile" set to profile and
    "num_steps" set to num_steps if given. All other keys are left as they were."""
    patched = dict(base_config)
    patched["schema"] = schema_name
    if num_steps is not None:
        patched["num_steps"] = num_steps
    if profile is not None:
        patched["action_weights_profile"] = profile
    _write_run_config(path, patched)


def _plan_text(args) -> str:
    """'15 runs', 'up to 60 min' or '16 runs, at most 60 min'."""
    if not args.max_minutes:
        return f"{args.runs} runs"
    budget = f"{args.max_minutes:g} min"
    return f"up to {budget}" if args.runs >= RUNS_CAP_WITH_TIME_BUDGET else f"{args.runs} runs, at most {budget}"


def _runs_label(args) -> str:
    """The "n" of "run i/n": the planned number of runs, or "?" while only the time budget limits the batch."""
    return "?" if args.max_minutes and args.runs >= RUNS_CAP_WITH_TIME_BUDGET else str(args.runs)


def _build_dataset_cycle(n_runs: int, dataset_names: list[str]) -> list[str]:
    """Dataset for each of n_runs runs, in order.

    Round-robin in laps: every lap runs each dataset once, in a fresh random order, so time-of-night effects
    such as load or throttling do not always hit the same dataset. If n_runs is not a multiple of
    len(dataset_names), the last lap is cut short, so some datasets get one extra run (25 runs over 4 datasets:
    which of them is decided by the shuffle). The summary reports this.
    """
    cycle = []
    while len(cycle) < n_runs:
        lap_order = list(dataset_names)
        random.shuffle(lap_order)
        cycle.extend(lap_order)
    return cycle[:n_runs]


# Windows keep-awake

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def _set_keep_awake(enabled: bool) -> None:
    if platform.system() != "Windows":
        return
    try:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if enabled else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception as e:
        print(f"[keep-awake] Could not set execution state ({e!r}) - "
              f"falling back entirely on your Windows power settings.")


class _StreamStats:
    """What the batch learns from one run's console output: the run id, how far the run got, how long each step
    took and how many failed checks were printed. Exact results come from history.json; this is what is left
    for a run that timed out or crashed before writing one."""

    def __init__(self):
        self.run_id: str | None = None
        self.steps_total: int | None = None
        self.step_started: dict[int, float] = {}
        self.step_action: dict[int, str] = {}
        self.failed_checks = 0
        self._step: int | None = None
        self._markers = False   # once the child sends markers they are the source; the printed lines are a fallback

    def feed(self, line: str, now: float) -> bool:
        """Learn from one output line. Returns True for a marker line, which is for the batch only."""
        marker = MARKER_RE.match(line.strip())
        if marker:
            self._markers = True
            kind, rest = marker.groups()
            if kind == "STEP":
                m = STEP_MARKER_RE.match(rest)
                if m:
                    self._start_step(int(m.group(1)), int(m.group(2)), now)
            elif kind == "ACTION" and self._step is not None:
                self.step_action.setdefault(self._step, rest.strip()[:ACTION_TEXT_CHARS])
            elif kind == "FAILS" and rest.strip().isdigit():
                self.failed_checks += int(rest)
            return True

        if self.run_id is None:
            m = RUN_ID_RE.search(line)
            if m:
                self.run_id = m.group(1)
        m = STEP_RE.search(line)
        if m:
            self._start_step(int(m.group(1)), int(m.group(2)), now)
            return False
        if self._step is not None and self._step not in self.step_action:
            m = ACTION_RE.search(line)
            if m:
                self.step_action[self._step] = m.group(1)[:ACTION_TEXT_CHARS]
        if not self._markers and FAILED_CHECK_RE.search(line):
            self.failed_checks += 1
        return False

    def _start_step(self, step: int, total: int, now: float) -> None:
        self._step, self.steps_total = step, total
        self.step_started.setdefault(step, now)   # a verbose child prints the same step again after its marker

    def step_seconds(self, ended: float) -> dict[int, float]:
        """Seconds per step: from its "--- Step n ---" line to the next one (the last one to `ended`). A step includes
        its recovery clicks."""
        starts = sorted(self.step_started.items())
        return {step: (starts[k + 1][1] if k + 1 < len(starts) else ended) - t0
                for k, (step, t0) in enumerate(starts)}


def _bar_text(label: str, stats: "_StreamStats") -> str:
    """'[run 3/20] [████░░░░] 31/60  ·  2 failed check(s)', cut to the terminal width so it never wraps."""
    step, total = stats._step, stats.steps_total
    if not step or not total:
        return ""
    filled = min(BAR_WIDTH, round(BAR_WIDTH * step / total))
    text = (f"  [{label}] [{'█' * filled}{'░' * (BAR_WIDTH - filled)}] {step}/{total}"
            f"  ·  {stats.failed_checks} failed check(s)")
    return text[:max(20, shutil.get_terminal_size((100, 20)).columns - 1)]


def _kill_tree(proc) -> None:
    """Kill the child and everything it started (Chromium); proc.kill() alone leaves the browser running on Windows."""
    if platform.system() == "Windows":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
            return
        except Exception:
            pass
    proc.kill()


def _run_and_stream(cmd: list, env: dict, stdin_text: str, timeout: int,
                     run_label: str, stats: "_StreamStats | None" = None,
                     log_file=None, bar: bool = False) -> tuple[str, float]:
    """Run `cmd`, streaming its output to the console. Output is read in a background thread
    so the timeout is enforced even when the child prints nothing. `stats` collects what the batch needs from the
    output; `log_file` (open for writing) receives every line. `bar` shows a progress bar on the last terminal line,
    redrawn on every step; it needs `stats`. With the bar on, the run's own output is not printed (the log files
    have it all); only if the run ends with an error or a timeout are its last lines shown.
    """
    import queue
    import threading

    run_start = time.time()
    q: "queue.Queue[str | None]" = queue.Queue()

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )

    try:
        proc.stdin.write(stdin_text)
        proc.stdin.close()
    except Exception:
        pass

    def _reader():
        try:
            for line in proc.stdout:
                q.put(line)
        finally:
            q.put(None)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    deadline = run_start + timeout
    short_label = run_label.split(" [", 1)[0]   # "run 1/3": the dataset is shown once, in the starting line
    status = "unknown"
    bar = bar and stats is not None
    bar_len = 0
    tail: collections.deque = collections.deque(maxlen=CRASH_TAIL_LINES)

    def clear_bar() -> None:
        nonlocal bar_len
        if bar_len:
            print("\r" + " " * bar_len + "\r", end="", flush=True)
            bar_len = 0

    def draw_bar() -> None:
        nonlocal bar_len
        text = _bar_text(short_label, stats)
        if text:
            print("\r" + text.ljust(bar_len), end="", flush=True)
            bar_len = len(text)

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            _kill_tree(proc)
            proc.wait()
            status = "timeout"
            break
        try:
            line = q.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            break
        if stats is not None and stats.feed(line, time.time()):
            if bar:
                draw_bar()
            continue   # a marker line: only for the summary
        if bar:
            if line.strip():
                tail.append(line)
        elif line.strip():
            print(f"  [{short_label}] {line}", end="")
        if log_file is not None:
            log_file.write(f"[{run_label}] {line}")
            log_file.flush()
    clear_bar()

    if status != "timeout":
        returncode = proc.wait()
        status = "ok" if returncode == 0 else f"exit_code_{returncode}"

    if bar and status != "ok":
        print(f"  [{short_label}] last {len(tail)} line(s) of output:")
        for line in tail:
            print(f"  [{short_label}]   {line}", end="")

    elapsed = time.time() - run_start
    return status, elapsed


def main():
    parser = argparse.ArgumentParser(description="Batch runner for mutation_loop.py (round-robin)")
    parser.add_argument("--runs", type=int, default=None,
                         help=f"Number of runs (default: {DEFAULT_RUNS}, or as many as --max-minutes allows)")
    parser.add_argument("--max-minutes", type=float, default=None,
                         help="Time budget: do not start a new run after this many minutes (the run in progress "
                              "finishes). Combine with --runs to stop at whichever comes first")
    parser.add_argument("--timeout", type=int, default=900,
                         help="Per-run timeout in seconds (default: 900 = 15 min)")
    parser.add_argument("--module", default="tests.mutation_testing.loop.mutation_loop",
                         help="Module to invoke with -m")
    parser.add_argument("--datasets", type=str, default=None,
                         help="Comma-separated subset of the schemas listed in \"_schema_options\" "
                              "of run_config.json to cycle over (default: all of them)")
    parser.add_argument("--profiles", type=str, default=None,
                         help="Comma-separated action_weights_profile names. The profile changes once per lap "
                              "(after every dataset has run once), so a dataset is not tied to one profile. "
                              "Default: the profile in run_config.json for every run")
    parser.add_argument("--steps", type=int, default=None,
                         help="Number of mutation steps in every run (default: \"num_steps\" in run_config.json)")
    parser.add_argument("--run-config", type=str, default=None,
                         help="Explicit path to run_config.json (skips the two-candidate guess)")
    parser.add_argument("--archive-first", action="store_true",
                         help="Move the recorded traces into a timestamped archive before the batch starts")
    parser.add_argument("--verbose", action="store_true",
                         help="Print the complete output of every run (default: only invariant failures, repairs "
                              "and progress; the complete output is always kept in tests/output/runs/<run_id>/console.log)")
    parser.add_argument("--keep-incomplete", action="store_true",
                         help="Do not remove the snapshots of runs that never finished (no history.json) "
                              "from tests/output/traces/ when the batch ends")
    args = parser.parse_args()
    if args.max_minutes is not None and args.max_minutes <= 0:
        parser.error("--max-minutes must be greater than 0")
    if args.steps is not None and args.steps <= 0:
        parser.error("--steps must be greater than 0")
    if args.runs is None:
        args.runs = RUNS_CAP_WITH_TIME_BUDGET if args.max_minutes else DEFAULT_RUNS

    if args.archive_first:
        from tests.mutation_testing.traces.trace_store import archive_traces
        archive_traces(TRACES_DIR.as_posix(), "before-batch")

    # The schemas and profiles come from run_config.json itself, so it is the only list to keep up to date.
    run_config_path = _resolve_run_config_path(args.run_config)
    original_config = _load_run_config(run_config_path)
    known_schemas = configured_schemas(original_config)
    known_profiles = configured_profiles(original_config)

    dataset_names = [d.strip() for d in args.datasets.split(",")] if args.datasets else known_schemas
    if not dataset_names:
        parser.error('no schemas to run: list them in "_schema_options" of run_config.json or pass --datasets')
    for d in dataset_names:
        if d not in known_schemas:
            print(f"'{d}' is not listed in \"_schema_options\" of run_config.json - check spelling "
                  f"(known: {known_schemas}).")

    profile_names = [p.strip() for p in args.profiles.split(",") if p.strip()] if args.profiles else []
    for p in profile_names:
        if p not in known_profiles:
            print(f"'{p}' is not listed in \"_action_weights_profile_options\" of run_config.json - "
                  f"check spelling (known: {known_profiles}).")

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []  # (run_num, dataset, status, elapsed_seconds)

    dataset_cycle = _build_dataset_cycle(args.runs, dataset_names)
    # One profile per lap: runs 1..n_ds get the first profile, the next n_ds runs the second, and so on.
    n_ds = len(dataset_names)
    args.profile_cycle = [
        profile_names[(k // n_ds) % len(profile_names)] if profile_names else None
        for k in range(args.runs)
    ]

    print(f"Batch starting: {_plan_text(args)}, {args.timeout}s timeout each"
          + (", verbose console" if args.verbose else ", quiet console (use --verbose for everything)"))
    print(f"Round-robin datasets: {dataset_names}  (order shuffled in each lap)")
    if profile_names:
        print(f"Action-weight profiles, one per lap: {profile_names}")
    print()

    try:
        _set_keep_awake(True)
        print("[keep-awake] Sleep prevention active for this process "
              "(still set your Windows power settings too).\n")

        batch_start = time.time()

        _run_batch(args, results, batch_start, batch_id,
                   dataset_cycle, original_config, run_config_path)
        if not args.keep_incomplete:
            _drop_incomplete_runs()
    finally:
        _set_keep_awake(False)
        print("[keep-awake] Released - normal sleep behavior restored.")
        _write_run_config(run_config_path, original_config)
        print(f"[config] {run_config_path} restored to its pre-batch content.")


def _drop_incomplete_runs() -> None:
    """Runs that crashed or timed out leave snapshots but no history.json. Their traces would mislead the
    inference engine, so they are moved to tests/output/traces/archive/_dropped/ (not deleted)."""
    try:
        from tests.mutation_testing.traces.trace_store import drop_runs, incomplete_run_ids
        ids = incomplete_run_ids(TRACES_DIR.as_posix())
        if ids:
            print(f"\n[Traces] {len(ids)} unfinished run(s) found: {', '.join(sorted(ids))}")
            drop_runs(TRACES_DIR.as_posix(), None, ids)
    except Exception as e:  # clean-up must never hide the batch result
        print(f"[Traces] could not check for unfinished runs: {e!r}")


def _invariant_id(entry) -> str:
    text = entry.get("id") if isinstance(entry, dict) else str(entry)
    return (text or "").split(" ", 1)[0]


def _read_history_steps(run_id: str | None) -> list[dict] | None:
    """The steps of a finished run's history.json, or None if the run never wrote one (timeout, crash)."""
    if not run_id:
        return None
    try:
        from tests.mutation_testing.runs.run_layout import find_run_dir
        folder = find_run_dir(run_id)
        path = folder / "history.json" if folder else None
        if path is None or not path.is_file():
            return None
        from tests.mutation_testing.runs.repair_view import expand_repairs
        data = json.loads(path.read_text(encoding="utf-8"))
        return expand_repairs(data["steps"] if isinstance(data, dict) else data)   # repair clicks count as steps here
    except Exception as e:  # the summary must never break the batch
        print(f"[summary] could not read the history of {run_id}: {e!r}")
        return None


# run_status.py status -> the batch summary's result word
_RESULT_OF_STATUS = {"clean": "CLEAN", "known-bugs": "KNOWN BUGS", "failures": "FAILURES", "diverged-failures": "FAILURES",
                     "diverged-clean": "CLEAN", "incomplete": "INCOMPLETE"}


def _summarise_run(run_num: int, dataset: str, profile: str | None, status: str, elapsed: float,
                   stats: "_StreamStats") -> dict:
    """One run as a dict for the batch summary. Exact numbers come from history.json (same rule as run_status.py:
    a step failed when passed is False and its action succeeded); a run without one only has what the console showed."""
    seconds = stats.step_seconds(time.time())
    slowest = sorted(seconds.items(), key=lambda kv: kv[1], reverse=True)[:SLOWEST_STEPS_SHOWN]
    info = {
        "run": run_num, "dataset": dataset, "profile": profile, "process_status": status,
        "seconds": round(elapsed), "run_id": stats.run_id,
        "steps_reached": max(stats.step_started, default=0), "steps_planned": stats.steps_total,
        "avg_step_seconds": round(sum(seconds.values()) / len(seconds), 1) if seconds else None,
        "slowest_steps": [{"step": s, "seconds": round(t), "action": stats.step_action.get(s, "")} for s, t in slowest],
    }
    steps = _read_history_steps(stats.run_id)
    if steps is not None:
        # The history has one record per loop step plus one per repair/recovery click. The verdict comes from the same
        # function that writes the run's status file (run_status.summarise_steps): only the loop's own steps decide it,
        # known bugs are counted apart, and repair clicks that left a rule failing are reported but never fail the run.
        from tests.mutation_testing.runs.run_status import summarise_steps
        verdict = summarise_steps(steps)
        info.update(steps_recorded=verdict["loop_steps"], repair_clicks=verdict["repair_clicks"],
                    failing_steps=verdict["failing_steps"], known_bug_steps=verdict["known_bug_steps"],
                    repair_failing_clicks=verdict["repair_failing_clicks"],
                    invariants_failed=dict(sorted(verdict["_failing_by_invariant"].items())),
                    failed_actions=verdict["failed_actions"], run_status=verdict["status"],
                    schema_invalid_episodes=sum(1 for s in steps if s.get("schema_became_invalid")))
    else:
        info["failed_checks_seen_in_console"] = stats.failed_checks
    if status == "timeout":
        info["result"] = "TIMEOUT"
    elif steps is None:
        info["result"] = "CRASHED" if status != "ok" else "NO HISTORY"
    else:
        info["result"] = _RESULT_OF_STATUS.get(info["run_status"], "FAILURES")
    return info


def _list_invariants(counts: dict) -> str:
    return ", ".join(f"{name} x{n}" for name, n in counts.items()) if counts else "none"


def _summary_text(data: dict) -> str:
    runs = data["runs"]
    counts = Counter(r["result"] for r in runs)
    totals = ", ".join(f"{counts[k]} {k.lower()}" for k in ("CLEAN", "KNOWN BUGS", "FAILURES", "INCOMPLETE", "TIMEOUT", "CRASHED", "NO HISTORY") if counts[k])
    lines = [f"Batch {data['batch_id']}: {len(runs)} of {data['runs_planned']} run(s) done in {data['total_seconds']} s "
             f"(timeout {data['timeout_seconds']} s per run)",
             f"Result: {totals or 'no run has finished yet'}"]

    runs_with, steps_with = Counter(), Counter()
    for r in runs:
        for name, n in (r.get("invariants_failed") or {}).items():
            runs_with[name] += 1
            steps_with[name] += n
    if runs_with:
        lines.append("Invariants failed: " + ", ".join(f"{name} ({runs_with[name]} run(s), {steps_with[name]} step(s))"
                                                       for name in sorted(runs_with)))
    lines.append("")
    for r in runs:
        profile = f" ({r['profile']})" if r["profile"] else ""
        planned = f"/{r['steps_planned']}" if r["steps_planned"] else ""
        avg = f", avg {r['avg_step_seconds']} s/step" if r["avg_step_seconds"] is not None else ""
        lines.append(f"Run {r['run']}  {r['dataset']}{profile}  {r['result']}  {r['seconds']} s  "
                     f"step {r['steps_reached']}{planned}{avg}")
        lines.append(f"    run folder: {r['run_id'] or 'unknown'}")
        if "failing_steps" in r:
            lines.append(f"    failing steps: {r['failing_steps']} of {r['steps_recorded']} (+{r['repair_clicks']} repair clicks, "
                         f"{r['repair_failing_clicks']} of them left a rule failing)  |  known-bug steps: {r['known_bug_steps']}  |  invariants failed: "
                         f"{_list_invariants(r['invariants_failed'])}  |  failed actions: {r['failed_actions']}  |  "
                         f"invalid-schema episodes: {r['schema_invalid_episodes']}")
        else:
            lines.append(f"    no history.json written; failed checks seen in the console: "
                         f"{r['failed_checks_seen_in_console']} (approximate)")
        if r["slowest_steps"]:
            lines.append("    slowest steps: " + "; ".join(
                f"{s['step']} ({s['seconds']} s{': ' + s['action'] if s['action'] else ''})" for s in r["slowest_steps"]))
    return "\n".join(lines) + "\n"


def _write_batch_summary(batch_id: str, args, summaries: list[dict], total_seconds: float) -> Path:
    """Rewritten after every run, so an interrupted batch still leaves its summary. Returns the .txt path."""
    stem = BATCH_DIR / f"batch_{batch_id}"
    data = {"batch_id": batch_id, "runs_planned": _runs_label(args), "timeout_seconds": args.timeout,
            "max_minutes": args.max_minutes,
            "total_seconds": round(total_seconds), "runs": summaries}
    stem.with_suffix(".json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    text_path = stem.with_suffix(".txt")
    text_path.write_text(_summary_text(data), encoding="utf-8")
    return text_path


def _run_batch(args, results, batch_start, batch_id,
               dataset_cycle, original_config, run_config_path):
    summaries: list[dict] = []
    summary_path = None
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    log_path = BATCH_DIR / f"batch_{batch_id}.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        for i in range(1, args.runs + 1):
            if args.max_minutes and time.time() - batch_start >= args.max_minutes * 60:
                stop_line = (f"[{datetime.now().strftime('%H:%M:%S')}] Time budget of {args.max_minutes:g} min reached - "
                             f"no new run is started ({i - 1} done).")
                print(stop_line)
                log_file.write(stop_line + "\n")
                break
            dataset = dataset_cycle[i - 1]
            profile = args.profile_cycle[i - 1]
            _set_config_schema(run_config_path, dataset, original_config, profile, args.steps)

            profile_tag = f" ({profile})" if profile else ""
            run_label = f"run {i}/{_runs_label(args)} [{dataset}]{profile_tag}"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {run_label} starting...")
            log_file.write(f"[{datetime.now().strftime('%H:%M:%S')}] {run_label} starting...\n")

            stats = _StreamStats()
            try:
                status, elapsed = _run_and_stream(
                    cmd=[sys.executable, "-m", args.module],
                    env=_child_env(args.verbose),
                    stdin_text="\n",
                    timeout=args.timeout,
                    run_label=run_label,
                    stats=stats,
                    log_file=log_file,
                    bar=not args.verbose and sys.stdout.isatty(),   # a redirected or verbose console keeps plain lines
                )
            except Exception as e:
                elapsed = time.time() - batch_start
                print(f"  [{run_label}] EXCEPTION: {e!r}")
                status = f"exception_{type(e).__name__}"

            results.append((i, dataset, status, elapsed))
            info = None
            try:
                info = _summarise_run(i, dataset, profile, status, elapsed, stats)
                summaries.append(info)
                summary_path = _write_batch_summary(batch_id, args, summaries, time.time() - batch_start)
            except Exception as e:  # the summary must never stop the batch
                print(f"[summary] could not write the batch summary: {e!r}")

            # Which invariants failed, so the run worth opening is visible without the failure blocks
            outcome = ""
            if info is not None:
                outcome = f"  -> {info['result']}"
                if info.get("invariants_failed"):
                    outcome += f" ({_list_invariants(info['invariants_failed'])})"
            finished_line = (f"[{datetime.now().strftime('%H:%M:%S')}] {run_label} finished: "
                             f"{status}  ({elapsed:.0f}s){outcome}")
            print(finished_line)
            log_file.write(finished_line + "\n")
            log_file.flush()

        total_elapsed = time.time() - batch_start

        def say(text: str = "") -> None:
            """To the console and to the batch log."""
            print(text)
            log_file.write(text + "\n")
            log_file.flush()

        by_run = {r["run"]: r for r in summaries}
        ok_count = sum(1 for _, _, s, _ in results if s == "ok")
        say(f"\nBatch done: {ok_count}/{len(results)} runs OK, {total_elapsed:.0f}s total.")
        per_ds: dict[str, list] = {}
        for _, ds, st, _ in results:
            per_ds.setdefault(ds, []).append(st)
        say("Per dataset:")
        for ds, statuses in per_ds.items():
            say(f"  {ds}: {len(statuses)} attempted, {sum(1 for x in statuses if x == 'ok')} ok")
        say("Per run:")
        for run_num, ds, st, e in results:
            prof = args.profile_cycle[run_num - 1]
            outcome = by_run.get(run_num, {}).get("result")
            say(f"  run_{run_num:03d} [{ds}]{f' ({prof})' if prof else ''}: {st}  ({e:.0f}s)"
                + (f"  -> {outcome}" if outcome else ""))

        if summary_path is not None:
            data = json.loads(summary_path.with_suffix('.json').read_text(encoding="utf-8"))
            say(f"\nBatch summary: {summary_path.resolve()}")
            say(f"Full log:      {log_path.resolve()}")
            say("Per-run output: tests/output/runs/<run folder>/console.log")
            say("")
            say(_summary_text(data).split("\n\n")[0])
            if not any(s["result"] in ("CLEAN", "KNOWN BUGS", "FAILURES", "INCOMPLETE") for s in summaries):
                say("No run finished and wrote a history.json, so the report has nothing to show.")


if __name__ == "__main__":
    main()