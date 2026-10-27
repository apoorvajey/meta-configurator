"""Tells failures caused by a known bug from all other failures, one step at a time.

The known bugs are the `known_bugs` list in config/triage_config.json: a bug names the invariants it makes fail
("invariants") and, optionally, the kinds of action that trigger it ("when_action_kinds"). A failure is treated as
a known bug when its invariant is listed for a bug and, if the bug names action kinds, the failure started under
one of them. The failure stays a failure in history.json; only the way the run is judged and printed changes
(status `known-bugs`, no FAILURE block on the terminal).

KnownBugMatcher keeps one piece of state: for every invariant that is failing, the kinds of action under which the
current streak of failures began. A streak ends when a step that evaluated the invariant passes it. So a bug that
begins with an undo is still the same bug in the steps that follow it, as in the issue report's episodes.

A known bug may carry "only_with": {"<invariant id>": ["<id>", ...]}. That invariant then counts for the bug only
in a report where at least one of the listed invariants fails too; on its own it is an ordinary failure. This is for
coarse invariants that mean this bug only next to its specific ones.

A known bug may carry "do_not_repair": true. The loop then does not start an undo-repair for its failures and does
not count them towards the stuck-run limit (no_repair_ids); everything else about the bug is unchanged.

A known bug may carry "symptom": {"<invariant id>": "<regex>"}. That invariant then counts for the bug only when the
regex finds a match in the failure's own details (the "rows" of its violations, else the whole details as JSON). This is
for coarse invariants whose one rule fits several different problems (e.g. INF-2AB96604 "every GUI row belongs to the
data or the schema"). When the details of a failure are not known (older callers, old histories), the symptom is not
checked and the bug matches on the invariant and the action kinds as before.

The loop calls observe() live (one call per report); run_status and the replay call it over a recorded history.
"""
from __future__ import annotations

import json
import re
from typing import Iterable


def load_known_bugs() -> list[dict]:
    """The `known_bugs` of triage_config.json; an empty list if the file cannot be read."""
    from tests.mutation_testing.reports.triage.run_loading import DEFAULT_CONFIG_PATH, load_config
    try:
        return list(load_config(DEFAULT_CONFIG_PATH).get("known_bugs", []))
    except (OSError, ValueError):
        return []


def bug_label(bug: dict) -> str:
    """How a bug is named in reports: 'BUG-01 · Format selector stale after undo', or just the id if it has no name."""
    name = str(bug.get("name", "")).strip()
    return f"{bug['id']} · {name}" if name else str(bug["id"])


def bug_label_for(bug_id: str) -> str:
    """bug_label for a bug id (looked up in the config); the id itself if the bug is unknown."""
    bug = next((b for b in load_known_bugs() if b.get("id") == bug_id), None)
    return bug_label(bug) if bug else bug_id


def invariant_id(name: str) -> str:
    """'INV-D4 GUI rows ...' -> 'INV-D4'"""
    return (name or "").split(" ", 1)[0]


def failure_details(results: Iterable) -> dict[str, dict]:
    """{invariant id: details} for failed InvariantResults, the form observe() takes as `details`."""
    return {invariant_id(r.name): (getattr(r, "details", None) or {}) for r in results}


def symptom_text(details: dict | None) -> str:
    """The text a bug's "symptom" regex is searched in: the "rows" of every violation, joined with '; ', else the whole
    details as JSON."""
    details = details or {}
    rows = [str(v.get("rows", "")) for v in details.get("violations", []) if isinstance(v, dict) and v.get("rows")]
    return "; ".join(rows) if rows else json.dumps(details, default=str, ensure_ascii=False)


def symptom_matches(bug: dict, inv: str, details: dict | None) -> bool:
    """False only when the bug names a symptom for `inv`, the details are known and the symptom is not in them."""
    pattern = (bug.get("symptom") or {}).get(inv)
    if not pattern or details is None:
        return True
    return re.search(pattern, symptom_text(details)) is not None


class KnownBugMatcher:
    def __init__(self, bugs: list[dict] | None = None):
        self.bugs = load_known_bugs() if bugs is None else bugs
        self._streak_kinds: dict[str, set[str]] = {}   # failing invariant id -> action kinds at the start of its streak
        self._last_user_kind = ""                       # kind of the latest action that is not a repair click
        self._details: dict[str, dict] = {}             # failing invariant id -> details of its latest failure

    def observe(self, action_id: str, failed: Iterable[str], checked: Iterable[str] | None = None,
                details: dict[str, dict] | None = None) -> tuple[dict[str, str], set[str]]:
        """One report of one step.

        action_id: the step's action ('dynamic:...', 'repair:...').
        failed: names or ids of the invariants that failed in this report.
        checked: ids of the invariants this report evaluated; None if unknown (then every invariant that is not
        failing counts as passing).
        details: {invariant id: details of its failure} (see failure_details); None if unknown. Only a bug with a
        "symptom" looks at it.
        Returns ({invariant id: bug id} for the failures of a known bug, {invariant id} for all other failures}.
        """
        from tests.mutation_testing.reports.triage.action_text import parse_action
        kind = parse_action(action_id)["kind"]
        if not (action_id or "").startswith("repair:"):
            self._last_user_kind = kind
        failed_ids = {invariant_id(n) for n in failed}
        checked_ids = None if checked is None else {invariant_id(n) for n in checked}
        self._details = {} if details is None else dict(details)
        for inv in list(self._streak_kinds):
            if inv not in failed_ids and (checked_ids is None or inv in checked_ids):
                del self._streak_kinds[inv]
        known: dict[str, str] = {}
        other: set[str] = set()
        for inv in sorted(failed_ids):
            kinds = self._streak_kinds.setdefault(inv, {kind, self._last_user_kind})
            bug = self._match(inv, kinds, failed_ids)
            if bug:
                known[inv] = bug["id"]
            else:
                other.add(inv)
        return known, other

    def no_repair_ids(self, failed: Iterable[str]) -> set[str]:
        """Invariant ids among `failed` (names or ids) whose current failure belongs to a known bug with
        "do_not_repair": true. Read-only: call it after observe() has seen the same report."""
        ids = set()
        failed_ids = {invariant_id(n) for n in failed}
        for name in failed:
            inv = invariant_id(name)
            kinds = self._streak_kinds.get(inv)
            bug = self._match(inv, kinds, failed_ids) if kinds else None
            if bug and bug.get("do_not_repair"):
                ids.add(inv)
        return ids

    def _match(self, inv: str, kinds: set[str], failed_ids: set[str] | None = None) -> dict | None:
        for bug in self.bugs:
            if inv not in bug.get("invariants", []):
                continue
            companions = (bug.get("only_with") or {}).get(inv)
            if companions and not set(companions) & (failed_ids or set()):
                continue
            wanted = bug.get("when_action_kinds")
            if wanted and not kinds & set(wanted):
                continue
            if not symptom_matches(bug, inv, self._details.get(inv)):
                continue
            return bug
        return None


def observe_history_step(matcher: KnownBugMatcher, step: dict) -> tuple[dict[str, str], set[str]]:
    """observe() for one step record of a history.json (loop or replay). A step whose action failed is skipped."""
    if step.get("action_succeeded") is False:
        return {}, set()
    evidence = step.get("failure_evidence")
    details = None if evidence is None else {ev.get("id"): ev.get("details") or {} for ev in evidence if ev.get("id")}
    return matcher.observe(step.get("action_id", ""), step.get("failed_invariants") or [],
                           step.get("checked_invariants"), details)