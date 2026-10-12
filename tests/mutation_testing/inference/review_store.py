"""Where the rule reviews live: one file per save from the review page, the newest wins
(config/rule_reviews/rule_review_<YYYYMMDD_HHMMSS>.json).

The review page names its download with the download time; put the file into the folder (or run
`python -m tests.cli review import`). Nothing is overwritten or moved, so the folder is the history of the
reviews, and every tool (engine, review page) reads the newest file. The older name
rule_review.<YYYYMMDD_HHMMSS>.json is recognised too.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

REVIEW_DIR = "tests/mutation_testing/config/rule_reviews"
STAMP_FORMAT = "%Y%m%d_%H%M%S"
FILE_PREFIX = "rule_review"
# rule_review_20261012_005607.json, rule_review.20261012_005607.json, and the browser's copies "... (1).json"
_NAME_RE = re.compile(r"^rule_review[._](\d{8}_\d{6})(?: ?\(\d+\)|-\d+)?\.json$")


def review_files(folder: str = REVIEW_DIR) -> list[Path]:
    """The review files in the folder, oldest first (by the time in the name)."""
    root = Path(folder)
    if not root.is_dir():
        return []
    found = [(m.group(1), p.name, p) for p in root.iterdir() if p.is_file() and (m := _NAME_RE.match(p.name))]
    return [p for _, _, p in sorted(found)]


def latest_review_file(folder: str = REVIEW_DIR) -> Path | None:
    files = review_files(folder)
    return files[-1] if files else None


def load_review(folder: str = REVIEW_DIR, announce: bool = True) -> dict:
    """The decisions of the newest review file; {} if the folder has none."""
    latest = latest_review_file(folder)
    if latest is None:
        if announce:
            print(f"[Review] no review file in {folder} yet - nothing is accepted")
        return {}
    if announce:
        print(f"[Review] using {latest}")
    return json.loads(latest.read_text(encoding="utf-8"))


def _free_name(folder: Path, base: str) -> Path:
    target, n = folder / f"{base}.json", 2
    while target.exists():
        target, n = folder / f"{base}-{n}.json", n + 1
    return target


def save_review(data: dict, folder: str = REVIEW_DIR) -> Path:
    """Write `data` as a new review file stamped with the current time."""
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    target = _free_name(root, f"{FILE_PREFIX}_{datetime.now():%Y%m%d_%H%M%S}")
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def default_download_dir() -> str:
    """Where the browser puts downloads (the user's Downloads folder)."""
    return str(Path.home() / "Downloads")


def install_review(source: str, folder: str = REVIEW_DIR) -> tuple[Path, bool]:
    """Copy a downloaded review file into the folder: under its own name if that already carries a time, else stamped now.
    Returns (file in the folder, copied). Nothing is copied if the file is already there (same name and content)."""
    src, root = Path(source), Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    if src.resolve().parent == root.resolve() and _NAME_RE.match(src.name):
        return src, False
    same_name = root / src.name
    if _NAME_RE.match(src.name) and same_name.exists():
        if same_name.read_bytes() == src.read_bytes():
            return same_name, False
        target = _free_name(root, f"{FILE_PREFIX}_{datetime.now():%Y%m%d_%H%M%S}")
    elif _NAME_RE.match(src.name):
        target = same_name
    else:
        target = _free_name(root, f"{FILE_PREFIX}_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(src, target)
    return target, True