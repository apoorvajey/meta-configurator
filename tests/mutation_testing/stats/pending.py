"""Rules that were added in a rules review but have not had a chance to run yet.

A rule is added when it is first accepted in a review file (rule_reviews/rule_review_<time>.json; a learned-bug check is
added when its learned bug is first marked "check"). It has had its chance once a run started after that review. Until
then its zero counts say nothing, so the stats reports list such rules in a section of their own.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.inference.learned_bug import check_wanted
from tests.mutation_testing.inference.review_store import REVIEW_DIR, review_files
from tests.mutation_testing.runs.run_layout import RUN_ID_FORMAT, run_id_of

_STAMP = re.compile(r"(\d{8}_\d{6})")


def added_in_review(rules: list[dict], review_dir: str = REVIEW_DIR) -> dict[str, tuple[datetime, str]]:
    """rule id -> (time, file name) of the first review that accepted it. Rules no review accepts are left out."""
    reviews = []
    for f in review_files(review_dir):   # oldest first
        try:
            when = datetime.strptime(_STAMP.search(f.name).group(1), RUN_ID_FORMAT)
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(data, dict):
            reviews.append((when, f.name, data))
    added = {}
    for r in rules:
        original = r.get("negates")
        for when, name, data in reviews:
            entry = data.get(r["id"])
            if (isinstance(entry, dict) and entry.get("status") == "accepted") or (original and check_wanted(original, data)):
                added[r["id"]] = (when, name)
                break
    return added


def pending_rules(rules: list[dict], run_folders: list[Path], review_dir: str = REVIEW_DIR) -> dict[str, dict]:
    """rule id -> {"added": datetime, "review": file name} for the rules added after the newest run started."""
    started = [datetime.strptime(run_id_of(d.name), RUN_ID_FORMAT) for d in run_folders if run_id_of(d.name)]
    last_run = max(started, default=None)
    return {rid: {"added": when, "review": name} for rid, (when, name) in added_in_review(rules, review_dir).items()
            if last_run is None or last_run < when}
