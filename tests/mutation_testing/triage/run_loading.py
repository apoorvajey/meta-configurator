"""Loading of recorded runs: log files, steps and their classification."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "triage_config.json"


QUOTED_RE = re.compile(r"'([^']*)'")


def _strip_docs(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_docs(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_docs(x) for x in obj]
    return obj


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return _strip_docs(json.load(f))


@dataclass
class Run:
    path: Path
    run_id: str
    schema: str
    data_file: str
    steps: list[dict]
    kinds: list[str]  # "action" | "auto_undo" | "walkback"
    started: datetime | None = None
    weights_profile: str = ""
    shots: dict | None = None
    shot_iters: list = field(default_factory=list)
    shot_image_count: int = 0
    shot_examples: list = field(default_factory=list)


def find_log_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.rglob("history.json"))
    raise FileNotFoundError(f"No such file or directory: {path}")


def classify_step(step: dict, cfg: dict) -> str:
    markers = cfg["log_markers"]
    desc = step.get("description") or ""
    if any(markers[k] in desc for k in ("walkback_attempt", "repair_cleared", "repair_gave_up")):
        return "walkback"
    if str(step.get("action_id", "")).startswith("repair:"):
        return "auto_undo"
    return "action"


def run_start_time(run_id: str, path: Path, cfg: dict) -> datetime:
    """Run start: run_id timestamp, else the folder name, else the file's modified time."""
    fmt = cfg["time_filter"]["run_id_format"]
    for text in (run_id, path.parent.name):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.fromtimestamp(path.stat().st_mtime)


def parse_time_arg(text: str, cfg: dict) -> datetime:
    for fmt in cfg["time_filter"]["input_formats"]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot read time '{text}'. Use one of: {', '.join(cfg['time_filter']['input_formats'])}")


def load_run(path: Path, cfg: dict) -> Run:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("meta", {}) if isinstance(data, dict) else {}
    steps = data.get("steps", []) if isinstance(data, dict) else data
    if not isinstance(steps, list):
        raise ValueError(f"expected a list of steps, got {type(steps).__name__}")
    steps = [s if isinstance(s, dict) else {} for s in steps]
    resolved = meta.get("resolved_settings", {}) or {}
    run_id = str(resolved.get("run_id") or path.parent.name)
    return Run(
        path=path,
        run_id=run_id,
        started=run_start_time(run_id, path, cfg),
        weights_profile=str((meta.get("run_config", {}) or {}).get("action_weights_profile", "")),
        schema=meta.get("schema_option_name", "unknown schema"),
        data_file=meta.get("data_file", "unknown data file"),
        steps=steps,
        kinds=[classify_step(s, cfg) for s in steps],
    )


def _rel(path: Path) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:
        return str(path)


def find_run_logs(run: Run, cfg: dict) -> list[Path]:
    """Files of the mutation loop's own log for this run (e.g. mutation_log/<run id>/...)."""
    rl = cfg["run_logs"]
    roots = [Path(rl["root"]), run.path.parent.parent.parent / rl["root"]]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if run.run_id not in entry.name:
                continue
            files = [entry] if entry.is_file() else [p for pat in ("*", "*/*") for p in sorted(entry.glob(pat)) if p.is_file()]
            for f in files:
                if f.resolve() not in {x.resolve() for x in found}:
                    found.append(f)
    return found[:rl["max_files"]]
