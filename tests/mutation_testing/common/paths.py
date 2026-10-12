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
INFERENCE_DIR = REPORTS_DIR / "inference"           # review.html, invariant_report.md
INFERENCE_RUNS_DIR = INFERENCE_DIR / "runs"         # one folder per inference run
SWEEP_DIR = INFERENCE_DIR / "threshold_sweep"       # threshold_sweep_<time>.csv / .md / .png
