"""How often each accepted inferred rule was checked, summed over the recorded runs (rule_activity.json).
    python -m tests.mutation_testing.reports.rule_activity_report [--csv] [--runs-dir DIR]
Rules that were never evaluated come first. --csv prints the per-rule table as CSV on stdout (summary goes to stderr).
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from tests.mutation_testing.inference.inferred_rules import load_inferred_rules
from tests.mutation_testing.runs.run_layout import RUNS_DIR, iter_run_dirs

HEADER = ["id", "template", "category", "runs_evaluated", "total_evaluations", "total_violations", "total_no_observations"]

parser = argparse.ArgumentParser()
parser.add_argument("--csv", action="store_true", help="print the per-rule table as CSV")
parser.add_argument("--runs-dir", default=str(RUNS_DIR), help="where the runs are (default: tests/output/runs)")
args = parser.parse_args()

# One entry per run folder that has rule_activity.json (folders outside the run layout, e.g. an archive folder, are not runs)
activities, missing = [], 0
for run_dir in iter_run_dirs(Path(args.runs_dir)):
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

summary = [f"{len(activities)} run(s) read, {missing} run folder(s) without rule_activity.json",
           f"accepted rules: {len(rows)} | evaluated at least once: {sum(v[1] for v in per_template.values())}"
           f" | never evaluated: {sum(v[0] - v[1] for v in per_template.values())}",
           "", f"{'template':<32}{'accepted':>9}{'evaluated':>10}{'never':>7}"]
summary += [f"{t:<32}{a:>9}{e:>10}{a - e:>7}" for t, (a, e) in sorted(per_template.items())]

if args.csv:
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    print("\n".join(summary), file=sys.stderr)
else:
    widths = [max(len(str(x)) for x in col) for col in zip(HEADER, *rows)]
    for line in [HEADER] + rows:
        print("  ".join(str(x).ljust(w) if i < 3 else str(x).rjust(w) for i, (x, w) in enumerate(zip(line, widths))))
    print("\n" + "\n".join(summary))
