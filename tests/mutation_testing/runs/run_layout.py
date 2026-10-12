"""Where the files of a run live, how run folders are named, and which history a report should read
(folder layout: README, Output).

_STATUS__<tag>.json in the run folder is the status as it counts now (none: not finished). The folder is never renamed.
    clean | failures                                       the run as recorded
    replayed-clean | replayed-failures                     a complete replay exists; its result counts
    replayed-diverged-clean | replayed-diverged-failures   the replay left the original run at some step; not a
                                                           step-for-step re-check
    replayed-incomplete                                    some steps were never reached

effective_history: the newest COMPLETE replay, else the original. A replay stopped early or limited with --until
never replaces the original.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from tests.mutation_testing.runs.run_status import MARKERS, write_run_status

from tests.mutation_testing.common.paths import RUNS_DIR   # tests/output/runs
REPLAYS = "replays"
TAG_SEP = "__"
STATUS_PREFIX = "_STATUS__"   # runs/<run_id>/_STATUS__<tag>.json: the status, visible without opening a file
CLEAN_STATUSES = ("clean", "diverged-clean")   # no failing step; diverged-clean left the original run but found nothing wrong
RUN_ID_FORMAT = "%Y%m%d_%H%M%S"
_NAME_RE = re.compile(r"^(\d{8}_\d{6})(?:__(.+))?$")


def run_id_of(name: str) -> str | None:
    """'20261009_153000' or '20261009_153000__replayed-clean' -> '20261009_153000'; None if not a run folder name."""
    m = _NAME_RE.match(name)
    return m.group(1) if m else None


def tag_of(name: str) -> str:
    m = _NAME_RE.match(name)
    return (m.group(2) or "") if m else ""


def iter_run_dirs(root: Path = RUNS_DIR) -> list[Path]:
    """Run folders under root, oldest first (tagged or not)."""
    if not root.is_dir():
        return []
    return sorted((d for d in root.iterdir() if d.is_dir() and run_id_of(d.name)), key=lambda d: d.name)


def find_run_dir(run_id: str, root: Path = RUNS_DIR) -> Path | None:
    """The folder of a run whatever its tag is, so a run id keeps working after the folder was renamed."""
    for d in iter_run_dirs(root):
        if run_id_of(d.name) == run_id:
            return d
    return None


def run_dir_of(history: Path) -> tuple[Path, bool] | None:
    """(run folder, is_replay) for an original or a replay history.json inside a run folder; None for loose files."""
    folder = history.parent
    if folder.parent.name == REPLAYS and run_id_of(folder.parent.parent.name):
        return folder.parent.parent, True
    if run_id_of(folder.name):
        return folder, False
    return None


def original_history(history: Path) -> Path:
    """The recorded run behind a history.json (the file itself unless it is a replay's)."""
    loc = run_dir_of(history)
    return loc[0] / "history.json" if loc and loc[1] else history


@lru_cache(maxsize=512)
def _is_complete_cached(path: str, mtime_ns: int) -> bool:
    try:
        meta = (json.loads(Path(path).read_text(encoding="utf-8")).get("meta") or {})
    except (OSError, ValueError, AttributeError):
        return False
    return bool((meta.get("replay") or {}).get("complete"))


def _is_complete(history: Path) -> bool:
    try:
        return _is_complete_cached(str(history), history.stat().st_mtime_ns)
    except OSError:
        return False


def complete_replays(run_dir: Path) -> list[Path]:
    """history.json of every complete replay of this run, newest first."""
    root = run_dir / REPLAYS
    if not root.is_dir():
        return []
    found = [d / "history.json" for d in sorted(root.iterdir(), reverse=True)
             if d.is_dir() and (d / "history.json").is_file()]
    return [h for h in found if _is_complete(h)]


def effective_history(run_dir: Path) -> Path | None:
    """The history a report should read for this run: newest complete replay, else the original, else None."""
    replays = complete_replays(run_dir)
    if replays:
        return replays[0]
    original = run_dir / "history.json"
    return original if original.is_file() else None


def keep_effective(files: list[Path]) -> list[Path]:
    """Drop every history.json that another history of the same run supersedes (the original when a complete
    replay exists, older replays). Files outside the run layout are kept as they are."""
    kept = []
    for f in files:
        loc = run_dir_of(f)
        if loc is None or effective_history(loc[0]) == f:
            kept.append(f)
    return kept


def resolve_history_path(path: Path) -> Path | None:
    """history.json for a path given to replay: the file, a run folder, or a run folder whose name has changed
    since the path was written (a tag was added or removed)."""
    if path.is_file():
        return path
    if path.is_dir():
        return path / "history.json" if (path / "history.json").is_file() else None
    folder, parent = (path.parent, path.parent.parent) if path.name == "history.json" else (path, path.parent)
    rid = run_id_of(folder.name)
    found = find_run_dir(rid, parent) if rid else None
    return found / "history.json" if found and (found / "history.json").is_file() else None


def status_of(history: Path) -> str | None:
    """'clean' | 'failures' | 'incomplete' from the marker next to a history.json; written first if it is missing."""
    def read() -> str | None:
        return next((k for k, m in MARKERS.items() if (history.parent / m).is_file()), None)
    return read() or (write_run_status(history) and read())


def is_clean(history: Path) -> bool:
    """True if the run behind this history.json is clean as it stands: the original, or the complete replay that
    stands in for it, has no failures and nothing diverged or was skipped."""
    loc = run_dir_of(history.resolve())
    effective = effective_history(loc[0]) if loc else None
    return effective is not None and status_of(effective) in CLEAN_STATUSES


def mark_run_dir(run_dir: Path) -> Path:
    """Show a run's current status inside its folder as `_STATUS__<tag>.json` (tag: clean, failures, incomplete,
    diverged-..., or replayed-... once a complete replay stands in for the run). The folder is never renamed, so a
    link by path or run id stays valid. A folder named <run_id>__<tag> is renamed to <run_id> once, if it can be.
    Never raises: a folder that is open in another program cannot be renamed on Windows."""
    rid = run_id_of(run_dir.name)
    if not rid:
        return run_dir
    if tag_of(run_dir.name):
        bare = run_dir.with_name(rid)
        if bare.exists():
            print(f"[!] Not renaming {run_dir.name}: {bare.name} already exists (see merge_duplicate_runs)")
        else:
            try:
                run_dir.rename(bare)
                run_dir = bare
            except OSError as e:
                print(f"[!] Could not rename {run_dir.name} to {bare.name}: {e}")
    hist = effective_history(run_dir)
    status = status_of(hist) if hist else None
    if status is None:
        return run_dir
    loc = run_dir_of(hist)
    tag = f"{'replayed-' if loc and loc[1] else ''}{status}"
    try:
        for old in run_dir.glob(f"{STATUS_PREFIX}*.json"):
            old.unlink()
        (run_dir / f"{STATUS_PREFIX}{tag}.json").write_text(
            json.dumps({"status": tag, "effective_history": hist.relative_to(run_dir).as_posix()}, indent=2),
            encoding="utf-8")
    except OSError as e:
        print(f"[!] Could not write the status marker in {run_dir.name}: {e}")
    print(f"Run folder: {run_dir}  [{tag}]")
    return run_dir
