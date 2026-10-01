"""Replays a saved mutation-loop history to reproduce and diagnose failures.

By default the whole history is replayed in one quiet pass. When an invariant fails, the evidence
and a before/after diff are printed at that step.

    python -m tests.mutation_testing.replay_runner --latest
    python -m tests.mutation_testing.replay_runner --history history.json
    python -m tests.mutation_testing.replay_runner --history history.json --schema "Strenda Schema"
    python -m tests.mutation_testing.replay_runner --latest --until 50
    python -m tests.mutation_testing.replay_runner --latest --interactive
    python -m tests.mutation_testing.replay_runner --latest --no-state-snapshots
    python -m tests.mutation_testing.replay_runner --history mutation_history/<run_id>/history.json --export-cause-history

--latest (also the default) picks the newest mutation_history/<run_id>/history.json, relative to the
current directory. The schema comes from --schema, else from the history's meta; if neither is
available the replay stops and lists the valid options.

--until N and --pause-at N run quietly up to step N, then switch to manual step-by-step mode
([Enter] next, [q] quit). If both are given the earlier one applies.

For every failing step the full before/after app state is written to
replay_snapshots/<run_id>/step_<n>_{before,after}.json. analyze_mutation_logs picks these up for
the triage report. --no-state-snapshots turns this off.

--export-cause-history writes a copy of the history, enriched with the cause-tracking fields that
analyze_mutation_logs needs, to mutation_history_with_causes/. The original file is not modified.

A replay reuses the recorded step seeds, typed input values and element fields, and follows the
same page setup and checking rules as the loop. The first point where it provably leaves the
original run (state hash, action outcome or generated property name) is reported as a divergence.
Histories recorded without seeds replay on a best-effort basis.
"""

from __future__ import annotations

import contextlib
import io
import json
import hashlib
import argparse
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import sync_playwright

from tests.shared.python.utils import SessionMode
from tests.mutation_testing.dom_discoverer import InteractionExecutor, ElementDescriptor, js_click_for_testid
from tests.mutation_testing.mutation_loop import (
    SCHEMA_DATA_PAIRS,
    DATA_DIR,
    VIEWPORT,
    _resolve_governing_schema,
    _dataset_slug,
    build_snapshot,
    prepare_app_page,
    scroll_to_validation_error,
    setup_app_for_schema,  # re-exported for existing callers
)
from tests.invariants.invariants_core import InvariantChecker, _diff_nested
from tests.invariants.temporal_invariants import TemporalInvariantChecker

__all__ = [
    "DATA_DIR", "SCHEMA_CHOICES", "ReplayOutcome", "ReplayRunner",
    "ReproducibilityCheck", "check_reproducibility", "reproducibility_status",
    "export_history", "find_latest_history", "load_history", "resolve_schema_for_replay",
    "setup_app_for_schema",
]

_SCHEMA_NAME_TO_DATA_FILE: dict[str, str] = {name: path.name for name, path in SCHEMA_DATA_PAIRS}
SCHEMA_CHOICES: tuple[str, ...] = tuple(_SCHEMA_NAME_TO_DATA_FILE.keys())

_UNDO_REDO_TESTIDS = ("undo", "redo", "schema_undo", "schema_redo")

# run_id format used by run_demo()
_RUN_ID_RE = re.compile(r"^\d{8}_\d{6}$")
MUTATION_HISTORY_DIR = Path("mutation_history")

# Kept outside mutation_history/ so replay output is not mistaken for a live run.
DEFAULT_STATE_SNAPSHOT_DIR = "replay_snapshots"

# Enriched copies use the <run_id>/history.json layout that analyze_mutation_logs scans.
DEFAULT_CAUSE_HISTORY_DIR = "mutation_history_with_causes"


def find_latest_history(root: Path = MUTATION_HISTORY_DIR) -> Path | None:
    """Newest mutation_history/<run_id>/history.json, or None.

    Runs are ordered by the run_id in the folder name. Folders that are not named like a run_id, or
    that have no history.json, are skipped.
    """
    if not root.is_dir():
        return None
    candidates = [
        d / "history.json"
        for d in root.iterdir()
        if d.is_dir() and _RUN_ID_RE.match(d.name) and (d / "history.json").is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.parent.name)


def resolve_schema_for_replay(meta: dict, cli_schema: str | None) -> tuple[str, str]:
    """--schema if given, else the schema recorded in the history's meta. Raises if neither exists."""
    if cli_schema is not None:
        if cli_schema not in _SCHEMA_NAME_TO_DATA_FILE:
            raise ValueError(f"'{cli_schema}' is not recognized. Valid: {', '.join(SCHEMA_CHOICES)}")
        return cli_schema, _SCHEMA_NAME_TO_DATA_FILE[cli_schema]
    meta_schema, meta_data = meta.get("schema_option_name"), meta.get("data_file")
    if meta_schema and meta_data:
        return meta_schema, meta_data
    raise ValueError(
        "No schema/data metadata in this file and no --schema given - pass one of: "
        + ", ".join(SCHEMA_CHOICES))


def _derive_mutation_log_path(history_abs_path: Path) -> str | None:
    """Path of the mutation_log.html that belongs to this history.json, based on the
    mutation_history/<run_id>/ and mutation_logs/<run_id>/ folder convention.

    Returns None if the file is not named history.json, is not under mutation_history, or the derived
    log does not exist.
    """
    parts = history_abs_path.parts
    if history_abs_path.name != "history.json" or "mutation_history" not in parts:
        return None
    idx = parts.index("mutation_history")
    if idx + 1 >= len(parts) - 1:  # a run_id segment is needed between mutation_history/ and history.json
        return None
    run_id = parts[idx + 1]
    candidate = Path(*parts[:idx]) / "mutation_logs" / run_id / "mutation_log.html"
    return str(candidate) if candidate.exists() else None


_MISSING_TARGET_ERROR_SUBSTRINGS = (
    "Timeout", "exceeded", "waiting for locator", "resolved to 0 elements",
    "strict mode violation", "no element", "element is not visible",
)


def _looks_like_missing_target(error_msg: str | None) -> bool:
    """Heuristic. js_click actions (undo/redo, expand/collapse) do nothing on a missing element and raise
    no exception, so this cannot see them. The state-hash check in ReplayRunner.run() catches those.
    """
    if not error_msg:
        return False
    return any(s.lower() in error_msg.lower() for s in _MISSING_TARGET_ERROR_SUBSTRINGS)


def _is_repair_record(step: dict) -> bool:
    """Repair steps, mode-switch clicks and clear-text auto-undos use the "repair:" prefix. They get no
    temporal check and no seed.
    """
    return (step.get("action_id") or "").startswith("repair:")


def _replay_value(step: dict):
    """The value to pass to executor.execute() for this record.

    New records carry "input_value", which is exact and type-preserved (None included). For a rename
    the recorded "value" is the generated name and is forced so the replay reuses it. Legacy records
    only have the stringified "value", where "" means no value.
    """
    if "input_value" in step:
        value = step["input_value"]
        if value is None and step.get("testid", "").startswith("rename-") and step.get("value"):
            return step["value"]
        return value
    return step["value"] if step.get("value") else None


@dataclass
class ReproducibilityCheck:
    """Whether a history.json has everything needed to replay the run exactly.

    ok=True means the recorded data is complete. It does not mean a replay matched; that needs a
    replay with ReplayOutcome.diverged_at None. See reproducibility_status().
    """
    ok: bool
    reason: str
    action_steps: int  # non-repair steps with a testid
    missing_steps: list[int] = field(default_factory=list)  # 1-based step numbers with missing data

    @property
    def first_missing_step(self) -> int | None:
        return self.missing_steps[0] if self.missing_steps else None


def check_reproducibility(steps: list[dict]) -> ReproducibilityCheck:
    """Every action step (non-empty testid, action_id not starting with "repair:") needs an
    "input_value" key (its value may be None) and a non-null "step_seed". Repair steps are excluded.
    """
    action_steps = 0
    missing: list[int] = []
    for i, s in enumerate(steps):
        if not s.get("testid") or _is_repair_record(s):
            continue
        action_steps += 1
        if "input_value" not in s or s.get("step_seed") is None:
            missing.append(i + 1)

    if action_steps == 0:
        return ReproducibilityCheck(False, "no action steps recorded", 0, [])
    if not missing:
        return ReproducibilityCheck(True, "all action steps have recorded input values and seeds",
                                    action_steps, [])
    if len(missing) == action_steps:
        reason = ("log predates recorded input values/seeds - replay can only approximate "
                  "this run")
    else:
        reason = (f"{len(missing)}/{action_steps} action step(s) lack recorded input values/seeds "
                  f"(first at step {missing[0]}) - replay is not exact from that point on")
    return ReproducibilityCheck(False, reason, action_steps, missing)


def reproducibility_status(check: ReproducibilityCheck,
                           outcome: "ReplayOutcome | None" = None) -> str:
    """One of "not_reproducible" (recorded data incomplete), "unverified" (complete, not replayed),
    "verified" (replayed without divergence) or "diverged".
    """
    if not check.ok:
        return "not_reproducible"
    if outcome is None:
        return "unverified"
    return "verified" if outcome.diverged_at is None else "diverged"


def load_history(path: str | Path) -> tuple[list[dict], dict]:
    """Read a history.json into (steps, meta). Accepts the legacy bare-list format, which has no meta."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        return raw, {}
    return raw.get("steps", []), raw.get("meta", {}) or {}


@dataclass
class ReplayOutcome:
    """Result of one ReplayRunner.run() call, used by test_case_reducer to classify candidates.

    steps_run is the number of steps the call was asked to replay. quit_early and steps_completed say
    how far it actually got.
    """
    reproduced: bool
    reproduced_at_step: int | None
    invalid: bool
    invalid_reason: str | None
    steps_run: int
    failing_steps: int = 0  # steps where any invariant failed
    diverged_at: int | None = None  # first display step where the replay left the original run
    state_snapshots_saved: int = 0  # failing steps whose before/after state was written to disk
    quit_early: bool = False  # the user pressed 'q' before the end
    steps_completed: int = 0  # steps run before returning


def _state_hash(snapshot: dict | None) -> str | None:
    if not snapshot:
        return None
    core = {k: snapshot.get(k) for k in ("de_data", "de_schema", "se_schema", "se_data")}
    return hashlib.sha1(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()


def _dump_state_snapshot(snapshot: dict | None, path: Path) -> None:
    """Write a full build_snapshot() dict to disk."""
    if snapshot is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, default=str)


# written into history.json, read by causal_analysis
_FACET_KEYS = ("de_data", "de_schema", "se_schema", "se_data", "de_text", "se_text")
_FACET_HASH_CHARS = 10
_MAX_EVIDENCE_PATHS = 20
_MAX_EVIDENCE_MESSAGE_CHARS = 300


def _facet_hashes(snap: dict) -> dict:
    return {k: hashlib.sha1(json.dumps(snap[k], sort_keys=True, default=str).encode()).hexdigest()[:_FACET_HASH_CHARS]
            for k in _FACET_KEYS if k in snap}


def _failure_paths(details) -> list:
    """Paths or keys named by a failed invariant, whatever shape its details have."""
    paths: list = []

    def add(p):
        p = "" if p is None else str(p)
        if p and p not in paths:
            paths.append(p)

    if not isinstance(details, dict):
        return paths
    for m in details.get("mismatches") or []:
        if not isinstance(m, dict):
            continue
        prop = m.get("property") or m.get("key") or m.get("path")
        add(prop)
        for v in m.get("reason") or []:
            vp = v.get("path") if isinstance(v, dict) else None
            if vp and vp not in ("(root)", "(unresolved)"):
                add(f"{prop}.{vp}" if prop else vp)
    add(details.get("path"))
    for v in details.get("expected_violations") or []:
        if isinstance(v, dict):
            add(v.get("path"))
            add(v.get("key"))
    for left in ("internal_state", "internal_schema"):
        if left in details and "text_view" in details:
            for p, _, _ in _diff_nested(details[left], details["text_view"]):
                add(p)
    return paths[:_MAX_EVIDENCE_PATHS]


def _cause_fields(report, snap: dict | None) -> dict:
    """Cause-tracking fields for one step, shared by export_history() and export_enriched_history()."""
    out: dict = {}
    if report is not None:
        out["checked_invariants"] = [r.name.partition(" ")[0] for r in report.results]
        out["failed_invariants"] = [r.name for r in report.failures]
        out["failure_evidence"] = [
            {"id": r.name.partition(" ")[0],
             "message": (r.message or "")[:_MAX_EVIDENCE_MESSAGE_CHARS],
             "paths": _failure_paths(r.details)}
            for r in report.failures]
        out["applicability"] = {
            r.name.partition(" ")[0]: r.details["violations_present"]
            for r in report.results if "violations_present" in r.details
        }
    out["facet_hashes"] = _facet_hashes(snap or {})
    return out

def export_enriched_history(source_path: Path, cause_records: dict[int, dict],
                            out_root: str | Path = DEFAULT_CAUSE_HISTORY_DIR,
                            run_id: str | None = None) -> Path:
    """Write a copy of a history.json to <out_root>/<run_id>/history.json with the cause-tracking fields
    merged into each replayed step.

    cause_records maps a step's 0-based position in the original list to its fields. Existing fields
    are kept, so analyze_mutation_logs builds the same episodes as for the original run. Two fields
    are added: replay_passed (invariants passed on this replay) and replay_faithful (False from the
    first divergence onwards). Steps the replay did not reach are left unchanged.
    """
    with open(source_path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        raw = {"meta": {}, "steps": raw}
    meta = raw.setdefault("meta", {}) or {}
    raw["meta"] = meta
    steps = raw.get("steps", [])

    resolved = meta.setdefault("resolved_settings", {}) or {}
    meta["resolved_settings"] = resolved
    run_id = run_id or resolved.get("run_id") or Path(source_path).resolve().parent.name
    resolved.setdefault("run_id", run_id)

    for idx, fields in cause_records.items():
        if 0 <= idx < len(steps) and isinstance(steps[idx], dict):
            steps[idx].update(fields)
    meta["cause_tracking"] = {
        "source_history": str(Path(source_path).resolve()),
        "steps_enriched": len(cause_records),
        "steps_total": len(steps),
        "steps_unfaithful": sum(1 for r in cause_records.values() if r.get("replay_faithful") is False),
    }

    out_path = Path(out_root) / str(run_id) / "history.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, default=str)
    return out_path


def export_history(history: list, path: str = "history.json",
                   schema_option_name: str | None = None,
                   data_file: str | None = None,
                   run_config: dict | None = None,
                   resolved_settings: dict | None = None,
                   save_state_snapshots: bool = True,
                   state_snapshot_dir: str | Path = DEFAULT_STATE_SNAPSHOT_DIR) -> None:
    """Export a MutationLoop history to JSON.

    Repair records carry "role" and "target_index" (for walk-back repairs, the earlier position they
    aim to return to). test_case_reducer uses them to keep a repair block together.

    Replay fields, absent for repair steps and older histories: step_seed, input_value, el_interaction,
    el_tag, el_role, el_view (diagnostic only) and step.

    run_config is the raw run_config.json; resolved_settings holds the values profile_config picked
    from it. replay's main() reads screenshots_enabled and run_id from there.

    With save_state_snapshots (default) the full before/after state of each failing step is written to
    state_snapshot_dir/<run_id>/, built from the in-memory history.
    """
    def _snap_of(entry) -> dict | None:
        return getattr(entry, "snapshot", None)

    run_id = (resolved_settings or {}).get("run_id") or "unknown_run"
    snap_dir = Path(state_snapshot_dir) / str(run_id)
    snapshots_saved = 0

    records = []
    for i, s in enumerate(history):
        snap = getattr(s, "snapshot", None) or {}
        rec = {
            "action_id": s.action_id,
            "description": s.description,
            "testid": s.kwargs.get("testid", ""),
            "value": s.kwargs.get("value", ""),
            "mode": s.report.mode if s.report else "",
            "action_succeeded": s.action_succeeded,
            "passed": s.invariants_passed,
            "role": s.kwargs.get("role"),
            "target_index": s.kwargs.get("target_index"),
            "el_mode": s.kwargs.get("el_mode"),
            "el_interaction": s.kwargs.get("el_interaction"),
            "el_tag": s.kwargs.get("el_tag"),
            "el_role": s.kwargs.get("el_role"),
            "el_view": s.kwargs.get("el_view"),
            "resolved_property_name": s.kwargs.get("resolved_property_name"),
            "step_seed": s.kwargs.get("step_seed"),
            "step": snap.get("step"),
            "state_hash": _state_hash(snap),
        }
        rec.update(_cause_fields(getattr(s, "report", None), snap))
        if "input_value" in s.kwargs:
            rec["input_value"] = s.kwargs["input_value"]

        report = getattr(s, "report", None)
        if save_state_snapshots and report is not None and not report.passed:
            before = _snap_of(history[i - 1]) if i > 0 else None
            after = _snap_of(s)
            _dump_state_snapshot(before, snap_dir / f"step_{i}_before.json")
            _dump_state_snapshot(after, snap_dir / f"step_{i}_after.json")
            if after is not None:
                snapshots_saved += 1

        records.append(rec)

    payload = {
        "meta": {
            "schema_option_name": schema_option_name,
            "data_file": data_file,
            "run_config": run_config,
            "resolved_settings": resolved_settings,
        },
        "steps": records,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\nHistory exported → {path}")
    print(f"   {len(records)} steps saved")
    if save_state_snapshots and snapshots_saved:
        print(f"   State snapshots for {snapshots_saved} failing step(s) → {snap_dir}")
    if schema_option_name:
        print(f"   Schema/data pair: {schema_option_name} / {data_file}")
    if resolved_settings:
        print("   Run settings recorded:")
        for k, v in resolved_settings.items():
            print(f"     {k}: {v}")
    print(f"   Replay with: python -m tests.mutation_testing.replay_runner --history {path}")
    print("   (or, while this is the newest run: python -m tests.mutation_testing.replay_runner --latest)")


class ReplayRunner:

    def __init__(self, page, history: list[dict], interactive: bool = False, pause_at: int | None = None,
                 dataset: str | None = None, run_id: str | None = None,
                 mirror_screenshot_scroll: bool = True):
        """dataset and run_id are stamped onto replayed snapshots, as the loop does for its own.

        mirror_screenshot_scroll repeats the loop's scroll to the first validation-error icon before a
        failing step's screenshot. Set it to the original run's screenshots_enabled.
        """
        self.page = page
        self.history = history
        self.interactive = interactive
        self.pause_at = pause_at
        self.dataset = dataset
        self.run_id = run_id
        self.mirror_screenshot_scroll = mirror_screenshot_scroll
        self.executor = InteractionExecutor(page)
        self._prev_snapshot: dict | None = None

    def _element_for_step(self, step: dict) -> ElementDescriptor:
        """Rebuild the ElementDescriptor from the record's el_* fields, inferring from the testid for
        legacy records. `view` is a read-only property and comes out the same once testid and mode match.
        """
        testid = step["testid"]
        el = ElementDescriptor(
            testid=testid,
            tag=step.get("el_tag") or "",
            role=step.get("el_role") or "",
            interaction=step.get("el_interaction") or self._infer_interaction(testid),
            label=testid,
        )
        el.mode = step.get("el_mode") or el.mode
        el.js_click = js_click_for_testid(testid)
        return el

    def _capture_step_screenshot(self, screenshot_dir: str, display_i: int) -> str | None:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        shot_path = f"{screenshot_dir}/step_{display_i + 1:03d}.png"
        try:
            self.page.screenshot(path=shot_path, full_page=True)
            return shot_path
        except Exception as e:
            print(f"   Screenshot failed: {e}")
            return None

    def run(self, until: int | None = None, include: list[int] | None = None,
            pause_on_failure: bool = True, pause_at_end: bool = True,
            export_log: bool = True, log_path: str = "replay_log.html", verbose: bool = True,
            analyze_failures: bool = True,
            capture_screenshots: bool = True, screenshot_dir: str = "replay_screenshots",
            progress_every: int = 10,
            target_invariant_name: str | None = None,
            source_history_path: str | None = None,
            source_mutation_log_path: str | None = None,
            schema_option_name: str | None = None,
            data_file_name: str | None = None,
            save_state_snapshots: bool = True,
            state_snapshot_dir: str = DEFAULT_STATE_SNAPSHOT_DIR,
            collect_cause_records: dict[int, dict] | None = None) -> ReplayOutcome:
        """analyze_failures: when verbose is False, a failure is only printed in full if it is new or has
        changed (message or details) since that invariant last failed. Identical repeats are counted and
        summarised per invariant at the end.

        capture_screenshots: one screenshot per step, whether it passed or failed.

        log_path: where the HTML log is written when export_log is True.

        source_history_path, source_mutation_log_path: shown in the HTML log header. Optional.

        save_state_snapshots, state_snapshot_dir: for each step that fails an invariant in this replay,
        the before/after snapshots are written to state_snapshot_dir/<run_id>/step_<orig_idx>_before.json
        and _after.json, where orig_idx is the position in the original steps list. "Before" is the
        previous snapshot, None for the first step or after a failed action.

        collect_cause_records: if a dict is passed it is filled with {orig_idx: cause fields} for every
        replayed step.

        When pause_at is reached, self.interactive is switched on mid-run and the per-step printing and
        the final prompt follow.

        The first step where the replay leaves the original run is printed as "REPLAY DIVERGED" and
        returned as diverged_at. It is detected from the recorded state hash, an action that succeeded in
        one run and failed in the other, or a different generated property name.
        """
        from tests.mutation_testing.mutation_loop import StepResult
        from tests.mutation_testing.log_exporter import export_html_log

        if include is not None:
            indexed_steps = [(idx, self.history[idx]) for idx in include]
        else:
            end = until if until else len(self.history)
            indexed_steps = list(enumerate(self.history[:end]))

        total = len(indexed_steps)
        if verbose:
            print(f"\n=== Replaying {total} steps ===")
            if self.interactive:
                print("   [Enter] Next step   [N] Run automatically until step N, then pause   [q] Quit\n")

        repro = check_reproducibility([s for _, s in indexed_steps])
        if not repro.ok:
            print(f"   Not reproducible from this log: {repro.reason}")

        replay_history = []
        reproduced = False
        reproduced_at_step: int | None = None
        invalid = False
        invalid_reason: str | None = None
        failing_steps = 0
        state_snapshots_saved = 0
        last_signature: dict[str, tuple[str, str]] = {}
        first_seen_step: dict[str, int] = {}
        last_seen_step: dict[str, int] = {}
        occurrence_count: dict[str, int] = {}
        choice = ""
        diverged_at: int | None = None
        quit_early = False
        steps_completed = 0

        for display_i, (orig_idx, step) in enumerate(indexed_steps):
            # set before any continue/break, including steps skipped for having no testid
            steps_completed = display_i + 1
            testid = step["testid"]
            value = _replay_value(step)
            mode = step["mode"]
            desc = step["description"]
            orig_succeeded = step["action_succeeded"]
            passed = step["passed"]
            is_repair = _is_repair_record(step)

            # self._prev_snapshot is overwritten later in this iteration
            step_before_snapshot = self._prev_snapshot
            curr_snapshot = None

            orig_status = "✓" if passed else "✗" if orig_succeeded else ""

            # switch to manual mode before this step prints, so it gets full detail
            if self.pause_at and display_i + 1 >= self.pause_at:
                self.interactive = True

            show = verbose or self.interactive

            if show:
                print(f"\n--- Step {display_i + 1}/{total} (orig index {orig_idx}) [orig: {orig_status}] ---")
                print(f"   {desc}")
                print(f"   testid: {testid}  value: {repr(value)}  mode: {mode}"
                      + (f"  seed: {step['step_seed']}" if step.get("step_seed") is not None else ""))
            elif progress_every and (display_i + 1) % progress_every == 0:
                print(f"   ... step {display_i + 1}/{total} ...")

            if not testid:
                if show:
                    print("   No testid - skipping")
                continue

            el = self._element_for_step(step)

            if testid.startswith("rename-property-") and show:
                print(f"   [replay-debug] forcing rename to value={value!r}")

            action_succeeded = True
            action_error = None
            try:
                # resolved before seeding, in the same order as the loop
                governing_schema = _resolve_governing_schema(self.page, el.mode)
                step_seed = step.get("step_seed")
                if step_seed is not None:
                    random.seed(step_seed)
                # InteractionExecutor prints its own diagnostics; silence them
                if show:
                    self.executor.execute(el, value, schema=governing_schema)
                    print("   ✓ Executed")
                else:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.executor.execute(el, value, schema=governing_schema)
            except Exception as e:
                action_succeeded = False
                action_error = str(e)
                print(f"   Execution error at step {display_i + 1} (testid={testid}): {e}")
                if _looks_like_missing_target(action_error) and not invalid:
                    invalid = True
                    invalid_reason = (f"step {display_i + 1} (orig index {orig_idx}, "
                                      f"testid={testid}): {action_error[:200]}")

            if diverged_at is None and action_succeeded != orig_succeeded:
                diverged_at = display_i + 1
                print(f"\n   REPLAY DIVERGED at step {display_i + 1} (orig index {orig_idx}, "
                      f"testid={testid}): action {'succeeded' if orig_succeeded else 'failed'} "
                      f"in the original run but {'succeeded' if action_succeeded else 'failed'} "
                      f"on replay. Everything after this is not a faithful replay.")

            expected_name = step.get("resolved_property_name")
            if action_succeeded and expected_name and testid.startswith("add-property-"):
                actual_name = getattr(self.executor, "_last_added_property_name", None)
                if actual_name != expected_name:
                    print(f"   add-property created {actual_name!r} on replay, but the original "
                          f"run created {expected_name!r} - later steps that target it will miss.")
                    if diverged_at is None:
                        diverged_at = display_i + 1
                        print(f"\n   REPLAY DIVERGED at step {display_i + 1} (orig index {orig_idx}, "
                              f"testid={testid}): generated property name differs.")

            # the loop only checks invariants after a successful action
            report = None
            if action_succeeded:
                try:
                    report = InvariantChecker(self.page, SessionMode.DataEditor).run_all()
                    # decided on the single-snapshot report, before temporal results are merged (as in the loop)
                    if self.mirror_screenshot_scroll and not report.passed:
                        scroll_to_validation_error(self.page)
                except Exception as e:
                    print(f"   Invariant check error at step {display_i + 1}: {e}")
                    report = None
            elif show:
                print("   Invariant check skipped - action failed (the original run skips it too)")

            shot_path = self._capture_step_screenshot(screenshot_dir, display_i) if capture_screenshots else None

            if action_succeeded:
                try:
                    curr_snapshot = build_snapshot(
                        self.page, testid, step.get("step") or orig_idx + 1,
                        self.run_id, self.dataset,
                    )
                    expected_hash = step.get("state_hash")
                    if expected_hash and diverged_at is None and _state_hash(curr_snapshot) != expected_hash:
                        diverged_at = display_i + 1
                        print(f"\n   REPLAY DIVERGED at step {display_i + 1} (orig index {orig_idx}, "
                              f"testid={testid}): state differs from the original run. "
                              f"Everything after this is not a faithful replay.")

                    prev_action = (self._prev_snapshot or {}).get("action", "")
                    if is_repair:
                        if show:
                            print("   Temporal check skipped - repair/auto-undo step "
                                  "(the original run never checks these)")
                    elif prev_action in _UNDO_REDO_TESTIDS:
                        if show:
                            print(f"   Temporal check suppressed - previous action was '{prev_action}'")
                    elif self._prev_snapshot is not None and report is not None:
                        temporal_report = TemporalInvariantChecker(
                            prev=self._prev_snapshot, curr=curr_snapshot, action=testid).run_all()
                        if temporal_report.results:
                            report.results.extend(temporal_report.results)
                    self._prev_snapshot = curr_snapshot
                except Exception as e:
                    print(f"   Snapshot/temporal check error at step {display_i + 1}: {e}")
                    self._prev_snapshot = None
            else:
                # the loop stores None here for a failed step, so the next step skips its temporal check
                self._prev_snapshot = None

            if collect_cause_records is not None:
                cause = _cause_fields(report, curr_snapshot)
                cause["replay_passed"] = None if report is None else report.passed
                cause["replay_faithful"] = diverged_at is None or display_i + 1 < diverged_at
                collect_cause_records[orig_idx] = cause

            changed = True
            if report is not None:
                if not report.passed:
                    failing_steps += 1

                    if save_state_snapshots:
                        snap_dir = Path(state_snapshot_dir) / (self.run_id or "unknown_run")
                        _dump_state_snapshot(step_before_snapshot, snap_dir / f"step_{orig_idx}_before.json")
                        _dump_state_snapshot(curr_snapshot, snap_dir / f"step_{orig_idx}_after.json")
                        if curr_snapshot is not None:
                            state_snapshots_saved += 1

                if show:
                    report.print()
                    if shot_path:
                        print(f"   Screenshot: {shot_path}")
                elif analyze_failures and not report.passed:
                    changed = False
                    for r in report.failures:
                        sig = (r.message, repr(r.details))
                        if last_signature.get(r.name) != sig:
                            changed = True
                        last_signature[r.name] = sig
                        occurrence_count[r.name] = occurrence_count.get(r.name, 0) + 1
                        first_seen_step.setdefault(r.name, display_i + 1)
                        last_seen_step[r.name] = display_i + 1

                    if changed:
                        print(f"\n--- Step {display_i + 1}/{total} (orig index {orig_idx}) - FAILURE ---")
                        print(f"   {desc}")
                        report.print()
                        if shot_path:
                            print(f"   Screenshot: {shot_path}")

                if target_invariant_name is not None and not reproduced:
                    names_here = {r.name for r in report.failures}
                    if any(name.startswith(target_invariant_name) for name in names_here):
                        reproduced = True
                        reproduced_at_step = display_i + 1

                if not report.passed and pause_on_failure and changed:
                    print("\n   Invariant failure - browser is paused.")
                    print("   Inspect the app, then [Enter] to continue or [q] to quit.")
                    try:
                        choice = input("   > ").strip().lower()
                        if choice == "q":
                            quit_early = True
                            steps_completed = display_i + 1
                            print(f"   Stopping replay at step {display_i + 1}/{total} "
                                  f"- {failing_steps} failing step(s) seen so far. This is NOT "
                                  f"a complete run: steps after this one were never checked.")
                            break
                    except EOFError:
                        pass

            replay_history.append(StepResult(
                action_id=f"replay:{testid}", description=desc,
                # log_exporter html-escapes "value", so it must be a str
                kwargs={"testid": testid,
                        "value": "" if value is None else str(value),
                        "value_repr": repr(value) if value is not None else None},
                action_succeeded=action_succeeded, action_error=action_error, report=report,
                screenshot_path=shot_path,
                # lets log_exporter diff against the previous entry; None if the action failed or the check errored
                snapshot=curr_snapshot,
            ))

            if self.interactive:
                while True:
                    choice = input("   > ").strip().lower()
                    if choice == "q":
                        quit_early = True
                        print(f"   Stopping replay at step {display_i + 1}/{total} "
                              f"- {failing_steps} failing step(s) seen so far. This is NOT "
                              f"a complete run: steps after this one were never checked.")
                        break
                    elif choice == "":
                        break
                    elif choice.isdigit():
                        target = int(choice)
                        if target <= display_i + 1:
                            print(f"   Step {target} is not after the current step "
                                  f"({display_i + 1}) - enter a later step number, [Enter], or [q].")
                            continue
                        self.pause_at = target
                        self.interactive = False
                        print(f"   Running automatically until step {target}...")
                        break
                    else:
                        print("   [Enter] Next step   [N] Run until step N   [q] Quit")
                if choice == "q":
                    break

            if display_i == total - 1:
                if verbose or self.interactive:
                    print("\nReplay complete.")
                if pause_at_end and (verbose or self.interactive):
                    input("Press Enter to close...")

        if not verbose and analyze_failures and occurrence_count:
            print(f"\n{'=' * 60}\nFailure summary (repeats collapsed above):")
            for name, count in occurrence_count.items():
                first, last = first_seen_step[name], last_seen_step[name]
                span = f"step {first}" if first == last else f"steps {first}-{last}"
                print(f"  {name}: {count} occurrence(s), {span}")

        if save_state_snapshots and state_snapshots_saved:
            print(f"\nState snapshots for {state_snapshots_saved} failing step(s) → "
                  f"{Path(state_snapshot_dir) / (self.run_id or 'unknown_run')}")
            print("   analyze_mutation_logs.py will pick these up automatically on its next run "
                  "(see triage_config.json's \"state_snapshots\" section).")

        if export_log:
            export_html_log(replay_history, path=log_path,
                            source_history_path=source_history_path,
                            source_mutation_log_path=source_mutation_log_path,
                            schema_option_name=schema_option_name,
                            data_file=data_file_name,
                            # safe here: only ReplayRunner.run() calls this, never the live loop
                            render_state_diffs=save_state_snapshots)

        return ReplayOutcome(
            reproduced=reproduced, reproduced_at_step=reproduced_at_step,
            invalid=invalid, invalid_reason=invalid_reason, steps_run=total,
            failing_steps=failing_steps, diverged_at=diverged_at,
            state_snapshots_saved=state_snapshots_saved,
            quit_early=quit_early, steps_completed=steps_completed,
        )

    def _infer_interaction(self, testid: str) -> str:
        """Fallback for records without el_interaction."""
        fill_prefixes = ("input-string-", "input-number-", "input-boolean-")
        select_prefixes = ("format-selector", "input-select-")
        click_prefixes = ("add-item-", "add-property-", "remove-property-",
                          "rename-property-", "expand-", "collapse-", "mode-active-")
        for p in fill_prefixes:
            if testid.startswith(p):
                return "fill"
        for p in select_prefixes:
            if testid.startswith(p) or testid == p:
                return "select"
        for p in click_prefixes:
            if testid.startswith(p):
                return "click"
        return "click"


def main():
    parser = argparse.ArgumentParser(description="Replay a mutation loop history and analyze its failures")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--history", help="Path to a history JSON file to replay.")
    which.add_argument("--latest", action="store_true",
                       help="Replay the most recent mutation loop run (newest "
                            "mutation_history/{run_id}/history.json). This is also what "
                            "happens when --history isn't given at all.")
    parser.add_argument("--until", type=int,
                        help="Run quietly through this step, then switch to manual, step-by-step "
                             "exploration for everything after - full detail per step, driven by "
                             "[Enter]/[N]/[q]. The browser stays open the whole time (it only "
                             "closes when you quit or confirm at the end), so you can also just "
                             "interact with the app directly once it pauses. Ignored with "
                             "--interactive, which is already manual from step 1.")
    parser.add_argument("--interactive", action="store_true",
                        help="Manual step-through instead of automated analysis: pauses before "
                             "each step, prints full detail for every step, writes replay_log.html.")
    parser.add_argument("--pause-at", type=int,
                        help="Same switch-to-manual-mode point as --until, without also being the "
                             "default way to invoke it. If both are given, the earlier one wins.")
    parser.add_argument("--schema", choices=SCHEMA_CHOICES, default=None,
                        help="Required if the history file predates schema/data metadata.")
    parser.add_argument("--html", action="store_true",
                        help="Also write an HTML log (replay_log.html) with embedded "
                             "screenshots, evidence, and the full step-by-step history - "
                             "off by default since the console analysis is usually enough.")
    parser.add_argument("--pause-on-failure", action="store_true",
                        help="Run quietly like the default, but actually pause (Enter/q) "
                             "whenever a step's invariant check fails, instead of just "
                             "printing. Browser stays open and interactive while paused.")
    parser.add_argument("--no-state-snapshots", action="store_true",
                        help="Do not write full before/after app state for failing steps to "
                             "replay_snapshots/ (see the module docstring's STATE SNAPSHOTS "
                             "FOR TRIAGE section). On by default; this opts back out to the "
                             "old hash-only behavior.")
    parser.add_argument("--state-snapshot-dir", default=DEFAULT_STATE_SNAPSHOT_DIR,
                        help=f"Where failing-step state snapshots are written, as "
                             f"<dir>/<run_id>/step_<n>_{{before,after}}.json. Default: "
                             f"{DEFAULT_STATE_SNAPSHOT_DIR!r} (must match triage_config.json's "
                             f"state_snapshots.root for analyze_mutation_logs.py to find them).")
    parser.add_argument("--export-cause-history", action="store_true",
                        help="After the replay, write an enriched COPY of the history file to "
                             "<cause-history-dir>/<run_id>/history.json, adding the fields "
                             "analyze_mutation_logs.py needs for its 'Where it likely started' "
                             "section. Use this for runs recorded before those fields existed. "
                             "The original history.json is never modified.")
    parser.add_argument("--cause-history-dir", default=DEFAULT_CAUSE_HISTORY_DIR,
                        help=f"Where --export-cause-history writes. Default: "
                             f"{DEFAULT_CAUSE_HISTORY_DIR!r}. Point analyze_mutation_logs.py at "
                             f"this folder.")
    args = parser.parse_args()

    if args.history:
        history_path = Path(args.history)
        if not history_path.exists():
            print(f"Error: History file not found: {history_path}")
            return
    else:
        history_path = find_latest_history()
        if history_path is None:
            print(f"Error: No mutation loop run found - looked for "
                  f"{MUTATION_HISTORY_DIR.resolve()}\\<run_id>\\history.json. Run this from the "
                  f"same folder you run the mutation loop from, or pass --history PATH.")
            return
        print(f"Latest run: {history_path.parent.name}")

    try:
        history, meta = load_history(history_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error: Could not read {history_path}: {e}")
        return

    repro = check_reproducibility(history)

    try:
        schema_option_name, data_file_name = resolve_schema_for_replay(meta, args.schema)
    except ValueError as e:
        print(f"Error: {e}")
        return

    resolved_settings = meta.get("resolved_settings") or {}
    # run_random() defaults to screenshots on
    orig_screenshots = resolved_settings.get("screenshots_enabled", True)
    orig_run_id = resolved_settings.get("run_id")
    dataset = _dataset_slug(schema_option_name, data_file_name)

    history_abs_path = history_path.resolve()
    source_mutation_log_path = _derive_mutation_log_path(history_abs_path)

    # --until and --pause-at both switch to manual mode; the earlier one applies
    switch_points = [n for n in (args.until, args.pause_at) if n]
    effective_pause_at = min(switch_points) if switch_points else None

    print(f"Loaded {len(history)} steps from {history_path}")
    print(f"   Full path: {history_abs_path}")
    if source_mutation_log_path:
        print(f"   Source mutation log: {source_mutation_log_path}")
    print(f"   Schema/data pair: {schema_option_name} / {data_file_name}")
    print(f"   Reproducibility: {'✓ ' if repro.ok else ''}{repro.reason}")
    print(f"   Original run: run_id={orig_run_id or '?'}, screenshots_enabled={orig_screenshots}"
          + ("" if resolved_settings else " (not recorded - assumed)"))
    if effective_pause_at and not args.interactive:
        print(f"   Running quietly through step {effective_pause_at}, then switching to manual "
              f"exploration - browser stays open until you quit or confirm at the end.")
    if not args.interactive:
        print("   Running quietly - full analysis will print at each failing step.")
    if not args.no_state_snapshots:
        print(f"   State snapshots for failing steps → {args.state_snapshot_dir}/{orig_run_id or '?'}/ "
              f"(disable with --no-state-snapshots)")

    will_export_log = args.interactive or args.html
    log_path = "replay_log.html"
    if will_export_log:
        replay_id = time.strftime("%Y%m%d_%H%M%S")
        replay_dir = Path("replay_logs") / replay_id
        replay_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(history_path, replay_dir / "history.json")
        log_path = str(replay_dir / "replay_log.html")
        print(f"   Replay session → {replay_dir}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()
        page.set_viewport_size(VIEWPORT)
        prepare_app_page(page, schema_option_name, data_file_name)

        # run_demo() runs a baseline invariant check first; mirror it for page-state parity
        with contextlib.redirect_stdout(io.StringIO()):
            InvariantChecker(page, SessionMode.DataEditor).run_all()

        runner = ReplayRunner(page, history, interactive=args.interactive, pause_at=effective_pause_at,
                              dataset=dataset, run_id=orig_run_id,
                              mirror_screenshot_scroll=orig_screenshots)

        cause_records: dict[int, dict] | None = {} if args.export_cause_history else None
        common_snapshot_kwargs = dict(
            save_state_snapshots=not args.no_state_snapshots,
            state_snapshot_dir=args.state_snapshot_dir,
            collect_cause_records=cause_records,
        )

        if args.interactive:
            outcome = runner.run(log_path=log_path,
                                 source_history_path=str(history_abs_path),
                                 source_mutation_log_path=source_mutation_log_path,
                                 schema_option_name=schema_option_name,
                                 data_file_name=data_file_name,
                                 **common_snapshot_kwargs)
        else:
            outcome = runner.run(pause_on_failure=args.pause_on_failure,
                                 pause_at_end=effective_pause_at is not None,
                                 export_log=args.html, log_path=log_path, verbose=False, analyze_failures=True,
                                 source_history_path=str(history_abs_path),
                                 source_mutation_log_path=source_mutation_log_path,
                                 schema_option_name=schema_option_name,
                                 data_file_name=data_file_name,
                                 **common_snapshot_kwargs)

        if cause_records is not None:
            out_path = export_enriched_history(history_path, cause_records,
                                               out_root=args.cause_history_dir, run_id=orig_run_id)
            unfaithful = sum(1 for r in cause_records.values() if r.get("replay_faithful") is False)
            print(f"\nCause-tracking history → {out_path}")
            print(f"   {len(cause_records)} of {len(history)} steps enriched"
                  + (f"; {unfaithful} step(s) at/after the point where the replay diverged are "
                     f"flagged replay_faithful=false" if unfaithful else ""))
            print(f"   Next: python -m tests.mutation_testing.analyze_mutation_logs {args.cause_history_dir}")

        print(f"\n{'=' * 60}")
        status = reproducibility_status(repro, outcome)
        if status == "not_reproducible":
            print(f"Not reproducible from this log - {repro.reason}. Divergence results "
                  f"below are best-effort only.")

        if outcome.quit_early:
            print(f"Replay stopped early by user at step {outcome.steps_completed}/{outcome.steps_run} "
                  f"- this is a PARTIAL result, not a completed replay.")
            if outcome.diverged_at is None:
                print(f"No divergence detected in the {outcome.steps_completed} step(s) actually replayed.")
            else:
                print(f"Replay diverged from the original run at step {outcome.diverged_at} - "
                      f"results after that step are not a faithful reproduction.")
            if outcome.failing_steps == 0:
                print(f"No invariant failures in the {outcome.steps_completed} step(s) replayed so far "
                      f"- the remaining {outcome.steps_run - outcome.steps_completed} step(s) were never "
                      f"checked. Re-run without quitting early (or use --pause-on-failure instead of "
                      f"--interactive) to get a real answer for the full run.")
            else:
                print(f"{outcome.failing_steps} failing step(s) seen in the "
                      f"{outcome.steps_completed} step(s) replayed so far - see analysis above. "
                      f"({outcome.steps_run - outcome.steps_completed} step(s) after that were never checked.)")
        else:
            if outcome.diverged_at is None:
                print("No divergence detected - replay tracked the original run throughout.")
            else:
                print(f"Replay diverged from the original run at step {outcome.diverged_at} - "
                      f"results after that step are not a faithful reproduction.")
            if outcome.failing_steps == 0:
                print(f"All {outcome.steps_run} steps passed - no invariant failures.")
            else:
                print(f"{outcome.failing_steps}/{outcome.steps_run} step(s) failed - see analysis above.")

        browser.close()


if __name__ == "__main__":
    main()