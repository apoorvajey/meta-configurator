"""Known bugs in the recorded runs, in three views of the same data (the known bugs of config/triage_config.json).
    python -m tests.cli stats bugs [--by bug|profile|invariant] [--csv] [--no-open]
One page with three tabs: By bug (steps to the first detection, bugs per run), By profile, By invariant (which
invariants flag which bug, hand-written vs inferred). --by chooses the tab that opens first and the table --csv prints.
All three read each run's original history.json through KnownBugMatcher, the matcher behind the run status, so a bug
counts here exactly when the run status calls it a known bug. Only the loop's own steps count (not repair clicks);
a step number is the step of the run (1-based), as in the logs. The report page is saved in tests/output/reports/stats/.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

from tests.mutation_testing.runs.history_io import load_history
from tests.mutation_testing.runs.known_bugs import KnownBugMatcher, load_known_bugs, observe_history_step
from tests.mutation_testing.runs.repair_view import expand_repairs
from tests.mutation_testing.stats.common import Report, Table, make_parser, publish, run_dirs


@dataclass
class RunFacts:
    run_id: str
    profile: str
    n_steps: int
    first_step: dict[str, int] = field(default_factory=dict)             # bug id -> first step that showed it
    first_by: dict[str, set[str]] = field(default_factory=dict)          # bug id -> invariants that flagged it at that step
    hits: dict[tuple[str, str], list[int]] = field(default_factory=dict)  # (bug id, invariant id) -> steps


def profile_of(meta: dict) -> str:
    """Action-weights profile of a run: 'action_weights_data_focus.json' -> 'data_focus', 'action_weights.json' -> 'default'."""
    file = (meta.get("resolved_settings") or {}).get("weights_profile_file")
    if file:
        return Path(str(file)).stem.removeprefix("action_weights").lstrip("_") or "default"
    return (meta.get("run_config") or {}).get("action_weights_profile") or "unknown"


def read_run(run_dir: Path, bugs: list[dict]) -> RunFacts | None:
    try:
        steps, meta = load_history(run_dir / "history.json")
    except (OSError, ValueError):
        return None
    facts = RunFacts(run_dir.name, profile_of(meta), len(steps))
    matcher = KnownBugMatcher(bugs)
    number = 0
    for s in expand_repairs(steps):
        known, _ = observe_history_step(matcher, s)   # every record, so failure streaks start and end correctly
        if str(s.get("action_id", "")).startswith("repair:"):
            continue
        number += 1
        if s.get("passed") is not False or not known:
            continue
        for inv, bug in known.items():
            facts.hits.setdefault((bug, inv), []).append(number)
        for bug in set(known.values()):
            if bug not in facts.first_step:
                facts.first_step[bug] = number
                facts.first_by[bug] = {i for i, b in known.items() if b == bug}
    return facts


def _pct(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def _stats(values: list[int]) -> list:
    """[min, median, mean, max] or four Nones."""
    if not values:
        return [None] * 4
    return [min(values), statistics.median(values), statistics.mean(values), max(values)]


def detection(args, facts: list[RunFacts], bugs: list[dict], base: str) -> Report:
    n = len(facts)
    if args.by == "profile":
        groups = defaultdict(list)
        for f in facts:
            groups[f.profile].append(f)
        rows = []
        for profile, group in sorted(groups.items()):
            firsts = [min(f.first_step.values()) for f in group if f.first_step]
            rows.append([profile, len(group), len(firsts), _pct(len(firsts), len(group)),
                         statistics.mean(len(f.first_step) for f in group),
                         statistics.median(firsts) if firsts else None, statistics.mean(firsts) if firsts else None,
                         statistics.mean(f.n_steps for f in group)])
        table = Table("By action-weights profile",
                      ["profile", "runs", "runs_with_bug", "pct_runs_with_bug", "bugs_per_run_mean",
                       "first_bug_step_median", "first_bug_step_mean", "run_length_mean"], rows,
                      intro="One row per action-weights profile of the runs.")
    else:
        rows = []
        for b in sorted(bugs, key=lambda b: str(b["id"])):
            steps = [f.first_step[b["id"]] for f in facts if b["id"] in f.first_step]
            rows.append([b["id"], b.get("name", ""), len(steps), _pct(len(steps), n), *_stats(steps)])
        table = Table("By known bug",
                      ["bug", "name", "runs_found", "pct_runs", "first_step_min", "first_step_median",
                       "first_step_mean", "first_step_max"], rows, text_columns=2, flag=lambda r: r[2] == 0,
                      intro="Steps to the first detection of each known bug; bugs no run found are in orange.")

    with_bug = [f for f in facts if f.first_step]
    firsts = [min(f.first_step.values()) for f in with_bug]
    per_run = Counter(len(f.first_step) for f in facts)
    notes = [base]
    if n:
        notes.append(f"Distinct known bugs per run: mean {statistics.mean(len(f.first_step) for f in facts):.2f}; runs by number of "
                     "bugs: " + ", ".join(f"{k}: {v}" for k, v in sorted(per_run.items())) + ".")
    return Report(
        name="bug_detection" + ("_by_profile" if args.by == "profile" else ""), title="Bug detection",
        subtitle=f"{n} run(s) in {args.runs_dir}",
        intro="When the loop first showed each known bug, and how many bugs a run finds. A bug counts when the run status "
              "calls it a known bug; repair clicks do not count.",
        cards=[(n, "runs read"), (f"{len(with_bug)} ({_pct(len(with_bug), n)}%)" if n else "-", "runs with a known bug"),
               (f"{statistics.mean(len(f.first_step) for f in facts):.2f}" if n else "-", "known bugs per run (mean)"),
               (statistics.median(firsts) if firsts else "-", "median step of the first bug"),
               (statistics.median(f.n_steps for f in facts) if n else "-", "median run length (steps)")],
        tables=[table], notes=notes)


def contribution(args, facts: list[RunFacts], bugs: list[dict], base: str) -> Report:
    def origin(inv: str) -> str:
        return "inferred" if inv.startswith("INF-") else "hand-written"

    rows = []
    for b in sorted(bugs, key=lambda b: str(b["id"])):
        bug_id = b["id"]
        runs_with_bug = sum(1 for f in facts if bug_id in f.first_step)
        invariants = set(b.get("invariants", [])) | {inv for f in facts for (bg, inv) in f.hits if bg == bug_id}
        bug_rows = []
        for inv in invariants:
            flagged = [f for f in facts if (bug_id, inv) in f.hits]
            bug_rows.append([bug_id, inv, origin(inv), len(flagged), _pct(len(flagged), runs_with_bug),
                             sum(len(f.hits[(bug_id, inv)]) for f in flagged),
                             sum(1 for f in facts if inv in f.first_by.get(bug_id, ()))])
        rows += sorted(bug_rows, key=lambda r: (-r[3], r[1]))   # the invariants that flag it most often first

    # Per (run, bug) detection: found by hand-written invariants only, by inferred ones only, or by both
    kinds = Counter()
    for f in facts:
        for bug_id in f.first_step:
            origins = {origin(inv) for (bg, inv) in f.hits if bg == bug_id}
            kinds["both" if len(origins) == 2 else ("inferred only" if "inferred" in origins else "hand-written only")] += 1
    return Report(
        name="invariant_contribution", title="Invariant contribution", subtitle=f"{len(facts)} run(s) in {args.runs_dir}",
        intro="Which invariants flag which known bug, and whether hand-written or inferred invariants found it. Invariants "
              "listed for a bug that never fired are in orange.",
        cards=[(sum(kinds.values()), "run/bug detections"), (kinds["hand-written only"], "found by hand-written only"),
               (kinds["inferred only"], "found by inferred only"), (kinds["both"], "found by both")],
        tables=[Table("Invariants per known bug",
                      ["bug", "invariant", "origin", "runs_flagged", "pct_of_runs_with_bug", "failing_steps", "runs_flagged_first"],
                      rows, text_columns=3, flag=lambda r: r[3] == 0)],
        notes=[base, "runs_flagged_first: runs in which the invariant flagged the bug at the very first step that showed it. "
                     "A run/bug detection is one bug found in one run."])


def main() -> None:
    parser = make_parser(__doc__)
    parser.add_argument("--by", choices=("bug", "profile", "invariant"), default="bug",
                        help="the tab that opens first, and the table --csv prints: per known bug (default), per "
                             "action-weights profile, or per bug and invariant")
    args = parser.parse_args()

    bugs = [b for b in load_known_bugs() if b.get("id")]
    facts, skipped = [], 0
    for run_dir in run_dirs(args):
        run = read_run(run_dir, bugs)
        if run is None:
            skipped += 1
        else:
            facts.append(run)
    base = f"{len(facts)} run(s) read, {skipped} run folder(s) without history.json; {len(bugs)} known bug(s) in triage_config.json."
    views = {"bug": ("By bug", detection(SimpleNamespace(**{**vars(args), "by": "bug"}), facts, bugs, base)),
             "profile": ("By profile", detection(SimpleNamespace(**{**vars(args), "by": "profile"}), facts, bugs, base)),
             "invariant": ("By invariant", contribution(args, facts, bugs, base))}
    order = [args.by] + [k for k in views if k != args.by]   # the chosen view is the first tab (and what --csv prints)
    tables = []
    for key in order:
        label, report = views[key]
        table = replace(report.tables[0], tab=label)
        if key == "invariant":   # the detection split of the contribution view, as text instead of cards
            c = {name: value for value, name in report.cards}
            table = replace(table, intro=f"{table.intro} Of {c['run/bug detections']} detections (one bug in one run): found by "
                                         f"hand-written only {c['found by hand-written only']}, inferred only "
                                         f"{c['found by inferred only']}, both {c['found by both']}.")
        tables.append(table)
    n = len(facts)
    with_bug = [f for f in facts if f.first_step]
    publish(args, Report(
        name="bug_detection", title="Bug detection", subtitle=f"{n} run(s) in {args.runs_dir}",
        intro="When the loop first showed each known bug, which profiles find them, and which invariants flag them. A bug counts "
              "when the run status calls it a known bug; repair clicks do not count.",
        cards=[(n, "runs read"), (f"{len(with_bug)} ({_pct(len(with_bug), n)}%)" if n else "-", "runs with a known bug")],
        tables=tables, csv_table=0, notes=views["bug"][1].notes + views["invariant"][1].notes[1:]))


if __name__ == "__main__":
    main()