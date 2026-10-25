"""Where the generated output lives; the one place that defines it. Relative to the repository root, like all
commands (python -m tests.cli ...).
"""
from pathlib import Path

OUTPUT_DIR = Path("tests/output")
RUNS_DIR = OUTPUT_DIR / "runs"                      # runs/<run_id>/ (history, logs, screenshots, replays)
REPORTS_DIR = OUTPUT_DIR / "reports"                # one subfolder per report type (below)
TRACES_DIR = OUTPUT_DIR / "traces"                  # <dataset>.jsonl snapshots for inference (+ archive/)
INFERRED_RULES_PATH = OUTPUT_DIR / "inferred_rules.json"   # the rules the mutation loop checks
RULES_BACKUPS_DIR = OUTPUT_DIR / "rules_backups"    # earlier versions of inferred_rules.json
ISSUES_DIR = REPORTS_DIR / "issues"                 # issues_<time>.html + .json
BATCH_DIR = REPORTS_DIR / "batch"                   # batch_<id>.txt / .json / .log
CORPUS_DIR = REPORTS_DIR / "corpus"                 # corpus_report_<time>.html
INBOX_DIR = OUTPUT_DIR / "inbox"                   # optional: save the pages' downloads here; picked up before the Downloads folder, applied files are moved on
BUG_UPDATES_DIR = OUTPUT_DIR / "bug_updates"          # where the known-bugs page's Update file is saved (setting: known_bugs_update_folder in triage_config.json)
KNOWN_BUGS_DIR = REPORTS_DIR / "known_bugs"          # known_bugs.html (refreshed by the bugs command)
CONFIG_BACKUPS_DIR = OUTPUT_DIR / "config_backups"  # triage_config.<time>.bak.json, made before every bug is added or removed
INVARIANTS_DIR = REPORTS_DIR / "invariants"         # invariants.html (all invariants, refreshed by the invariants command)
INFERENCE_DIR = REPORTS_DIR / "inference"           # review.html, invariant_report.md
INFERENCE_RUNS_DIR = INFERENCE_DIR / "runs"         # one folder per inference run
SWEEP_DIR = INFERENCE_DIR / "threshold_sweep"       # threshold_sweep_<time>.csv / .md / .png
STATS_DIR = REPORTS_DIR / "stats"                  # the stats command: <report>_<time>.html (+ csv / md / png of sweep and yield)