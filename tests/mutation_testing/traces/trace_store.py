"""Maintenance of the recorded snapshot traces (tests/output/traces/{dataset}.jsonl): archive, replace, drop, list.

Run `archive` after a major code change and before a batch, so the inference engine only sees traces written by the
current code; it reads only the top-level *.jsonl files, so archived traces are ignored. Every archive, replacement
and removal is appended to <traces_dir>/corpus_log.log (--log prints it); each archive folder gets a manifest.json
(time, label, per-file snapshot and run counts).

replace_run() is used by `replay --update-traces`: it swaps the snapshots of one run for the replayed ones and stamps
each with "replayed_at".

--drop-before / --drop-runs remove the snapshots of runs recorded before a tool change (run ids are timestamps, so
"--drop-before 20261009_120000" means every run started before that moment). Snapshots stamped "replayed_at" were
re-recorded by the current code and are kept. Removed snapshots go to <traces_dir>/archive/_dropped/, not deleted;
add --dry-run to preview.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.common.paths import RUNS_DIR as _RUNS_DIR, TRACES_DIR as _TRACES_DIR

TRACES_DIR = _TRACES_DIR.as_posix()
ARCHIVE_SUBDIR = "archive"
REPLACED_SUBDIR = "_replaced"  # inside the archive folder: copies of files before a run was replaced
KEEP_BACKUPS = 3               # newest backups kept per trace file
_BACKED_UP: set[str] = set()   # trace files already backed up by this process (a batch replay backs up once)
CORPUS_LOG = "corpus_log.log"   # append-only record of what was done to the traces
DROPPED_SUBDIR = "_dropped"    # inside the archive folder: snapshots removed by --drop-before / --drop-runs


def _log(root: Path, event: str, **fields) -> None:
    """Append one event to <traces_dir>/corpus_log.log."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / CORPUS_LOG).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}) + "\n")
    except OSError as e:
        print(f"[Traces] could not write the corpus log: {e}")


def log_event(traces_dir: str, event: str, **fields) -> None:
    """Public form of _log() for other tools (replay comparison, ...)."""
    _log(Path(traces_dir), event, **fields)


def show_log(traces_dir: str = TRACES_DIR) -> None:
    path = Path(traces_dir) / CORPUS_LOG
    if not path.exists():
        print(f"[Traces] no corpus log yet ({path})")
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        rest = ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("at", "event"))
        print(f"{e['at']}  {e['event']:<8} {rest}")


def _scan(path: Path) -> tuple[int, set[str]]:
    """(number of lines, run ids) of one trace file; unreadable lines are counted but have no run id."""
    lines, runs = 0, set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            lines += 1
            try:
                run_id = json.loads(line).get("run_id")
            except Exception:
                continue
            if run_id:
                runs.add(str(run_id))
    return lines, runs


def archive_traces(traces_dir: str = TRACES_DIR, label: str | None = None, dry_run: bool = False) -> Path | None:
    """Move every top-level *.jsonl into <traces_dir>/archive/<timestamp>[_label]/ and write a manifest."""
    root = Path(traces_dir)
    files = sorted(root.glob("*.jsonl")) if root.exists() else []
    if not files:
        print(f"[Traces] nothing to archive in {root}")
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{label}" if label else "")
    dest = root / ARCHIVE_SUBDIR / stamp
    manifest = {
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "files": {},
    }
    for p in files:
        lines, runs = _scan(p)
        manifest["files"][p.name] = {"snapshots": lines, "runs": len(runs), "run_ids": sorted(runs)}
        print(f"[Traces] {p.name}: {lines} snapshot(s) from {len(runs)} run(s)")
    if dry_run:
        print(f"[Traces] dry run - would move {len(files)} file(s) to {dest}")
        return None
    dest.mkdir(parents=True, exist_ok=False)
    for p in files:
        shutil.move(str(p), str(dest / p.name))
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _log(root, "archive", label=label, to=str(dest),
         files={n: {"snapshots": v["snapshots"], "runs": v["runs"]} for n, v in manifest["files"].items()})
    print(f"[Traces] archived {len(files)} file(s) -> {dest}")
    return dest


def _backup(path: Path, backup_dir: Path) -> None:
    """Gzip a trace file into backup_dir and keep only the newest KEEP_BACKUPS backups of that file."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{path.name}.gz"
    with path.open("rb") as src, gzip.open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    old = sorted(backup_dir.glob(f"*_{path.name}.gz"))
    for stale in old[:-KEEP_BACKUPS]:
        stale.unlink()


def replace_run(dataset: str, run_id: str, snapshots: list[dict], traces_dir: str = TRACES_DIR) -> int:
    """Drop every snapshot of run_id from {dataset}.jsonl and append the replayed ones; returns how many were dropped.

    The file is backed up (gzipped, once per process, newest 3 kept) to <traces_dir>/archive/_replaced/ first and rewritten
    through a temp file, so a crash cannot leave a half-written trace. Lines that cannot be parsed are kept untouched.
    """
    root = Path(traces_dir)
    path = root / f"{dataset}.jsonl"
    root.mkdir(parents=True, exist_ok=True)
    kept, dropped = [], 0
    if path.exists():
        if str(path) not in _BACKED_UP:
            _backup(path, root / ARCHIVE_SUBDIR / REPLACED_SUBDIR)
            _BACKED_UP.add(str(path))
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    same = str(json.loads(line).get("run_id")) == str(run_id)
                except Exception:
                    same = False
                if same:
                    dropped += 1
                else:
                    kept.append(line.rstrip("\n"))
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for line in kept:
            f.write(line + "\n")
        stamp = datetime.now().isoformat(timespec="seconds")
        for snap in snapshots:
            f.write(json.dumps({**snap, "replayed_at": stamp}, default=str) + "\n")
    os.replace(tmp, path)
    print(f"[Traces] {dataset}.jsonl: replaced {dropped} snapshot(s) of run {run_id} with {len(snapshots)} replayed one(s)")
    _log(root, "replace", dataset=dataset, run_id=run_id, removed=dropped, added=len(snapshots))
    return dropped


def list_runs(traces_dir: str = TRACES_DIR) -> None:
    """One line per run in every top-level trace file: snapshots, last step, and whether it was re-recorded by a replay."""
    root = Path(traces_dir)
    files = sorted(root.glob("*.jsonl")) if root.exists() else []
    if not files:
        print(f"[Traces] no trace files in {root}")
        return
    for p in files:
        runs: dict[str, dict] = {}
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    snap = json.loads(line)
                except Exception:
                    continue
                r = runs.setdefault(str(snap.get("run_id")), {"n": 0, "last": 0, "replayed": False})
                r["n"] += 1
                r["last"] = max(r["last"], snap.get("step") or 0)
                r["replayed"] = r["replayed"] or bool(snap.get("replayed_at"))
        print(f"\n{p.name}: {sum(r['n'] for r in runs.values())} snapshot(s), {len(runs)} run(s)")
        for run_id in sorted(runs):
            r = runs[run_id]
            print(f"  {run_id}  {r['n']:>4} snapshots, last step {r['last']:>3}" + ("  [replayed]" if r["replayed"] else ""))


def drop_runs(traces_dir: str = TRACES_DIR, before: str | None = None, run_ids: set[str] | None = None,
              dry_run: bool = False) -> int:
    """Remove the snapshots of runs older than `before` (a run id) and/or listed in run_ids; returns how many.

    Snapshots stamped "replayed_at" are kept: the current code re-recorded them. Removed lines go to
    <traces_dir>/archive/_dropped/<timestamp>_<file>.jsonl.
    """
    root = Path(traces_dir)
    files = sorted(root.glob("*.jsonl")) if root.exists() else []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    total = 0
    for p in files:
        kept, dropped, dropped_runs = [], [], set()
        with p.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    snap = json.loads(line)
                except Exception:
                    kept.append(line.rstrip("\n"))
                    continue
                rid = str(snap.get("run_id"))
                old = (before is not None and rid < before) or (run_ids is not None and rid in run_ids)
                if old and not snap.get("replayed_at"):
                    dropped.append(line.rstrip("\n"))
                    dropped_runs.add(rid)
                else:
                    kept.append(line.rstrip("\n"))
        if not dropped:
            continue
        total += len(dropped)
        print(f"[Traces] {p.name}: {'would drop' if dry_run else 'dropping'} {len(dropped)} snapshot(s) "
              f"from {len(dropped_runs)} run(s), keeping {len(kept)}")
        if dry_run:
            continue
        out_dir = root / ARCHIVE_SUBDIR / DROPPED_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{stamp}_{p.name}").write_text("\n".join(dropped) + "\n", encoding="utf-8")
        tmp = p.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
        os.replace(tmp, p)
        _log(root, "drop", dataset=p.stem, snapshots=len(dropped), runs=sorted(dropped_runs),
             rule=(f"before {before}" if before else "") + (" " if before and run_ids else "") + ("run ids" if run_ids else ""))
    if total == 0:
        print("[Traces] nothing matched - no snapshots dropped")
    return total


def incomplete_run_ids(traces_dir: str = TRACES_DIR, history_dir: str = _RUNS_DIR.as_posix()) -> set[str]:
    """Run ids that have snapshots in the traces but no history.json: the run crashed or was stopped before it
    finished. Runs re-recorded by replay are not reported (drop_runs keeps them anyway)."""
    from tests.mutation_testing.runs.run_layout import find_run_dir
    root, hist = Path(traces_dir), Path(history_dir)
    found: set[str] = set()
    for p in sorted(root.glob("*.jsonl")) if root.exists() else []:
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    snap = json.loads(line)
                except Exception:
                    continue
                rid = str(snap.get("run_id"))
                if rid in found or snap.get("replayed_at"):
                    continue
                run_dir = find_run_dir(rid, hist)
                if run_dir is None or not (run_dir / "history.json").is_file():
                    found.add(rid)
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive or prune the recorded snapshot traces")
    parser.add_argument("--traces-dir", default=TRACES_DIR)
    parser.add_argument("--label", default=None, help="Short note added to the archive folder name")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, change nothing")
    parser.add_argument("--list", action="store_true", help="List the runs in each trace file and exit")
    parser.add_argument("--log", action="store_true", help="Print the corpus log (archives, replacements, removals) and exit")
    parser.add_argument("--drop-before", default=None, metavar="RUN_ID",
                        help="Remove runs started before this run id (e.g. 20261009_120000); replayed runs are kept")
    parser.add_argument("--drop-runs", default=None, metavar="ID,ID",
                        help="Remove these runs (comma-separated run ids); replayed runs are kept")
    parser.add_argument("--drop-incomplete", action="store_true",
                        help="Remove runs that never finished (snapshots but no history.json); see --history-dir")
    parser.add_argument("--history-dir", default=_RUNS_DIR.as_posix(), help="Where the runs are (tests/output/runs/<run_id>/history.json)")
    args = parser.parse_args()
    if args.drop_incomplete:
        ids = incomplete_run_ids(args.traces_dir, args.history_dir)
        print(f"[Traces] {len(ids)} unfinished run(s): {', '.join(sorted(ids)) or 'none'}")
        if ids:
            drop_runs(args.traces_dir, None, ids, args.dry_run)
        return
    if args.log:
        show_log(args.traces_dir)
    elif args.list:
        list_runs(args.traces_dir)
    elif args.drop_before or args.drop_runs:
        ids = {r.strip() for r in args.drop_runs.split(",") if r.strip()} if args.drop_runs else None
        drop_runs(args.traces_dir, args.drop_before, ids, args.dry_run)
    else:
        archive_traces(args.traces_dir, args.label, args.dry_run)


if __name__ == "__main__":
    main()