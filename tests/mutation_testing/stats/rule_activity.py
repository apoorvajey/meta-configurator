"""How often each accepted inferred rule was checked, summed over the recorded runs (rule_activity.json).
    python -m tests.cli stats rules [--csv] [--no-open] [--out FILE] [--runs-dir DIR]
Rules that were never evaluated come first. The report page is saved in tests/output/reports/stats/.
"""
import json
from collections import defaultdict

from tests.mutation_testing.inference.inferred_rules import load_inferred_rules
from tests.mutation_testing.stats.common import Report, Table, add_review_arg, make_parser, publish, run_dirs
from tests.mutation_testing.stats.pending import pending_rules

HEADER = ["id", "template", "category", "runs_evaluated", "total_evaluations", "total_violations", "total_no_observations"]


def main() -> None:
    parser = make_parser(__doc__)
    add_review_arg(parser)
    args = parser.parse_args()

    # One entry per run folder that has rule_activity.json
    activities, missing = [], 0
    for run_dir in run_dirs(args):
        try:
            activities.append(json.loads((run_dir / "rule_activity.json").read_text(encoding="utf-8"))["rules"])
        except (OSError, ValueError, KeyError):
            missing += 1

    # The accepted rules are the ones in the current inferred_rules.json; without it, the ones the runs mention
    accepted = {r["id"]: r for r in load_inferred_rules()}
    if not accepted:
        for rules in activities:
            for rid, v in rules.items():
                accepted.setdefault(rid, {"id": rid, "template": v.get("template", "?"), "category": v.get("category", "?")})

    # Rules accepted in a review after the newest run started cannot have run yet: their own section, not in the counts
    pending = pending_rules(list(accepted.values()), run_dirs(args), args.review_dir)
    pending_rows = [[rid, accepted[rid]["template"], accepted[rid]["category"], f"{p['added']:%Y-%m-%d %H:%M}", p["review"]]
                    for rid, p in sorted(pending.items(), key=lambda kv: (kv[1]["added"], kv[0]))]
    accepted = {rid: r for rid, r in accepted.items() if rid not in pending}

    rows = []
    for rid, rule in accepted.items():
        seen = [a[rid] for a in activities if rid in a]
        rows.append([rid, rule["template"], rule["category"],
                     sum(1 for v in seen if v["evaluated"] > 0), sum(v["evaluated"] for v in seen),
                     sum(v["violated"] for v in seen), sum(v["no_observations"] for v in seen)])
    rows.sort(key=lambda r: (r[4] > 0, r[4], r[0]))   # never evaluated first, then the least evaluated

    per_template = defaultdict(lambda: [0, 0])   # template -> [accepted, evaluated at least once]
    for r in rows:
        per_template[r[1]][0] += 1
        per_template[r[1]][1] += r[4] > 0
    evaluated = sum(v[1] for v in per_template.values())

    publish(args, Report(
        name="rule_activity", title="Rule activity", subtitle=f"{len(activities)} run(s) in {args.runs_dir}",
        intro="How often each accepted inferred rule was checked in the mutation loop. A rule that is never violated is not "
              "a problem; a rule that is never evaluated is dead weight.",
        cards=[(len(activities), "runs read"), (len(rows), "accepted rules"), (evaluated, "evaluated at least once"),
               (len(rows) - evaluated, "never evaluated")] + ([(len(pending_rows), "not run yet")] if pending_rows else []),
        tables=([Table("Not run yet", ["id", "template", "category", "added", "review_file"], pending_rows, text_columns=5,
                       links={0: ("invariant",)}, intro="Accepted in a review after the newest run started, so they have not had a chance to run. "
                             "They are not in the counts below.")] if pending_rows else [])
        + [Table("Rules", HEADER, rows, text_columns=3, intro="Never evaluated first.", flag=lambda r: r[4] == 0,
                 links={0: ("invariant",)}),
           Table("By template", ["template", "accepted", "evaluated", "never_evaluated"],
                 [[t, a, e, a - e] for t, (a, e) in sorted(per_template.items())])],
        csv_table=1 if pending_rows else 0,
        notes=([f"{missing} run folder(s) without rule_activity.json (runs from before the log existed)."] if missing else [])
        + ([f"{len(pending_rows)} rule(s) added in a review after the newest run are listed under \"Not run yet\"."] if pending_rows else [])))


if __name__ == "__main__":
    main()