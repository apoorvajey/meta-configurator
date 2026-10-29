# Mutation testing for MetaConfigurator

A Playwright loop makes random, schema-aware edits in MetaConfigurator and checks invariants after every step. Failures are reported, reduced to short sequences and replayed as regression tests in CI. An inference engine mines more invariants from the recorded runs; you review them before they are checked.

Scope: GUI View and Text View of the Data Editor and Schema Editor. Other views and the Settings mode are not explored.

## Setup

Python 3.10+. Run everything from the repository root.

```
pip install -r tests/requirements.txt
playwright install chromium
pip install pillow matplotlib      # optional: report screenshots, `sweep` figure
```

App under test: `"app"` in `tests/mutation_testing/config/run_config.json` = `hosted` (default, https://www.metaconfigurator.org) | `local` | a URL. `METACONFIGURATOR_URL` overrides it.
For `local`, keep this running in a second terminal (address: `local_url`, default `http://localhost:5173`):

```
cd meta_configurator && npm install && npm run dev
```

## Full workflow

```
# 1. Explore: batch of runs, then the problem report opens
python -m tests.cli run --runs 15                      # or --max-minutes 480

# 2. Log a failure that is a real bug (page opens: edit, click Update, press Enter here)
python -m tests.cli bugs

# 3. Reduce it and add it to the regression manifest (5 of 5 replays must agree)
python -m tests.cli reduce BUG-12                      # no id: shows where every bug stands

# 4. Mine and review invariants from the recorded runs
python -m tests.cli infer
python -m tests.cli review                             # page opens: decide, click Download, press Enter here
python -m tests.cli run --runs 15                      # accepted rules are now checked in the loop

# 5. Regression check on the hosted site and the local build
python -m tests.cli regression --app both --repeat 5
python -m tests.cli regression-report

# 6. Numbers for the evaluation
python -m tests.cli stats                              # lists: rules, bugs, usage, yield, sweep, corpus
```

After a bug is fixed, its manifest entry fails on purpose. Change its `"expect"` in `tests/regression/manifest.json` from `"known-bugs"` to `"clean"`. CI (`.github/workflows/regression-replays.yml`) replays every entry once per push.

## Commands

`python -m tests.cli` opens a menu. `python -m tests.cli <command> --help` shows options.

| Command | Purpose |
|---|---|
| `run` | One run. `--runs N` / `--max-minutes M`: batch, then report (`--no-report` skips it) |
| `report` | Problem report. `--last-hours`, `--since`, `--until`, `--last-runs` |
| `replay` | Replay a recorded run. `--history`, `--until N`, `--interactive`, `--headless`, `--since`, `--folder` |
| `bugs` | Known-bugs page; `bugs import [--all]` applies a downloaded update |
| `reduce` | Reduce a bug's run (ddmin) and admit it. `--app`, `--all`, `--reduce-only`, `--admit-only` |
| `regression` | Replay the manifest. `--app hosted\|local\|both`, `--repeat N`, `--only <entry>`; exit code 1 if an entry differs |
| `regression-report` | HTML report of saved regression runs |
| `infer` | Mine rules from the traces. `--export-only` rewrites `inferred_rules.json` from your review |
| `review` | Review inferred rules; only accepted rules are checked |
| `sweep` | Effect of the promotion threshold |
| `stats` | `rules`, `bugs`, `usage`, `yield`, `sweep`, `corpus` |
| `archive` | `--label`, `--list`, `--drop-before`, `--drop-runs`, `--drop-incomplete`, `--dry-run` |
| `corpus` | HTML report of the recorded traces |
| `invariants` | List all invariants (hand-written and inferred) |

## Configuration (`tests/mutation_testing/config/`)

| File | Content |
|---|---|
| `run_config.json` | `app`, `action_weights_profile` (`textview_focus` \| `schema_focus` \| `data_focus` \| `uniform` \| `random`), `schema` (`random` or one of Autonomous Vehicle, Feature Testing, Strenda, EnzymeML), `num_steps`, valid-value probabilities, `locality_bias`, `loop_limits`, `screenshots` (`every_step` \| `on_failure` \| `off`), `run_mode` |
| `inference_config.json` | Promotion threshold 0.85, minimum observations, guards; profiles `strict`, `lenient` |
| `triage_config.json` | Known bugs and report settings (edit through `bugs`) |
| `rule_reviews/` | Review decisions; the newest file is used |

## Extending

- **Invariant:** one file in `invariants/state/<data_editor|schema_editor|any_editor>/` or `invariants/temporal/<action>/` with `ID`, `NAME`, `check(ctx)`. Loaded automatically.
- **Inference template:** one file in `mutation_testing/inference/templates/` with `NAME`, `RULE`.
- **Weights profile:** new `action_weights_<name>.json` plus an entry in `_KNOWN_PROFILES` in `loop/profile_config.py`.
- **Command:** one line in `COMMANDS` and `COMMAND_HELP` in `cli.py`.

## Output (`tests/output/`, not in Git)

`runs/<run_id>/` (history, logs, screenshots, replays, `_STATUS__<tag>.json`) · `traces/` (input of `infer`) · `inferred_rules.json` (rules the loop checks) · `reports/` · `regression_runs/` · `bug_updates/`, `config_backups/`

Regression details: [`regression/README.md`](regression/README.md).