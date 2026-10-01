"""Runs the mutation loop N times in a row, unattended, to collect snapshots overnight.

Runs cycle through DATASET_SCHEMA_NAMES, so each lap covers every dataset once. Before each run the
"schema" key of run_config.json is set to that run's dataset. The original file content is restored
when the batch ends, including on Ctrl+C or a crash. _summary.txt reports attempted and successful
runs per dataset.

The loop writes each snapshot to invariant_miner_traces/<dataset>.jsonl itself.

Before a long batch: archive old snapshots, run from the repo root, turn screenshots off and stop the
machine from sleeping (on Windows the runner also keeps it awake).

    python -m tests.mutation_testing.overnight_runner --runs 24
    python -m tests.mutation_testing.overnight_runner --runs 50 --timeout 1200
    python -m tests.mutation_testing.overnight_runner --runs 24 --shuffle-laps
    python -m tests.mutation_testing.overnight_runner --runs 12 --datasets "Strenda Schema,EnzymeML Schema"
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Not imported from mutation_loop, to avoid its Playwright import. Keep in sync with SCHEMA_DATA_PAIRS there.
DATASET_SCHEMA_NAMES = [
    "Autonomous Vehicle Schema",
    "Feature Testing Schema",
    "Strenda Schema",
    "EnzymeML Schema",
]

RUN_CONFIG_PATH = Path(__file__).parent / "config" / "run_config.json"


def _resolve_run_config_path(explicit: str | None) -> Path:
    """The run_config.json to use for this batch. --run-config wins if given and must exist. Otherwise
    config/run_config.json is used.
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


def _child_env() -> dict:
    """Force UTF-8 in the child process. With piped output on Windows, Python otherwise encodes print()
    output with the system codepage (cp1252).
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
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


def _set_config_schema(path: Path, schema_name: str, base_config: dict) -> None:
    """Write base_config back with "schema" set to schema_name. All other keys are left as they were."""
    patched = dict(base_config)
    patched["schema"] = schema_name
    _write_run_config(path, patched)


def _build_dataset_cycle(n_runs: int, dataset_names: list[str], shuffle_laps: bool) -> list[str]:
    """Dataset for each of n_runs runs, in order.

    Round-robin by default, wrapping around. If n_runs is not a multiple of len(dataset_names), the
    first datasets get one extra run (25 runs over 4 datasets: the first gets 7, the rest 6). The
    summary reports this.

    shuffle_laps=True shuffles the dataset order within each lap, so time-of-night effects such as load
    or throttling do not always hit the same dataset. Off by default.
    """
    cycle = []
    lap = 0
    while len(cycle) < n_runs:
        lap_order = list(dataset_names)
        if shuffle_laps:
            random.shuffle(lap_order)
        cycle.extend(lap_order)
        lap += 1
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


def _run_and_stream(cmd: list, env: dict, stdin_text: str, timeout: int,
                     log_path: Path, run_label: str) -> tuple[str, float]:
    """Run `cmd`, streaming its output to the console and log_path. Output is read in a background thread
    so the timeout is enforced even when the child prints nothing.
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
    status = "unknown"
    with open(log_path, "w", encoding="utf-8") as log_f:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                status = "timeout"
                break
            try:
                line = q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                break
            print(f"  [{run_label}] {line}", end="")
            log_f.write(line)
            log_f.flush()

        if status != "timeout":
            returncode = proc.wait()
            status = "ok" if returncode == 0 else f"exit_code_{returncode}"

    elapsed = time.time() - run_start
    return status, elapsed


def main():
    parser = argparse.ArgumentParser(description="Overnight batch runner for mutation_loop.py (round-robin)")
    parser.add_argument("--runs", type=int, default=15, help="Number of runs (default: 15)")
    parser.add_argument("--timeout", type=int, default=900,
                         help="Per-run timeout in seconds (default: 900 = 15 min)")
    parser.add_argument("--module", default="tests.mutation_testing.mutation_loop",
                         help="Module to invoke with -m")
    parser.add_argument("--datasets", type=str, default=None,
                         help="Comma-separated subset of DATASET_SCHEMA_NAMES to cycle over "
                              "(default: all four)")
    parser.add_argument("--shuffle-laps", action="store_true",
                         help="Shuffle dataset order independently each lap (default: fixed order)")
    parser.add_argument("--run-config", type=str, default=None,
                         help="Explicit path to run_config.json (default: config/run_config.json)")
    args = parser.parse_args()

    dataset_names = (
        [d.strip() for d in args.datasets.split(",")] if args.datasets else DATASET_SCHEMA_NAMES
    )
    for d in dataset_names:
        if d not in DATASET_SCHEMA_NAMES:
            print(f"'{d}' is not in DATASET_SCHEMA_NAMES - check spelling / keep this list "
                  f"in sync with mutation_loop.py's SCHEMA_DATA_PAIRS.")

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path("overnight_logs") / batch_id
    log_dir.mkdir(parents=True, exist_ok=True)

    summary_path = log_dir / "_summary.txt"
    results = []  # (run_num, dataset, status, elapsed_seconds)

    dataset_cycle = _build_dataset_cycle(args.runs, dataset_names, args.shuffle_laps)

    print(f"Overnight batch starting: {args.runs} runs, {args.timeout}s timeout each")
    print(f"Round-robin datasets: {dataset_names}  (shuffle_laps={args.shuffle_laps})")
    print(f"Per-run logs → {log_dir}\n")

    original_config = None
    run_config_path = None
    try:
        run_config_path = _resolve_run_config_path(args.run_config)
        original_config = _load_run_config(run_config_path)

        _set_keep_awake(True)
        print("[keep-awake] Sleep prevention active for this process "
              "(still set your Windows power settings too).\n")

        batch_start = time.time()

        _run_batch(args, log_dir, summary_path, results, batch_start, batch_id,
                   dataset_cycle, original_config, run_config_path)
    finally:
        _set_keep_awake(False)
        print("[keep-awake] Released - normal sleep behavior restored.")
        if run_config_path is not None and original_config is not None:
            _write_run_config(run_config_path, original_config)
            print(f"[config] {run_config_path} restored to its pre-batch content.")


def _run_batch(args, log_dir, summary_path, results, batch_start, batch_id,
               dataset_cycle, original_config, run_config_path):
    for i in range(1, args.runs + 1):
        dataset = dataset_cycle[i - 1]
        _set_config_schema(run_config_path, dataset, original_config)

        run_log_path = log_dir / f"run_{i:03d}.log"
        run_label = f"run {i}/{args.runs} [{dataset}]"
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {run_label} starting...")

        try:
            status, elapsed = _run_and_stream(
                cmd=[sys.executable, "-m", args.module],
                env=_child_env(),
                stdin_text="\n",
                timeout=args.timeout,
                log_path=run_log_path,
                run_label=run_label,
            )
        except Exception as e:
            elapsed = time.time() - batch_start
            run_log_path.write_text(f"=== EXCEPTION ===\n{e!r}\n", encoding="utf-8")
            status = f"exception_{type(e).__name__}"

        results.append((i, dataset, status, elapsed))
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {run_label} finished: "
              f"{status}  ({elapsed:.0f}s)")

        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"Batch {batch_id} - {len(results)}/{args.runs} runs attempted\n")
            f.write(f"Total elapsed so far: {time.time() - batch_start:.0f}s\n\n")
            ok = sum(1 for _, _, s, _ in results if s == "ok")
            f.write(f"OK: {ok}   Not-OK: {len(results) - ok}\n\n")

            f.write("Per-dataset breakdown:\n")
            per_ds: dict[str, list] = {}
            for _, ds, s, _ in results:
                per_ds.setdefault(ds, []).append(s)
            for ds, statuses in per_ds.items():
                ds_ok = sum(1 for s in statuses if s == "ok")
                f.write(f"  {ds}: {len(statuses)} attempted, {ds_ok} ok\n")
            f.write("\n")

            for run_num, ds, s, e in results:
                f.write(f"  run_{run_num:03d} [{ds}]: {s}  ({e:.0f}s)\n")

    total_elapsed = time.time() - batch_start
    ok_count = sum(1 for _, _, s, _ in results if s == "ok")
    print(f"\nBatch done: {ok_count}/{args.runs} runs OK, {total_elapsed:.0f}s total.")
    print(f"Summary: {summary_path}")
    print(f"Per-run logs: {log_dir}")


if __name__ == "__main__":
    main()