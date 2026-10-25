"""Labels a person puts on a problem of the issue report, so later reports only show what still needs attention.

A problem is the report's grouped failure; its id (P-xxxxxx, "Technical details") is stable, so a label is kept per id.
Labels live in config/problem_labels.json next to triage_config.json:

    {"labels": {"P-1a2b3c": {"label": "irrelevant", "note": "...", "title": "...", "labelled_at": "2026-10-24 15:30"}}}

    tool-attention   a problem of the test tool that still needs fixing: listed apart from the app problems
    tool-resolved    a problem of the test tool that was fixed: hidden for runs up to labelled_at; if a newer run shows it
                     again it is listed, with only those newer runs
    irrelevant       not worth following: hidden, and not counted in the "seen in other runs" of other problems

(An app bug is logged with "Log as a bug" on the problem; that goes to the known bugs, not here.)

The report page collects labels and saves them in the same known_bugs_update_<time>.json file as bug entries, under the
key "label" (a record with label "" removes the label). The terminal applies it (`python -m tests.cli bugs import`,
or Enter in the terminal that built the report).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.reports.triage.run_loading import DEFAULT_CONFIG_PATH

LABELS_FILE = DEFAULT_CONFIG_PATH.parent / "problem_labels.json"
TIME_FORMAT = "%Y-%m-%d %H:%M"
LABELS = {
    "tool-attention": "Test-tool problem, needs attention",
    "tool-resolved": "Test-tool problem, resolved",
    "irrelevant": "Irrelevant",
}


def load_labels(path: Path = LABELS_FILE) -> dict[str, dict]:
    """{problem id: record}; empty if there is no file or it cannot be read."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    labels = data.get("labels") if isinstance(data, dict) else None
    return {k: v for k, v in (labels or {}).items() if isinstance(v, dict) and v.get("label") in LABELS}


def labelled_at(record: dict) -> datetime | None:
    try:
        return datetime.strptime(record.get("labelled_at", ""), TIME_FORMAT)
    except ValueError:
        return None


def hides(record: dict | None, started: datetime | None) -> bool:
    """True if a run that started at `started` should not count for a problem with this label."""
    if not record:
        return False
    if record["label"] == "irrelevant":
        return True
    if record["label"] == "tool-resolved":
        at = labelled_at(record)
        return bool(at and started and started <= at)
    return False


def check(records: list[dict]) -> list[str]:
    """Problems found in a list of label records from an update file (empty list: all fine)."""
    errors = []
    for r in records:
        uid = str(r.get("uid", "")).strip()
        if not uid:
            errors.append("a label needs the problem id (uid)")
        elif r.get("label", "") not in ("", *LABELS):
            errors.append(f"{uid}: unknown label '{r.get('label')}' (use {', '.join(LABELS)}, or empty to remove it)")
    return errors


def apply_labels(records: list[dict], path: Path = LABELS_FILE) -> list[str]:
    """Set or remove the labels of the records (see check) and save the file. Returns one line per change."""
    labels = load_labels(path)
    now = datetime.now().strftime(TIME_FORMAT)
    done = []
    for r in records:
        uid, label = str(r["uid"]).strip(), r.get("label", "")
        if not label:
            if labels.pop(uid, None) is not None:
                done.append(f"{uid}: label removed")
            continue
        labels[uid] = {"label": label, "note": str(r.get("note", "")).strip(), "title": str(r.get("title", "")).strip(),
                       "labelled_at": now}
        done.append(f"{uid}: labelled '{LABELS[label]}'")
    Path(path).write_text(json.dumps({"labels": labels}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return done
