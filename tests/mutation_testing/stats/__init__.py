"""Stats: read-only reports with the numbers for the evaluation chapter.   python -m tests.cli stats [report] [options]

Every report saves a report page in tests/output/reports/stats/ (the look of the corpus report).
To add a report: write a module that runs as `python -m <module>` and add one line to REPORTS.
A report is (description, module, preset arguments put in front of the user's options; {stamp} = the current time).
"""
from tests.mutation_testing.common.paths import STATS_DIR

_STATS = "tests.mutation_testing.stats"
_OUT = STATS_DIR.as_posix()

REPORTS: dict[str, tuple[str, str, list[str]]] = {
    "rules": ("Rule activity: how often each accepted inferred rule was checked, violated, never applicable",
              f"{_STATS}.rule_activity", []),
    "bugs": ("Bug detection: steps to the first known bug, bugs per run (--by bug|profile), or which invariants flag which bug (--by invariant)",
             f"{_STATS}.bug_findings", []),
    "usage": ("Invariant usage: which invariants are never checked, never fail, or fire (all invariants, hand-written and inferred)",
              f"{_STATS}.invariant_usage", []),
    "yield": ("Template yield: candidates, promotable, exported, reviewed, accepted, trivial, rejected per template",
              f"{_STATS}.template_yield", []),
    "sweep": ("Threshold sweep: how the promotion threshold changes the mined invariants",
              "tests.mutation_testing.inference.threshold_sweep",
              ["--csv", f"{_OUT}/threshold_sweep_{{stamp}}.csv", "--report", f"{_OUT}/threshold_sweep_{{stamp}}.md",
               "--figure", f"{_OUT}/threshold_sweep_{{stamp}}.png"]),
    "corpus": ("Corpus report: HTML overview of the runs in the recorded traces",
               "tests.mutation_testing.traces.corpus_report", ["--out", f"{_OUT}/corpus_report_{{stamp}}.html"]),
}