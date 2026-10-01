# Mutation testing for MetaConfigurator

A Playwright loop drives the real MetaConfigurator web app with random, schema-aware edits. After every step it checks the app against invariants: statements that must always hold, such as "the Text View shows the same content as the internal data". Failing runs are saved so they can be replayed and triaged. A separate engine learns further invariants from the recorded runs.

```
   config/*.json  ──►  mutation_loop  ──►  history.json, mutation_log.html, screenshots
                              │                        │
                              │                        ├──►  replay_runner  ──►  snapshots, cause-tracking history
                              │                        └──►  issue_report  ──►  reports/issues.html
                              ▼
        invariant_miner_traces/<dataset>.jsonl
                       │
        invariant_inference_engine  ──►  inferred_rules.json  ──►  checked by the loop from then on
                       │
        inference_dashboard
```

## Contents

- [Setup](#setup)
- [Running the tools](#running-the-tools)
- [The mutation loop](#the-mutation-loop)
- [Invariants](#invariants)
- [Replay](#replay)
- [Issue report](#issue-report)
- [Older tools](#older-tools)
- [Invariant inference](#invariant-inference)
- [Configuration files](#configuration-files)
- [Output folders](#output-folders)
- [Limitations and open items](#limitations-and-open-items)
- [File map](#file-map)

## Setup

Python 3.10 or newer. Run everything from the repository root.

```
pip install playwright deepdiff rstr jsonschema referencing pyyaml
playwright install chromium
pip install pillow                 # optional
```

Pillow shrinks screenshots in the reports.

**Target app.** `LIVE_BASE_URL` in `tests/shared/python/utils.py` is `https://www.metaconfigurator.org`. For a local build, run `npm run dev` in `meta_configurator` and set the constant to `http://localhost:5173`. The loop enables the app's internal test panel through `localStorage`; the invariants read the ground-truth state from it. The browser runs with a visible window (`headless=False`).

## Running the tools

| Tool | Command | Modes and options |
|---|---|---|
| **Mutation loop** | `python -m tests.mutation_testing.mutation_loop` | No flags. Everything comes from `config/run_config.json`: `run_mode` = `background` (no pauses), `watch_failures` (pause on a failure), `interactive` (pause after every step), `null` (asks in the terminal). Also `schema`, `num_steps`, `screenshots`, `action_weights_profile` (`default`, `data_focus`, `schema_focus`, `textview_focus`, `random`). |
| **Batch of runs** | `python -m tests.mutation_testing.overnight_runner --runs 24` | `--timeout` (s per run), `--datasets`, `--shuffle-laps`, `--run-config`, `--module`. Cycles through the four datasets, one subprocess per run. |
| **Replay** | `python -m tests.mutation_testing.replay_runner --latest` | Automatic (default), `--until N` (run quietly to step N, then manual), `--interactive` (manual from step 1), `--pause-on-failure`. See [Replay](#replay). |
| **Issue report** (open this after a batch) | `python -m tests.mutation_testing.issue_report` | All runs by default; `--last-hours N`, `--since`/`--until`, or one `history.json` as argument. `--no-open` skips the browser. |
| **Inference engine** | `python -m tests.mutation_testing.invariant_inference_engine` | `--profile default\|strict\|lenient`, `--threshold`, `--min-obs`, `--datasets`, `--verbose`. |
| **Triage report** (older) | `python -m tests.mutation_testing.analyze_mutation_logs --last-hours 12` | `--mode auto\|single\|multi`, `--since`/`--until`, `--out`. |

Typical overnight workflow:

```
python -m tests.mutation_testing.overnight_runner --runs 24
python -m tests.mutation_testing.issue_report --last-hours 12
python -m tests.mutation_testing.invariant_inference_engine
```

Reports are written to `reports/`.

## The mutation loop

`mutation_loop.py` loads one schema with its data file, takes a baseline invariant check, then performs `num_steps` mutation steps.

**Datasets.** Autonomous Vehicle, Feature Testing, Strenda, EnzymeML. The schema is chosen in the app's "Example Schemas" dropdown and the data file (in `test_data/`) is uploaded. Snapshots are tagged with the dataset so invariants can be inferred per dataset.

**Actions.** `dom_discoverer.py` lists the actions on the page from the app's `data-testid` attributes, in the GUI View and Text View of both the Data Editor and the Schema Editor:

| Area | Actions |
|---|---|
| GUI View | change a value, dropdown or checkbox, add a property or required entry, rename, remove, expand, collapse, switch format (JSON/YAML), undo, redo, switch between Data and Schema |
| Text View | edit a value, add a field, change a value's type, remove a field, rename a field, add or remove an array item, clear the whole document |

Values come from `ValueGenerator`, which reads the governing schema at the target path (types, enums, patterns) through `schema_path_enumerator.py`. It also produces invalid values on purpose, to exercise validation.

**Choosing the next action.** Three levels, each with a weight: Mode (Schema or Data Editor), then View (GUI or Text), then action type, then a concrete element. Weights live in `config/action_weights*.json`; an action can be switched off there with `enabled: false`. Three mechanisms shape the walk:

- *Locality bias.* The next action tends to stay in the same mode, view and part of the document. The focus fades after four steps without reinforcement.
- *Create-then-fill.* After adding a field, the next step tries to fill it in.
- *Undo/redo boosting.* Undo and redo are more likely right after a mutating action, and right after a single undo or redo, so short undo/redo chains occur. After two consecutive undo/redo steps the boost stops.

**Recovery.** A failure that appears in two consecutive steps triggers an automatic repair: undo clicks (at most 15) until the state matches the last known-good snapshot, confirmed by a fresh invariant check. If the failure followed a mode switch, the loop switches back first, because undo stacks are per mode. A `clear-text` action is always undone immediately. Every repair click is recorded as its own step.

**Reproducibility.** Each step records its random seed, the typed input and the element it acted on. The seed is re-applied right before the action runs.

## Invariants

Checked after each step. A failure is recorded with its evidence (values compared, paths involved, a diff where one applies).

### Single-state invariants (`tests/invariants/invariants_core.py`)

`InvariantDispatcher` reads the active mode from the UI and runs the matching checker. Settings mode is skipped.

| ID | Mode | Statement |
|---|---|---|
| INV-D1 | Data Editor | The Text View content parses to exactly the internal data. |
| INV-D3 | Data Editor | A top-level property shows the required star if and only if it is in `schema.required`. |
| INV-D4 | Data Editor | Every data key has a GUI row, and no GUI row shows content that has no data key behind it. |
| INV-D5 | Data Editor | Each top-level string field shows the same value as the internal data. |
| INV-D6 | Data Editor | A top-level property shows a validation-error icon if and only if its value violates the schema. |
| INV-GUI-ERR-01 | Data Editor | An `additionalProperties` violation shown in the Text View also shows an icon in the GUI View. |
| INV-S0 | Schema Editor | The schema is an object (or a boolean). |
| INV-S1 | Schema Editor | The Text View content parses to exactly the internal schema. |
| INV-S2 | Schema Editor | Every top-level schema key has a GUI row. |
| INV-S3 | Schema Editor | The title field equals the schema's `title`. |

INV-D2 was retired: "required fields are present in data" only holds right after adding a required item (TEMP-ADD-03), not as a standing rule.

### Temporal invariants (`tests/invariants/temporal_invariants.py`)

These compare the state before and after one action.

| ID | After | Statement |
|---|---|---|
| TEMP-EDIT-01 | edit field | `schema.properties` keys are unchanged. |
| TEMP-EDIT-02 | edit field (Data Editor) | The schema is unchanged. |
| TEMP-EDIT-03 | edit field | `schema.required` is unchanged in length. |
| TEMP-EXPAND-01, -02 | expand | Data, and schema, are unchanged. |
| TEMP-COLLAPSE-01, -02 | collapse | Schema, and data, are unchanged. |
| TEMP-RENAME-01, -02 | rename | The number of data keys, and of `schema.properties`, is unchanged. |
| TEMP-RENAME-03, -04 | rename | Exactly one key is swapped in `schema.properties`, and in data (or none, for a nested rename). |
| TEMP-ADD-01 | add item | `schema.properties` keys are unchanged. |
| TEMP-ADD-02 | add required item | `schema.required` grows by exactly one. |
| TEMP-ADD-03 | add required item | The new required key appears in data. |
| TEMP-MODE-01, -02 | mode switch | Data, and schema, are unchanged. |
| NEW-07 | any action | The schema state is still an object. |

No temporal check runs after a failed action, a repair step, or right after an undo or redo.

### Inferred invariants

`inferred_rules.json` holds the rules produced by the [inference engine](#invariant-inference). `TemporalInvariantChecker.run_all()` loads it from the working directory and evaluates the rules with the same functions that produced them; if the file is missing, they are skipped. Do not edit it by hand. Rules are exported when they reach `promotion_threshold` in every dataset. The committed file was exported at 95% (`--profile strict` or `--threshold 0.95`) and has 194 rules; the default threshold in `inference_config.json` is 0.85. `INF-9B75F68F` is left out on purpose: it says the GUI validation icon disagrees with validity after `clear_text@data`, which is a bug candidate, not an invariant. (Yet to investigate)

### Adding an invariant

- **Single state:** add a method named `_inv_<something>` to `InvariantCheckerDataEditor` or `InvariantCheckerSchemaEditor`. `run_all()` runs every method with that prefix.
- **Transition:** add a method to `TemporalInvariantChecker` and add it to the `checks` list in its `run_all()`. It is not discovered by name.

## Replay

`replay_runner.py` re-executes a saved `history.json` in a fresh browser.

```
python -m tests.mutation_testing.replay_runner --latest
python -m tests.mutation_testing.replay_runner --history mutation_history/<run_id>/history.json
python -m tests.mutation_testing.replay_runner --latest --until 50
python -m tests.mutation_testing.replay_runner --latest --interactive
python -m tests.mutation_testing.replay_runner --history history.json --export-cause-history
```

| Flag | Effect |
|---|---|
| `--latest` (default) | Newest `mutation_history/<run_id>/history.json`. |
| `--history PATH` | Replay that file. |
| `--schema NAME` | Schema/data pair; only needed for old histories without metadata. |
| `--until N`, `--pause-at N` | Run quietly to step N, then step manually. |
| `--interactive` | Manual from step 1; writes `replay_log.html`. |
| `--pause-on-failure` | Pause whenever an invariant fails. |
| `--html` | Also write `replay_log.html`. |
| `--no-state-snapshots` | Skip the before/after state files. |
| `--state-snapshot-dir DIR` | Default `replay_snapshots`. |
| `--export-cause-history` | Write a copy of the history with cause-tracking fields to `mutation_history_with_causes/`. |
| `--cause-history-dir DIR` | Change that folder. |

A replay uses the loop's own setup, the recorded seed and typed input for every step, and the same rules for when invariants are checked. The first point where it leaves the original run (different state hash, an action that succeeded in one run and failed in the other, a different generated property name) is reported as `REPLAY DIVERGED`. 

For every failing step the before/after app state is written to `replay_snapshots/<run_id>/step_<n>_before.json` and `_after.json`. The live loop writes the same files when a run finishes.

To get the cause analysis for an old run, replay it with `--export-cause-history`, then point a report at `mutation_history_with_causes`:

```
python -m tests.mutation_testing.issue_report mutation_history_with_causes
```

## Issue report

`issue_report.py` turns all recorded runs into one short list of distinct problems.

```
python -m tests.mutation_testing.issue_report
python -m tests.mutation_testing.issue_report --last-hours 12
python -m tests.mutation_testing.issue_report --since "2026-09-27 20:00" --until "2026-09-28 09:00"
python -m tests.mutation_testing.issue_report mutation_history/<run_id>/history.json
```

By default it reads every `history.json` under `mutation_history` and writes `reports/issues.html` and `reports/issues.json`.

| Option | Effect |
|---|---|
| `paths` | Folders (scanned recursively) and/or `history.json` files. Default: `mutation_history`. |
| `--since`, `--until`, `--last-hours` | Only runs that started in that window. |
| `--config PATH` | Another `triage_config.json`. |
| `--html FILE` | Output file, default `reports/issues.html`. |
| `--no-open` | Do not open the report in the browser. |

**How failures become problems.** Consecutive failing steps are merged into episodes. Each episode is sorted into a real problem, a crashed check (`CHK-ERR-...`, a tool problem) or noise (retired invariants, short unnamed glitches). Real problems are grouped by the rules that broke at the first failing step and the kind of action and editor that triggered it, so the same problem in 40 runs is one row. Problems found only by inferred rules (`INF-`) are tagged and listed last.

**What it shows.** A one-line list first; click a problem for what you would see on screen and up to five example runs (shortest first) with the exact steps, a link to that run's `mutation_log.html` at the failing step, copy buttons for the replay command and a drafted GitHub issue, and screenshots. Technical details (rules that broke, paths, the likely causing step from `causal_analysis.py`, snapshot locations) are collapsed. The repro length is the number of steps from a fresh load to the first failure in the shortest run that hit the problem; this is the shortest natural occurrence, not a reduced sequence.

Settings are in `triage_config.json`: the `issue_report` block, the rule wording (`invariants`, `invariant_details`), and the `causal` block.

## Older tools

These came first and still work. They share their loading code with the issue report.

- **Triage report** (`analyze_mutation_logs.py`): one card per finding, in tiers *Likely bug*, *Worth a look*, *Blip*, *Filtered*. With state snapshots a card shows the exact diff; with cause-tracking data it names the earlier action most likely responsible. `--mode multi` adds a section correlating each invariant's failures across runs. Writes `reports/triage_report.html`.
- **Test-case reducer** (`test_case_reducer.py`): ddmin reduction of a failing history. **Parked:** too slow to be practical, and not used by the issue report. Undo and redo act on a stack the harness cannot see, so a reduced sequence containing them must be replayed before it is trusted. Usage: `python -m tests.mutation_testing.test_case_reducer --history history.json --invariant INV-D6 --out reduced_history.json` (`--schema`, `--headed` also available).

Before a long batch, archive old traces, turn screenshots off and stop the machine from sleeping (the runner does this itself on Windows).

## Invariant inference

`invariant_inference_engine.py` generates candidate invariants from templates, checks them against the recorded snapshots, and exports the reliable ones, in the style of Daikon.

**Input.** One JSONL file per dataset in `invariant_miner_traces/`, written by the loop after every step. Each line is a snapshot of the app state, including GUI facts such as validation-icon state.

**Templates.** Each template is one rule function, used both to measure a candidate and to check the promoted rule during a run.

| Template | Statement |
|---|---|
| `stability` | A facet (or a schema keyword path) is identical before and after. |
| `change_count` | A facet changes in exactly k places. |
| `correlation` | If the top-level keys of A change, the keys of B change (or do not). |
| `path_key` | A key added or removed in A is added or removed in B too (or not). |
| `value_to_key` | Each key newly added to A is a top-level key of B afterwards (or not). |
| `key_containment` | After the step, the keys of A are a subset of the keys of B (or not). |
| `format_agreement` | The Text View content is in the format shown by the selector (or not). |
| `text_store_agreement` | The parsed Text View equals the editor's store (or not). |
| `order_stability` | The order of `schema.required` is unchanged (checked only when its elements are unchanged). |
| `gui_validation_agreement` | For a root-level property, the GUI error icon is shown exactly when its value violates its schema (or not). |

Candidates are kept per action category and editor, e.g. `add_item_required@schema`. An undo immediately followed by its redo (or the reverse) is also evaluated as one step under `undo_then_redo@<editor>` and `redo_then_undo@<editor>`. This pairing is used for mining only, not for live checking. Rule descriptions are generated by plain string templates, not by an LLM.

**Verdicts** (per dataset, then across datasets):

| Verdict | Meaning |
|---|---|
| `PROMOTABLE` | Enough observations in every dataset and at least `promotion_threshold` support for the same direction. |
| `BELOW_THRESHOLD` | Enough observations, but support is too low in some dataset. |
| `CONTRADICTORY_ACROSS_DATASETS` | Datasets support opposite directions. |
| `INSUFFICIENT_EVIDENCE` | Some dataset has too few observations. |

Promotable candidates are exported to `inferred_rules.json` (not for the `other@` and `@unknown` categories), each with its per-dataset evidence.

**Outputs.** `reports/invariant_report.md` (rules grouped into statement families) and `reports/invariant_dashboard.html` (offline dashboard from `inference_dashboard.py`, with rule-ID search and a grouped/flat toggle). Options: `--rules`, `--report`, `--dashboard`, `--output-descriptions`, `--config`, `--snapshots-dir`.

## Configuration files

All in `tests/mutation_testing/config/`. There are no magic numbers in code; tunable values live here.

| File | Controls |
|---|---|
| `run_config.json` | One loop run: `action_weights_profile`, `schema`, `num_steps`, `screenshots`, `run_mode`. A missing file, bad JSON or unknown value falls back to a default and prints why. Keys starting with `_` are notes. |
| `action_weights*.json` | The weight tree (Mode, View, action type). Profiles: `default`, `data_focus`, `schema_focus`, `textview_focus`, `random` (equal weights). |
| `triage_config.json` | Issue report and triage report: invariant descriptions, retired invariants, `causal` and `issue_report` blocks. |
| `meta_schema.json` | Vendored copy of MetaConfigurator's meta-schema, read by `dom_discoverer.py`. Input data, not a setting. |
| `inference_config.json` | Every number the inference engine and dashboard use. No defaults in code, so a missing parameter is an error. Profiles `default`, `strict`, `lenient` (default plus overrides). |

A new weights profile needs `config/action_weights_<name>.json` and an entry in `_KNOWN_PROFILES` in `profile_config.py`.

## Output folders

Created in the directory the commands are run from. `run_id` is the start time, `YYYYMMDD_HHMMSS`.

| Folder or file | Content |
|---|---|
| `mutation_history/<run_id>/history.json` | The run: steps, seeds, inputs, settings. Input for replay and reports. |
| `mutation_logs/<run_id>/mutation_log.html` | Step-by-step HTML log. |
| `mutation_screenshots/<run_id>/` | One screenshot per step. |
| `mutation_terminal.log` | Console output of the last run. |
| `invariant_miner_traces/<dataset>.jsonl` | State snapshots, appended after every step. |
| `replay_snapshots/<run_id>/` | Before/after state of failing steps. |
| `replay_logs/` | Replay sessions, including `replay_log.html`. |
| `mutation_history_with_causes/` | Histories enriched by `--export-cause-history`. |
| `reports/` | Issue report, triage report, inference report and dashboard. |

## Limitations and open items

- **Undo and redo.** They act on a stack the harness cannot see, and the loop skips the temporal check after them.
- **Silent no-ops.** Some Text View actions and the undo/redo clicks do nothing when their target is missing and raise no error. Replay catches these through its state-hash check.
- **GUI add-property action.** `add_property@schema` can hit a row that is already filled (e.g. a `oneOf` selector), add nothing, and still be logged as executed. This produces many false "Text View differs from schema" findings. A fix (check that the target's property count grew, otherwise log a no-op) is planned.
- **Coverage gaps.** The Diagram View and Settings mode are not explored. INV-D3, D5 and D6 check top-level properties only.
- **Inferred rules.** Requiring agreement across datasets removes coincidences of one random walk, not a real bug that all datasets share; promoted rules are for review, not ground truth. Support is counted over steps, not runs, rules are not conditioned on the target path, and many candidates have too few observations to judge.
- **Issue grouping.** Two different bugs with the same rule and trigger can share a row, and one bug triggered by different actions can appear as several rows.
- **Not implemented here.** LLM-guided natural-language invariant and test generation (the third thesis component) is not part of this code.

## File map

| File | Role |
|---|---|
| `mutation_loop.py` | The loop, weighted action selection, repair, history export. |
| `dom_discoverer.py` | Action discovery, value generation, execution. |
| `schema_path_enumerator.py` | What can still be added at a node of a schema. |
| `profile_config.py` | Reads `config/run_config.json`. |
| `config/` | Run settings, weight profiles, triage and inference parameters. |
| `replay_runner.py` | Replay, state snapshots, cause-tracking export. |
| `issue_report.py` | The distinct problems across runs. |
| `analyze_mutation_logs.py`, `causal_analysis.py` | Run loading, episodes, action labels, cause analysis; the older triage report. |
| `overnight_runner.py` | Batch runs. |
| `log_exporter.py` | HTML step log. |
| `invariant_inference_engine.py`, `inference_config.py` | Templates, verdicts, rule export. |
| `inference_dashboard.py` | Inference dashboard. |
| `test_case_reducer.py` | ddmin reduction (parked). |
| `../invariants/invariants_core.py` | Single-state invariants. |
| `../invariants/temporal_invariants.py` | Temporal and inferred invariants. |
| `../shared/python/` | Helpers mirroring the app's Playwright test utilities, including the test-panel reader. |