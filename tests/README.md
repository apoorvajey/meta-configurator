# Mutation testing for MetaConfigurator

A Playwright loop makes random, schema-aware edits in MetaConfigurator and checks invariants after every step. Failing runs can be replayed and are grouped into a problem report. An inference engine mines further invariants from the recorded runs.

## Setup

Python 3.10+, run from the repository root.

```
pip install playwright deepdiff rstr jsonschema referencing pyyaml
playwright install chromium
pip install pillow matplotlib   # optional
```

The app under test is `LIVE_BASE_URL` in `tests/shared/python/utils.py` (default `https://www.metaconfigurator.org`). For a local build, run `npm run dev` in `meta_configurator` and set it to `http://localhost:5173`.

## Usage

```
python -m tests.cli [command] [options]    # no command: menu
```

| Command | Purpose |
|---|---|
| `run` | One run (settings in `config/run_config.json`). `--runs N` or `--max-minutes M`: unattended batch, then the report. |
| `report` | Problem report. `--last-hours`, `--since`, `--until`. |
| `replay` | Re-run a recorded run and compare. `--history`, `--until N` (step), `--interactive`, `--headless`; `--since` / `--folder` for several runs. |
| `infer` | Mine invariants from the traces. |
| `review` | Review inferred rules: `html`, `import`, `export` or `import-sheet`. Only accepted rules are checked in the loop. |
| `sweep` | Show how the promotion threshold changes the mined invariants. |
| `archive` | Archive or drop traces (`--list`, `--log`, `--drop-before`, `--drop-runs`, `--drop-incomplete`). |
| `corpus` | HTML report of the runs in the traces. |
| `invariants` | List loaded invariants (`--mode data\|schema\|any\|temporal`). |

`--help` after a command lists its options.

## Notes

- A new invariant is one file in `invariants/state/<data_editor|schema_editor|any_editor>/` or `invariants/temporal/<action>/` with `ID`, `NAME` and `check(ctx)`. A new inference template is one file in `mutation_testing/inference/templates/` with `NAME` and `RULE`. Both load automatically.
- Settings are in `mutation_testing/config/`. A new action-weights profile also needs an entry in `_KNOWN_PROFILES` in `loop/profile_config.py`.
- Output goes to `tests/output/`: `runs/<run_id>/` (history, logs, screenshots, replays; `_STATUS__<tag>.json` gives the status), `traces/`, `inferred_rules.json` and `reports/`.

## Known limitations

- Undo and redo are not visible to the harness, so no temporal invariant is checked right after them.
- The Diagram View and Settings mode are not explored. 
