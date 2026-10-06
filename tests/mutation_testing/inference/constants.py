"""Default folders and file names of the inference engine."""
from __future__ import annotations




# One JSONL file per dataset, written by MutationLoop._append_snapshot(). _untagged.jsonl is ignored.
SNAPSHOTS_DIR = "invariant_miner_traces"


UNTAGGED_DATASET_NAME = "_untagged"


INFERRED_RULES_PATH = "inferred_rules.json"


REPORTS_DIR = "reports"  # default folder for generated reports and dashboards
