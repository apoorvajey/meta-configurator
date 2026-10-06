# Mutation testing for MetaConfigurator

A Playwright loop drives the real MetaConfigurator app with random, schema-aware edits. After every step it checks the app's internal state against **invariants** (statements that must always hold, e.g. "the Text View shows the same content as the internal data"). Failing runs are saved, can be replayed and are grouped into a problem report. A separate engine mines further invariants from the recorded runs.

## Setup

Python 3.10+. Run everything from the repository root.

```
pip install playwright deepdiff rstr jsonschema referencing pyyaml
playwright install chromium
pip install pillow   # optional: shrinks report screenshots
```

The target app is `LIVE_BASE_URL` in `tests/shared/python/utils.py` (default `https://www.metaconfigurator.org`). For a local build, run `npm run dev` in `meta_configurator` and set it to `http://localhost:5173`.

## Usage

```
python -m tests.cli                    # interactive menu
python -m tests.cli <command> [options]
```

| Command | Purpose |
|---|---|
| `run` | One mutation-loop run, configured by `config/run_config.json`. |
| `run --runs N` | `N` unattended runs, then the report for those runs (`--no-report`, `--timeout`, `--datasets`, `--shuffle-laps`). |
| `report` | Problem report across recorded runs → `reports/issues.html` (`--last-hours`, `--since`, `--until`, `--no-open`). |
| `replay` | Re-execute a recorded run (`--history <file>`, `--until N`, `--interactive`, `--pause-on-failure`). |
| `infer` | Mine invariants from recorded traces (`--profile`, `--threshold`, `--min-obs`, `--datasets`). |
| `sweep` | Show how the promotion threshold changes the mined invariants. |
| `invariants` | List all loaded invariants (`--mode data\|schema\|temporal`). |

Options are passed through to the tool behind the command; add `--help` for the full list. Typical overnight session:

```
python -m tests.cli run --runs 24
python -m tests.cli infer
```

## How it works

- **Loop.** Loads one schema and its data (Autonomous Vehicle, Feature Testing, Strenda, EnzymeML), then performs `num_steps` actions found via the app's `data-testid` attributes, in the GUI View and Text View of both the Data and Schema Editor. Mode, view, action type and element are drawn by the weights in `config/action_weights*.json`; values come from the governing schema, including deliberately invalid ones. A failure that persists for two steps triggers repair by undo. Every step records its seed and input.
- **Invariants.** *State* invariants check one snapshot, *temporal* invariants compare the state before and after an action, *inferred* invariants are loaded from `inferred_rules.json`. One file per invariant in `tests/invariants/`.
- **Replay.** Re-runs a history in a fresh browser with the same seeds and inputs and reports the first step where it diverges. Before/after states of failing steps go to `replay_snapshots/<run_id>/`.
- **Report.** Groups failures by the rules that broke and the triggering action. Each problem lists example runs (shortest first), a replay command, a drafted GitHub issue and screenshots.
- **Inference.** Instantiates rule templates (stability, change count, key correlation, format agreement, …) over `invariant_miner_traces/<dataset>.jsonl`. A rule is exported once its support reaches `promotion_threshold` in every dataset. Exported rules are candidates for review, not ground truth.

## Layout

```
tests/
├── cli.py                   entry point
├── invariants/              one file per invariant (state/ and temporal/), plus the loaders
├── mutation_testing/
│   ├── mutation_loop.py     the loop (loop/: weights, repair, recording, setup)
│   ├── discovery/           finds available actions and generates values
│   ├── actions/             executes actions in the GUI View and Text View
│   ├── replay_runner.py     replay (replay/: history I/O, reproducibility)
│   ├── issue_report.py      problem report (triage/: loading and grouping)
│   ├── invariant_inference_engine.py   inference (inference/templates/: one file per template)
│   └── config/              settings, meta-schema, test data
└── shared/                  Playwright helpers (TypeScript, plus Python mirrors in python/)
```

The TypeScript helpers are shared with the MetaConfigurator E2E and relay tests. A new invariant is one file in `invariants/state/<data_editor|schema_editor>/` or `invariants/temporal/<action>/` defining `ID`, `NAME` and `check(ctx)`; it is loaded automatically (see the loader docstrings). A new inference template is one file in `inference/templates/` defining `NAME` and `RULE`; it is picked up automatically.

## Configuration

In `tests/mutation_testing/config/`:

| File | Controls |
|---|---|
| `run_config.json` | `schema`, `num_steps`, `screenshots`, `run_mode` (`background`, `watch_failures`, `interactive`), `action_weights_profile`. |
| `action_weights_<profile>.json` | Weights. Profiles: `default`, `data_focus`, `schema_focus`, `textview_focus`, `random`. A new profile also needs an entry in `_KNOWN_PROFILES` in `profile_config.py`. |
| `inference_config.json` | Inference thresholds; profiles `default`, `strict`, `lenient`. |
| `triage_config.json` | Report settings, rule descriptions, known bugs. |
| `meta_schema.json` | Copy of MetaConfigurator's meta-schema, used for action discovery. |

## Output

Written to the directory the commands are run from; `run_id` is the start time (`YYYYMMDD_HHMMSS`).

| Path | Content |
|---|---|
| `mutation_history/<run_id>/history.json` | Steps, seeds, inputs; input for replay and report. |
| `mutation_logs/<run_id>/mutation_log.html` | Step-by-step log. |
| `mutation_screenshots/<run_id>/` | One screenshot per step. |
| `invariant_miner_traces/<dataset>.jsonl` | Snapshots for inference. |
| `replay_snapshots/<run_id>/` | Before/after state of failing steps. |
| `reports/` | Problem report and inference reports. |

## Known limitations

- The undo/redo stack is not visible to the harness, so no temporal invariant is checked right after undo or redo.
- The Diagram View and Settings mode are not explored. INV-D3, D5 and D6 check top-level properties only.
- Inferred rules count support over steps, not runs, and are not conditioned on the target path.
