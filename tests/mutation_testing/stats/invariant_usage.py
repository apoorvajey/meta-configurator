"""How useful is each invariant? Which ones are never checked, never fail, or fire, over all recorded runs.
    python -m tests.cli stats usage [--csv] [--no-open] [--out FILE] [--runs-dir DIR]

Every invariant of `python -m tests.cli invariants` (hand-written and inferred) gets one row, read from each run's original
history.json. Counted over the loop's own steps (repair clicks do not count):
    steps_checked   steps where the invariant was evaluated        steps_skipped  steps where its precondition did not hold
    steps_failed    steps where it failed = known_bug_steps (the failure belongs to a known bug) + other_steps
    failed_alone    failed steps where no other invariant failed too (what only this invariant would have caught)
Status: never checked (never evaluated: dead weight, or its precondition never held) | never failed (evaluated, silent) |
known bugs only (fails only on known bugs) | other failures too. Never-checked invariants come first.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from tests.mutation_testing.reports.invariant_list import collect_invariants
from tests.mutation_testing.runs.history_io import load_history
from tests.mutation_testing.runs.known_bugs import KnownBugMatcher, invariant_id, load_known_bugs, observe_history_step
from tests.mutation_testing.runs.repair_view import expand_repairs
from tests.mutation_testing.inference.inferred_rules import load_inferred_rules
from tests.mutation_testing.stats.common import Report, Table, add_review_arg, make_parser, publish, run_dirs
from tests.mutation_testing.stats.pending import pending_rules

STATUSES = ("never checked", "never failed", "known bugs only", "other failures too")
HEADER = ["invariant", "origin", "type", "runs_checked", "steps_checked", "steps_skipped", "steps_failed", "runs_failed",
          "known_bug_steps", "other_steps", "failed_alone", "fail_rate_pct", "status"]


def _status(c: Counter) -> str:
    if not c["checked"]:
        return STATUSES[0]
    if not c["known"] and not c["other"]:
        return STATUSES[1]
    return STATUSES[3] if c["other"] else STATUSES[2]


def count_run(steps: list[dict], bugs: list[dict], stats: dict[str, Counter]) -> tuple[int, set[str]]:
    """Add one run's loop steps to `stats` (invariant id -> counts). Returns (loop steps, ids seen)."""
    matcher = KnownBugMatcher(bugs)
    seen: set[str] = set()
    n = 0
    touched: dict[str, set[str]] = defaultdict(set)   # which counters this run touched, for the per-run counts
    for s in expand_repairs(steps):
        known, other = observe_history_step(matcher, s)   # every record, so failure streaks start and end correctly
        if str(s.get("action_id", "")).startswith("repair:") or s.get("action_succeeded") is False:
            continue
        n += 1
        checked = {invariant_id(i) for i in s.get("checked_invariants") or []}
        skipped = {invariant_id(i) for i in s.get("skipped_invariants") or []} - checked
        failed = set(known) | other
        seen |= checked | skipped | failed
        for inv in checked | failed:   # a failing invariant was evaluated
            stats[inv]["checked"] += 1
            touched[inv].add("checked")
        for inv in skipped:
            stats[inv]["skipped"] += 1
        for inv in failed:
            stats[inv]["known" if inv in known else "other"] += 1
            touched[inv].add("failed")
            if len(failed) == 1:
                stats[inv]["alone"] += 1
    for inv, kinds in touched.items():
        stats[inv]["runs_checked"] += "checked" in kinds
        stats[inv]["runs_failed"] += "failed" in kinds
    return n, seen


def main() -> None:
    parser = make_parser(__doc__)
    add_review_arg(parser)
    args = parser.parse_args()
    bugs = load_known_bugs()
    stats: dict[str, Counter] = defaultdict(Counter)
    runs = skipped_runs = loop_steps = 0
    seen: set[str] = set()
    for run_dir in run_dirs(args):
        try:
            steps, _ = load_history(run_dir / "history.json")
        except (OSError, ValueError):
            skipped_runs += 1
            continue
        n, ids = count_run(steps, bugs, stats)
        runs += 1
        loop_steps += n
        seen |= ids

    # Rules accepted in a review after the newest run started cannot have run yet: their own section, not in the counts
    pending = pending_rules(load_inferred_rules(), run_dirs(args), args.review_dir)
    everything = collect_invariants()
    pending_rows = [[i["id"], i["type"], i["text"], f"{pending[i['id']]['added']:%Y-%m-%d %H:%M}", pending[i["id"]]["review"]]
                    for i in sorted((i for i in everything if i["id"] in pending), key=lambda i: (pending[i["id"]]["added"], i["id"]))]
    invariants = [i for i in everything if i["id"] not in pending]
    rows, status_of = [], {}
    for inv in invariants:
        c = stats.get(inv["id"], Counter())
        status_of[inv["id"]] = _status(c)
        failed = c["known"] + c["other"]
        rows.append([inv["id"], inv["origin"], inv["type"], c["runs_checked"], c["checked"], c["skipped"], failed,
                     c["runs_failed"], c["known"], c["other"], c["alone"],
                     round(100 * failed / c["checked"], 1) if c["checked"] else None, status_of[inv["id"]]])
    rows.sort(key=lambda r: (STATUSES.index(r[12]), r[6], r[4], r[0]))

    by_type: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in rows:
        by_type[(r[1], r[2])][r[12]] += 1
        by_type[(r[1], r[2])]["total"] += 1
    type_rows = [[origin, typ, c["total"], *(c[s] for s in STATUSES)] for (origin, typ), c in sorted(by_type.items())]
    counts = Counter(status_of.values())
    gone = sorted(seen - {i["id"] for i in invariants})

    notes = [f"{runs} run(s) read ({loop_steps} loop steps), {skipped_runs} run folder(s) without history.json.",
             "never checked = not evaluated in any step: its precondition never held (see steps_skipped) or it never applies in "
             "these runs. fail_rate_pct = steps_failed / steps_checked * 100: how often the invariant fails in the steps where it was "
             "evaluated (\"-\" if never checked). A rule that never fails is not necessarily useless (a correct rule on a mostly correct app rarely fires)."]
    if gone:
        notes.append(f"{len(gone)} invariant id(s) appear in the runs but are not defined any more (retired or re-mined), not in "
                     f"the table: {', '.join(gone[:8])}{' ...' if len(gone) > 8 else ''}")
    publish(args, Report(
        name="invariant_usage", title="Invariant usage", subtitle=f"{runs} run(s) in {args.runs_dir}",
        intro="Which invariants are checked and which ever fire, over all recorded runs. Never-checked invariants come first (orange).",
        cards=[(len(rows), "invariants"), (counts[STATUSES[0]], "never checked"), (counts[STATUSES[1]], "checked, never failed"),
               (counts[STATUSES[2]], "fail on known bugs only"), (counts[STATUSES[3]], "also fail otherwise")]
        + ([(len(pending_rows), "not run yet")] if pending_rows else []),
        tables=([Table("Not run yet", ["invariant", "template", "statement", "added", "review_file"], pending_rows, text_columns=5,
                       collapsed=True,
                       intro="Inferred rules accepted in a review after the newest run started: they have not had a chance to run, "
                             "so they are not in the tables below.")] if pending_rows else [])
        + [Table("Invariants", HEADER, rows, text_columns=3, flag=lambda r: r[12] == STATUSES[0]),
                Table("By origin and type", ["origin", "type", "invariants", *(s.replace(" ", "_") for s in STATUSES)],
                      type_rows, text_columns=2,
                      intro="Hand-written: type = the editor it runs in (data, schema, any) or temporal. Inferred: the template.")],
        csv_table=1 if pending_rows else 0,
        notes=notes + ([f"{len(pending_rows)} rule(s) added in a review after the newest run are listed under \"Not run yet\"."]
                       if pending_rows else [])))


if __name__ == "__main__":
    main()