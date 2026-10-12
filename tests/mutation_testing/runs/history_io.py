"""Finding, loading and exporting run histories (history.json)."""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from tests.mutation_testing.common.datasets import SCHEMA_DATA_PAIRS
from tests.invariants.invariants_core import _diff_nested
from tests.mutation_testing.runs.reproducibility import _dump_state_snapshot, _state_hash
from tests.mutation_testing.runs.run_layout import REPLAYS, RUNS_DIR, run_id_of


_SCHEMA_NAME_TO_DATA_FILE: dict[str, str] = {name: path.name for name, path in SCHEMA_DATA_PAIRS}
SCHEMA_CHOICES: tuple[str, ...] = tuple(_SCHEMA_NAME_TO_DATA_FILE.keys())


# run_id format: time.strftime("%Y%m%d_%H%M%S");

def find_latest_history(root: Path = RUNS_DIR) -> Path | None:
    """Newest tests/output/runs/{run_id}/history.json (the recorded run, not a replay), or None.

    "Newest" comes from the run_id in the folder name (the start time), which copying files cannot change. Folders
    not named like a run_id, and runs without a history.json (crashed or killed), are skipped.
    """
    if not root.is_dir():
        return None
    candidates = [
        d / "history.json"
        for d in root.iterdir()
        if d.is_dir() and run_id_of(d.name) and (d / "history.json").is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: run_id_of(p.parent.name))


_SINCE_FORMATS = ("%Y%m%d_%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                  "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d")


def parse_since(text: str) -> datetime:
    """Parse a --since value into a datetime: a run_id (20260921_121528), an ISO-ish date or time (2026-09-21,
    "2026-09-21 12:15", 2026-09-21T12:15:28) or a relative age like 90m, 6h, 2d.
    """
    text = text.strip()
    m = re.fullmatch(r"(\d+)([mhd])", text)
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return datetime.now() - timedelta(**{unit: int(m.group(1))})
    for fmt in _SINCE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse --since value {text!r}. Use a run_id "
                     f"(20260921_121528), a date/time (2026-09-21 or '2026-09-21 12:15'), "
                     f"or a relative age (90m, 6h, 2d).")


def _history_time(history_path: Path) -> datetime:
    """When a run started: from its run_id folder name, else the file's mtime."""
    try:
        return datetime.strptime(run_id_of(history_path.parent.name) or "", "%Y%m%d_%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(history_path.stat().st_mtime)


def find_histories(root: Path, since: datetime | None = None,
                   explicit_folder: bool = False) -> list[Path]:
    """All history.json files to batch-replay, oldest first.

    - root contains a history.json: just that one.
    - explicit_folder (--folder): every history.json under root, except the replays' own (tests/output/runs/<id>/replays/...).
    - otherwise: tests/output/runs/{run_id}/history.json folders named like a run_id.
    - since: only runs that started at or after that time.
    """
    if not root.is_dir():
        return []
    if (root / "history.json").is_file():
        found = [root / "history.json"]
    elif explicit_folder:
        found = [p for p in root.rglob("history.json") if p.parent.parent.name != REPLAYS]
    else:
        found = [d / "history.json" for d in root.iterdir()
                 if d.is_dir() and run_id_of(d.name) and (d / "history.json").is_file()]
    if since is not None:
        found = [p for p in found if _history_time(p) >= since]
    return sorted(found, key=lambda p: (_history_time(p), str(p)))


def resolve_schema_for_replay(meta: dict, cli_schema: str | None) -> tuple[str, str]:
    """--schema if given, else the pair recorded in meta, else raises."""
    if cli_schema is not None:
        if cli_schema not in _SCHEMA_NAME_TO_DATA_FILE:
            raise ValueError(f"'{cli_schema}' is not recognized. Valid: {', '.join(SCHEMA_CHOICES)}")
        return cli_schema, _SCHEMA_NAME_TO_DATA_FILE[cli_schema]
    meta_schema, meta_data = meta.get("schema_option_name"), meta.get("data_file")
    if meta_schema and meta_data:
        return meta_schema, meta_data
    raise ValueError(
        "No schema/data metadata in this file and no --schema given — pass one of: "
        + ", ".join(SCHEMA_CHOICES))


def _derive_mutation_log_path(history_abs_path: Path) -> str | None:
    """Path of the mutation_log.html of the run that produced this history.json, or None.

    It sits next to the history.json (tests/output/runs/{run_id}/mutation_log.html); None if the file was moved or there is no log.
    """
    candidate = history_abs_path.parent / "mutation_log.html"
    return str(candidate) if history_abs_path.name == "history.json" and candidate.exists() else None


def load_history(path: str | Path) -> tuple[list[dict], dict]:
    """Read a history.json as (steps, meta). Accepts a bare list of steps or {"meta": ..., "steps": ...}."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        return raw, {}
    return raw.get("steps", []), raw.get("meta", {}) or {}


# State facets hashed per step for cause tracking (read by causal_analysis.py)
_FACET_KEYS = ("de_data", "de_schema", "se_schema", "se_data", "de_text", "se_text")

# Extra per-step fields the mutation loop records (what an action actually changed, schema validity
# transitions, why a recovery click happened). Written to history.json only when present.
_EXTRA_STEP_FIELDS = ("observed_value", "observed_effect", "schema_valid_after",
                      "schema_became_invalid", "schema_meta_violations", "recovery_reason")
_FACET_HASH_CHARS = 10
_MAX_EVIDENCE_PATHS = 20
_MAX_EVIDENCE_MESSAGE_CHARS = 300
_MAX_EVIDENCE_DETAILS_CHARS = 20000   # JSON size cap for a failure's details; larger ones are replaced by a note


def _facet_hashes(snap: dict) -> dict:
    return {k: hashlib.sha1(json.dumps(snap[k], sort_keys=True, default=str).encode()).hexdigest()[:_FACET_HASH_CHARS]
            for k in _FACET_KEYS if k in snap}


def _failure_paths(details) -> list:
    """Paths/keys a failed invariant names, whatever its details shape is."""
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


def _failure_details(details) -> dict | None:
    """The failure's details (what the mutation log shows under the message) as plain JSON, or a note if too big."""
    if not details:
        return None
    try:
        text = json.dumps(details, default=str, ensure_ascii=False)
    except Exception:
        return {"note": "details could not be serialised"}
    if len(text) > _MAX_EVIDENCE_DETAILS_CHARS:
        return {"note": f"details too large to keep ({len(text)} characters); see the run log"}
    return json.loads(text)


def _cause_fields(report, snap: dict | None) -> dict:
    """The cause-tracking fields of one step; shared by export_history() and export_replay_history()."""
    out: dict = {}
    if report is not None:
        # Skipped results (precondition not met) were not evaluated
        out["checked_invariants"] = [r.name.partition(" ")[0] for r in report.results
                                     if not getattr(r, "skipped", False)]
        out["skipped_invariants"] = [r.name.partition(" ")[0] for r in report.results
                                     if getattr(r, "skipped", False)]
        out["failed_invariants"] = [r.name for r in report.failures]
        out["failure_evidence"] = [
            {"id": r.name.partition(" ")[0],
             "message": (r.message or "")[:_MAX_EVIDENCE_MESSAGE_CHARS],
             "paths": _failure_paths(r.details),
             "details": _failure_details(r.details),
             "evidence": [str(e) for e in (r.evidence or [])][:30]}
            for r in report.failures]
    out["facet_hashes"] = _facet_hashes(snap or {})
    return out


def export_replay_history(source_path: Path, cause_records: dict[int, dict],
                          replay_dir: str | Path, replay_id: str,
                          orig_run_id: str | None = None, complete: bool = False) -> Path:
    """Write the replay's own history to <replay_dir>/history.json (tests/output/runs/<run_id>/replays/<replay_id>/).

    Same step format as a loop history, so the report reads it as-is. "passed" is the REPLAY's result;
    steps the replay never reached get passed=None (not False), so they cannot show up as failures.
    The original file is only referenced in meta.replay, never copied.
    complete: the replay ran to the end (not stopped early, no --until). Only a complete replay replaces the
    original in the report (meta.replay.complete).
    """
    with open(source_path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        raw = {"meta": {}, "steps": raw}
    meta = raw.setdefault("meta", {}) or {}
    raw["meta"] = meta
    resolved = meta.setdefault("resolved_settings", {}) or {}
    meta["resolved_settings"] = resolved
    folder_name = Path(source_path).resolve().parent.name
    orig_run_id = orig_run_id or resolved.get("run_id") or run_id_of(folder_name) or folder_name

    steps = raw.get("steps", [])
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        fields = cause_records.get(idx)
        if fields is None:
            step["passed"] = None
            step["replayed"] = False
            continue
        step.update(fields)
        step["passed"] = fields.get("replay_passed")
        step["replayed"] = True

    resolved["run_id"] = replay_id          # the report shows the replay's own time
    meta["replay"] = {
        "replay_id": replay_id,
        "source_run_id": orig_run_id,
        "source_history": str(Path(source_path).resolve()),
        "complete": complete,
        "steps_replayed": len(cause_records),
        "steps_total": len(steps),
        "steps_unfaithful": sum(1 for r in cause_records.values() if r.get("replay_faithful") is False),
    }

    out_path = Path(replay_dir) / "history.json"
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
                   state_snapshot_dir: str | Path | None = None) -> None:
    """Export a MutationLoop history to JSON; "role" marks repair steps. Replay fields:
    - step_seed: re-applied right before execute();
    - input_value: the exact value passed to execute(); replay uses it instead of the stringified "value";
    - el_interaction, el_tag, el_role: the discovered element's fields (el_view is for diagnostics only);
    - step: the snapshot's step number.

    run_config and resolved_settings record the run_config.json content and the values resolved from it; replay
    reads resolved_settings.screenshots_enabled and .run_id.

    save_state_snapshots: write the full before/after state of each failing step to
    state_snapshot_dir/step_{n}_{before,after}.json (default tests/output/runs/{run_id}/states), the same files a
    replay writes. A step record itself keeps only a state hash.
    """
    def _snap_of(entry) -> dict | None:
        return getattr(entry, "snapshot", None)

    run_id = (resolved_settings or {}).get("run_id") or "unknown_run"
    snap_dir = Path(state_snapshot_dir) if state_snapshot_dir else RUNS_DIR / str(run_id) / "states"
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
        for key in _EXTRA_STEP_FIELDS:
            if s.kwargs.get(key) is not None:
                rec[key] = s.kwargs[key]

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
    print(f"\nHistory exported: {path}")
    print(f"   {len(records)} steps saved")
    if save_state_snapshots and snapshots_saved:
        print(f"   State snapshots for {snapshots_saved} failing step(s): {snap_dir}")
    if schema_option_name:
        print(f"   Schema/data pair: {schema_option_name} / {data_file}")
    if resolved_settings:
        print("   Run settings recorded:")
        for k, v in resolved_settings.items():
            print(f"     {k}: {v}")
    print(f"   Replay with: python -m tests.mutation_testing.replay.replay_runner --history {path}")