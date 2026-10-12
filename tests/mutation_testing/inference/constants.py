"""Default folders and file names of the inference engine."""
from __future__ import annotations




# One JSONL file per dataset, written by MutationLoop._append_snapshot(). _untagged.jsonl is ignored.
from tests.mutation_testing.common import paths as _paths

SNAPSHOTS_DIR = _paths.TRACES_DIR.as_posix()


UNTAGGED_DATASET_NAME = "_untagged"


INFERRED_RULES_PATH = _paths.INFERRED_RULES_PATH.as_posix()


INFERENCE_DIR = _paths.INFERENCE_DIR.as_posix()   # review page, readable report
INFERENCE_RUNS_DIR = _paths.INFERENCE_RUNS_DIR.as_posix()   # one folder per inference run
SWEEP_DIR = _paths.SWEEP_DIR.as_posix()   # threshold sweeps
