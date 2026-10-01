"""Triage report for mutation-loop history files.

Reads one or more history.json files and writes one self-contained HTML report: a summary of
whether the run found bugs, then one card per candidate.

- Episodes: consecutive failing steps form one episode. Failures that flicker between pass and fail
  stay one episode (episodes.max_pass_gap), and the auto-repair walk-back belongs to the episode it
  was trying to fix.
- Naming: a failing invariant is named only where the log records it (in the auto-repair steps, or
  in a step's failed_invariants list). Otherwise the report says it is unnamed.
- Tiers: Likely bug (repair gave up, or still failing at the end of the run), Known bug (matches
  known_bugs in triage_config.json), Worth a look (recovered, but named or long), Blip (short,
  unnamed, recovered), Filtered (only retired invariants).
- Cross-run: the same finding in several runs is merged into one card.
- State diff: if the run was replayed with state snapshots (see replay_runner), the card shows the
  exact before/after app state. Otherwise it points to the replay command.

Thresholds, invariant descriptions and known bugs are in triage_config.json (in config/, or
--config).

    python -m tests.mutation_testing.analyze_mutation_logs mutation_history --html reports/triage_report.html
    python -m tests.mutation_testing.analyze_mutation_logs path/to/history.json
    python -m tests.mutation_testing.analyze_mutation_logs --last-hours 12
    python -m tests.mutation_testing.analyze_mutation_logs --since 20260927_200000 --until 20260928_090000
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from tests.mutation_testing.causal_analysis import causal_html, lead_lines, invariant_explanations_html
except ImportError:
    from causal_analysis import causal_html, lead_lines, invariant_explanations_html

# Optional. Without it, _state_diff_html prints the two full JSON states instead of a field-level diff.
try:
    from deepdiff import DeepDiff
except ImportError:
    DeepDiff = None

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "triage_config.json"
QUOTED_RE = re.compile(r"'([^']*)'")

TIER_ORDER = ["likely", "known", "watch"]
TIER_LABEL = {
    "likely": "Likely bug",
    "known": "Known bug",
    "watch": "Worth a look",
}


# Config and loading

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


# Plain-language action translation

EDITOR_KINDS = [
    ("add-array-item-text-", "add_array_item"),
    ("add-child-text-", "add_child"),
    ("remove-array-item-text-", "remove_array_item"),
    ("remove-child-text-", "remove_child"),
    ("change-type-text-", "change_type"),
    ("rename-child-text-", "rename_child"),
    ("edit-text-", "edit"),
    ("clear-text-", "clear"),
]
PATH_KINDS = [
    ("add-property-", "add_property"),
    ("add-item-", "add_item"),
    ("collapse-", "collapse"),
    ("expand-", "expand"),
]
STANDALONE_KINDS = {
    "undo": "undo", "redo": "redo", "schema_undo": "undo", "schema_redo": "redo",
    "format-selector": "format", "mode-active-false": "switch", "mode-active-true": "switch",
}
# what the log does not record for each kind of action
MISSING_DETAIL = {
    "edit": "new value not recorded",
    "change_type": "new type not recorded",
    "rename_child": "new name not recorded",
    "add_property": "property name not recorded",
    "add_array_item": "item content not recorded",
    "format": "chosen format not recorded",
    "switch": "target tab not recorded",
}


def parse_action(action_id: str) -> dict:
    """Split an action id such as
    'dynamic:add-child-text-schemaEditor:definitions.File.properties.content:if'
    into kind, panel, parent path and child name.
    """
    raw = action_id or ""
    is_repair = raw.startswith("repair:")
    for p in ("dynamic:", "repair:"):
        if raw.startswith(p):
            raw = raw[len(p):]
            break
    out = {"kind": "other", "editor": None, "path": "", "child": None, "repair": is_repair, "raw": raw}
    if raw in STANDALONE_KINDS:
        out["kind"] = STANDALONE_KINDS[raw]
        out["standalone"] = raw
        return out
    for prefix, kind in EDITOR_KINDS:
        if raw.startswith(prefix):
            editor, _, rest = raw[len(prefix):].partition(":")
            out.update(kind=kind, editor=editor)
            if kind == "add_child":
                if ":" in rest:
                    parent, _, child = rest.rpartition(":")
                    out.update(path=parent, child=None if child in ("", "*") else child)
                else:
                    out.update(path=rest)
            else:
                out["path"] = rest.rstrip(":*").strip(":")
            return out
    for prefix, kind in PATH_KINDS:
        if raw.startswith(prefix):
            out.update(kind=kind, path=raw[len(prefix):])
            return out
    return out


def missing_detail(action_id: str) -> str:
    p = parse_action(action_id)
    if p["kind"] == "add_child" and p["child"] is None:
        return "child name not recorded"
    return MISSING_DETAIL.get(p["kind"], "")


def translate_action(action_id: str, mode: str, panels: dict, include_path: bool = True) -> str:
    p = parse_action(action_id)
    kind, path, child = p["kind"], p["path"], p["child"]
    where = panels.get(p["editor"] or mode, "")
    in_ = f" in the {where}" if where else ""
    par = f" ({where})" if where else ""
    root = "the top level of the schema" if p["editor"] == "schemaEditor" else "the top level of the data"

    if kind == "add_child":
        if include_path:
            head = f"Added the child field '{child}'" if child else "Added a child field"
            label = f"{head} under '{path}'{in_}" if path else f"{head} at {root}{in_}"
        else:
            label = f"Added a child field{in_}"
    elif kind in ("edit", "change_type", "rename_child", "remove_child", "add_array_item", "remove_array_item"):
        verb = {
            "edit": "Edited the value at", "change_type": "Changed the type of",
            "rename_child": "Renamed the field", "remove_child": "Removed the field",
            "add_array_item": "Added a new item to the array", "remove_array_item": "Removed the array item",
        }[kind]
        bare = {
            "edit": "Edited a value", "change_type": "Changed a field's type", "rename_child": "Renamed a field",
            "remove_child": "Removed a field", "add_array_item": "Added an array item", "remove_array_item": "Removed an array item",
        }[kind]
        label = (f"{verb} '{path}'{in_}" if path else f"{verb} {root}{in_}") if include_path else f"{bare}{in_}"
    elif kind == "clear":
        label = f"Cleared all the text{in_}"
    elif kind in ("add_item", "add_property", "collapse", "expand"):
        verb = {"add_item": "Added an item to", "add_property": "Added a property to",
                "collapse": "Collapsed", "expand": "Expanded"}[kind]
        bare = {"add_item": "Added an item", "add_property": "Added a property",
                "collapse": "Collapsed a node", "expand": "Expanded a node"}[kind]
        via = " using the GUI" if kind in ("add_item", "add_property") else ""
        label = f"{verb} '{path}'{via}{par}" if (include_path and path) else f"{bare}{via}{par}"
    elif kind == "undo":
        label = f"Clicked Undo{par}"
    elif kind == "redo":
        label = f"Clicked Redo{par}"
    elif kind == "format":
        label = "Changed the file format"
    elif kind == "switch":
        label = "Switched to another view/tab"
    else:
        label = p["raw"].replace("-", " ").replace(":", " -> ") or "unknown action"
    return f"Test tool: {label}" if p["repair"] else label


# Episodes

@dataclass
class Episode:
    run: Run
    start: int
    end: int
    fail_steps: list[int] = field(default_factory=list)
    walkback_steps: list[int] = field(default_factory=list)
    invariants: dict[str, str] = field(default_factory=dict)  # id -> name as logged
    names_from_walkback_only: bool = True
    outcome: str = "self_recovered"  # gave_up | run_ended | cleared | self_recovered
    tier: str = "blip"
    known_bug: dict | None = None

    @property
    def length(self) -> int:
        return self.end - self.start + 1


INV_ID_RE = re.compile(r"^[A-Z]+(?:-[A-Z0-9]+)+$")  # INV-D4, INF-E8C6D1CF, TEMP-ADD-03, NEW-07, INV-GUI-ERR-01
LEGACY_INV_RE = re.compile(r"^_inv_([a-z])(\d+)_")  # _inv_d4_gui_rows_... -> INV-D4


def split_invariant(full: str) -> tuple[str, str] | None:
    """'INV-D4 name' -> ('INV-D4', 'name'); '_inv_d4_xyz' -> ('INV-D4', '_inv_d4_xyz').
    None if the text is not an invariant label (e.g. a quoted path like 'Vehicles.car2').
    """
    first, _, name = (full or "").strip().partition(" ")
    if INV_ID_RE.match(first):
        return first, name or first
    m = LEGACY_INV_RE.match(first)
    if m:
        return f"CHK-ERR-INV-{m.group(1).upper()}{m.group(2)}", first
    return None


def parse_invariants(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for full in QUOTED_RE.findall(text or ""):
        parsed = split_invariant(full)
        if parsed:
            out[parsed[0]] = parsed[1]
    return out


def _is_failing(step: dict) -> bool:
    return step.get("passed") is False and step.get("action_succeeded") is not False


def build_episodes(run: Run, cfg: dict) -> list[Episode]:
    gap_limit = cfg["episodes"]["max_pass_gap"]
    markers = cfg["log_markers"]
    episodes: list[Episode] = []
    cur: Episode | None = None
    pass_streak = 0

    for i, step in enumerate(run.steps):
        kind = run.kinds[i]
        if kind == "walkback":
            if cur is None:
                cur = Episode(run=run, start=i, end=i)
            cur.walkback_steps.append(i)
            cur.end = i
            desc = step.get("description") or ""
            cur.invariants.update(parse_invariants(desc))
            if markers["repair_gave_up"] in desc:
                cur.outcome = "gave_up"
            elif markers["repair_cleared"] in desc and cur.outcome != "gave_up":
                cur.outcome = "cleared"
            pass_streak = 0
            continue

        if _is_failing(step):
            if cur is None:
                cur = Episode(run=run, start=i, end=i)
            cur.fail_steps.append(i)
            cur.end = i
            for entry in step.get("failed_invariants", []) or []:
                if isinstance(entry, str):
                    _id, _, _rest = entry.partition(" ")
                    inv_id, name = split_invariant(entry) or (_id, _rest or _id)
                    cur.invariants[inv_id] = name
                    cur.names_from_walkback_only = False
            pass_streak = 0
        elif cur is not None:
            pass_streak += 1
            if pass_streak > gap_limit:
                episodes.append(cur)
                cur = None
                pass_streak = 0

    if cur is not None:
        episodes.append(cur)

    last_index = len(run.steps) - 1
    for ep in episodes:
        if ep.outcome in ("gave_up", "cleared"):
            continue
        ep.outcome = "run_ended" if ep.end == last_index else "self_recovered"
    return episodes


def assign_tier(ep: Episode, cfg: dict) -> None:
    ids = set(ep.invariants)
    retired = set(cfg["retired_invariants"])
    blip_max = cfg["episodes"]["blip_max_steps"]

    if ids and ids <= retired:
        ep.tier = "artifact"
        return
    for bug in cfg["known_bugs"]:
        if ids & set(bug["invariants"]):
            ep.tier, ep.known_bug = "known", bug
            return
    if ep.outcome in ("gave_up", "run_ended"):
        ep.tier = "likely"
    elif ep.invariants or ep.length > blip_max:
        ep.tier = "watch"
    else:
        ep.tier = "blip"


# Labels, titles, evidence, repro, draft issue

def step_label(run: Run, i: int, cfg: dict, include_path: bool = True) -> str:
    s = run.steps[i]
    return translate_action(s.get("action_id", ""), s.get("mode", ""), cfg["panels"], include_path)


def trigger_index(ep: Episode) -> int:
    """The user-level action that started the episode. If the first failing step is the tool's own
    auto-undo, the trigger is the action it undid.
    """
    if ep.run.kinds[ep.start] == "auto_undo" and ep.start > 0:
        return ep.start - 1
    return ep.start


def rule_text(inv_id: str, logged_name: str, cfg: dict) -> str:
    return cfg["invariants"].get(inv_id, logged_name or inv_id)


def look_for_text(inv_id: str, cfg: dict) -> str:
    detail = cfg.get("invariant_details", {}).get(inv_id, {})
    return detail.get("look_for") or "The editor's internal consistency check failed; the exact on-screen symptom is not recorded yet."


def repeated_undo_target(ep: Episode, cfg: dict) -> tuple[str | None, list[int]]:
    """If the tool's automatic undo left the rules failing at least twice, return the action being
    undone and those step indexes.
    """
    bad = [i for i in ep.fail_steps if ep.run.kinds[i] == "auto_undo" and i > 0]
    if len(bad) < 2:
        return None, bad
    what = Counter(step_label(ep.run, i - 1, cfg, include_path=False) for i in bad).most_common(1)[0][0]
    return what, bad


def episode_title(ep: Episode, cfg: dict) -> str:
    what, _ = repeated_undo_target(ep, cfg)
    rule = ""
    if ep.invariants:
        first_id = sorted(ep.invariants)[0]
        more = len(ep.invariants) - 1
        extra = f" (+{more} more)" if more else ""
        rule = f" Broken rule: {rule_text(first_id, ep.invariants[first_id], cfg)}{extra}."
    if what:
        return f"Repeatedly inconsistent after undoing '{what}'.{rule}"
    trig = step_label(ep.run, trigger_index(ep), cfg)
    if ep.invariants:
        return f"Views became inconsistent after: {trig}.{rule}"
    return f"Views became inconsistent after: {trig} (which rule broke is not recorded)"


def short_title(ep: Episode, cfg: dict) -> str:
    what, _ = repeated_undo_target(ep, cfg)
    if what:
        return f"Inconsistent state after undoing: {what}"
    return f"Inconsistent state after: {step_label(ep.run, trigger_index(ep), cfg)}"


def outcome_text(ep: Episode) -> str:
    return {
        "gave_up": "The test tool retried Undo/Redo automatically and the problem stayed, so it gave up.",
        "run_ended": "The problem was still there when the test session ended.",
        "cleared": "The problem appeared, then the automatic Undo/Redo retry fixed it.",
        "self_recovered": "The problem appeared, then went away on its own.",
    }[ep.outcome]


def _panel(step: dict, cfg: dict) -> str:
    return cfg["panels"].get(step.get("mode", ""), step.get("mode", "") or "?")


def episode_evidence(ep: Episode, cfg: dict) -> tuple[list[str], list[str]]:
    """Returns (why_flagged, technical_details)."""
    run, why, tech = ep.run, [], []
    why.append(f"It lasted {ep.length} actions (numbers {ep.start} to {ep.end} in the session). {outcome_text(ep)}")

    what, bad = repeated_undo_target(ep, cfg)
    if what:
        why.append(f"It happened {len(bad)} times in a row: each time the test tool undid "
                   f"'{what}', the checks failed afterwards.")

    if ep.walkback_steps:
        undo = sum(1 for i in ep.walkback_steps if run.steps[i].get("action_id", "").endswith("undo"))
        redo = sum(1 for i in ep.walkback_steps if run.steps[i].get("action_id", "").endswith("redo"))
        tech.append(f"Automatic retry clicked Undo {undo}x and Redo {redo}x (actions {ep.walkback_steps[0]} to {ep.walkback_steps[-1]}).")

    fail_by, all_by = Counter(), Counter()
    for i in range(ep.start, ep.end + 1):
        if run.kinds[i] == "walkback":
            continue
        p = _panel(run.steps[i], cfg)
        all_by[p] += 1
        fail_by[p] += _is_failing(run.steps[i])
    if len(all_by) >= 2:
        parts = [f"{p}: {fail_by[p]} of {n} actions failing" for p, n in all_by.items()]
        why.append("Which panel was active when checks failed: " + "; ".join(parts) + ".")

    if ep.invariants and ep.names_from_walkback_only and ep.walkback_steps and run.kinds[ep.start] != "walkback":
        tech.append(f"Rule names come from the automatic-retry records (actions {ep.walkback_steps[0]} to "
                    f"{ep.walkback_steps[-1]}). The log does not say which rules failed at action {ep.start}, so they may differ.")
    if not ep.invariants:
        tech.append("The log records only pass/fail for these actions, not which rule failed, and no automatic retry was logged.")
    return why, tech


def repro_steps(ep: Episode, cfg: dict, full: bool = False) -> list[dict]:
    run = ep.run
    idx = [i for i in range(ep.start + 1) if run.kinds[i] != "walkback"]
    if not full:
        idx = idx[-cfg["episodes"]["repro_window"]:]
    return [{
        "step": i,
        "label": step_label(run, i, cfg),
        "panel": _panel(run.steps[i], cfg),
        "failed": i == ep.start,
        "auto": run.kinds[i] == "auto_undo",
        "hint": missing_detail(run.steps[i].get("action_id", "")),
    } for i in idx]


def needs_panel_caveat(steps: list[dict], run: Run, cfg: dict) -> bool:
    ids = set(cfg["undo_redo_action_ids"])
    used_undo = any(
        run.steps[s["step"]].get("action_id", "").replace("dynamic:", "").replace("repair:", "") in ids
        for s in steps)
    return used_undo and len({s["panel"] for s in steps}) >= 2


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _shot_folders(run: Run, cfg: dict) -> list[Path]:
    return [run.path.parent, Path(cfg["screenshots"]["root"]) / run.run_id]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def get_shots(run: Run, cfg: dict) -> dict[int, list[tuple[str, Path]]]:
    """Index screenshots by their step counter N, and compute N for every log step. N counts the
    main ('dynamic:') actions; the tool's own undo/retry entries share the N of the action that
    triggered them.
    """
    if run.shots is not None:
        return run.shots
    sc = cfg["screenshots"]
    rx = re.compile(sc["filename_regex"])
    idx: dict[int, list[tuple[str, Path]]] = defaultdict(list)
    seen: set[Path] = set()
    if sc.get("enabled"):
        for folder in _shot_folders(run, cfg):
            if not folder.is_dir():
                continue
            for pattern in ("*", "*/*"):
                for p in sorted(folder.glob(pattern)):
                    if p.suffix.lower() not in IMAGE_EXTS or not p.is_file() or p in seen:
                        continue
                    seen.add(p)
                    m = rx.match(p.stem)
                    if m:
                        idx[int(m.group("n"))].append((m.group("name"), p))
    count, iters = 0, []
    for st in run.steps:
        if str(st.get("action_id", "")).startswith("dynamic:"):
            count += 1
        iters.append(count)
    run.shots, run.shot_iters = dict(idx), iters
    run.shot_image_count = len(seen)
    run.shot_examples = [p.name for p in sorted(seen)[:3]]
    return run.shots


def shots_for_step(run: Run, i: int, cfg: dict) -> list[Path]:
    """Screenshots whose counter and action name both match log step i."""
    get_shots(run, cfg)
    step = run.steps[i]
    cands = (run.shots or {}).get(run.shot_iters[i], [])
    name = re.sub(r"^(dynamic|repair):", "", str(step.get("action_id", ""))).lower()
    kind = run.kinds[i]
    if kind == "auto_undo":
        hits = [p for f, p in cands if f.lower() == f"autoundo_{name}"]
    elif kind == "walkback":
        block_start = i
        while block_start > 0 and run.kinds[block_start - 1] == "walkback":
            block_start -= 1
        ordinal = sum(1 for k in range(block_start, i + 1) if run.steps[k].get("action_id") == step.get("action_id"))
        rep_files = sorted(((int(m.group(1)), p) for f, p in cands
                            for m in [re.fullmatch(r"repair(\d+)_" + re.escape(name), f.lower())] if m))
        hits = [p for _, p in rep_files]
        hits = [hits[ordinal - 1]] if len(hits) >= ordinal else hits[:1]
    else:
        tid = _norm(str(step.get("testid") or name))
        hits = [p for f, p in cands
                if not f.lower().startswith(("autoundo_", "repair")) and _norm(f) and tid.startswith(_norm(f))]
    return hits[:cfg["screenshots"]["max_per_step"]]


def draft_issue(ep: Episode, cfg: dict, steps: list[dict]) -> str:
    run = ep.run
    rules = sorted(ep.invariants.items())
    expected = "\n".join(f"- {rule_text(i, n, cfg)}" for i, n in rules) or "- The views stay consistent with each other."
    actual = "\n".join(f"- {look_for_text(i, cfg)}" for i, _ in rules) or f"- {look_for_text('', cfg)}"
    numbered = "\n".join(
        f"{n}. {s['label'].replace('Test tool: ', '')}" + (f" ({s['hint']})" if s["hint"] else "")
        for n, s in enumerate(steps, 1))
    return (
        f"**Title:** {short_title(ep, cfg)}\n\n"
        f"**How it was found:** automated random-action testing of {cfg['project']['app_url']} with consistency checks. "
        f"NOT yet confirmed by hand. Please treat as a candidate until reproduced.\n\n"
        f"**Setup:** choose the example schema \"{run.schema}\" and upload the data file \"{run.data_file}\".\n\n"
        f"**Log reference:** test session {run.run_id}, actions {ep.start} to {ep.end} "
        f"(positions in the \"steps\" list of its history.json, counting from 0).\n\n"
        f"**Steps** (the last part of a longer session; if it does not reproduce, earlier actions may matter):\n{numbered}\n\n"
        f"**Expected:**\n{expected}\n\n**Actual:**\n{actual}\n"
    )


# Cross-run grouping

@dataclass
class Candidate:
    gid: int
    tier: str
    episodes: list[Episode]

    @property
    def primary(self) -> Episode:
        return self.episodes[0]


def group_candidates(episodes: list[Episode], cfg: dict) -> list[Candidate]:
    groups: dict[tuple, list[Episode]] = defaultdict(list)
    for ep in episodes:
        if ep.tier not in TIER_ORDER:
            continue
        generic = step_label(ep.run, trigger_index(ep), cfg, include_path=False)
        groups[(ep.tier, tuple(sorted(ep.invariants)), generic, ep.outcome)].append(ep)

    ordered = []
    for (tier, _, _, _), eps in groups.items():
        eps.sort(key=lambda e: -e.length)
        ordered.append((tier, eps))
    ordered.sort(key=lambda t: (TIER_ORDER.index(t[0]), -len({e.run.path for e in t[1]}), -t[1][0].length))
    return [Candidate(gid=n + 1, tier=t, episodes=eps) for n, (t, eps) in enumerate(ordered)]


# State diff

# de_gui/se_gui are left out: raw DOM facts, noisy to diff and not what the invariants compare.
_SNAPSHOT_FACETS = ("de_data", "de_schema", "se_schema", "se_data", "de_text", "se_text")


def _snapshot_dir(run: Run, cfg: dict) -> Path:
    root = cfg.get("state_snapshots", {}).get("root", "replay_snapshots")
    return Path(root) / run.run_id


def _load_snapshot(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _replay_command_hint(ep: Episode) -> str:
    return f"python -m tests.mutation_testing.replay_runner --history {esc(_rel(ep.run.path))}"


_DIFF_PATH_RE = re.compile(r"root(\[[^\]]+\])+")


def _diff_kind(line: str) -> str:
    low = line.lower()
    if "added" in low:
        return "add"
    if "removed" in low:
        return "remove"
    return "change"


def _diff_list_html(delta) -> str:
    """One <li> per DeepDiff change, coloured as add, remove or change."""
    lines = [ln for ln in delta.pretty().split("\n") if ln.strip()]
    rows = []
    for line in lines:
        marked = _DIFF_PATH_RE.sub(lambda m: f"<code>{m.group(0)}</code>", esc(line))
        rows.append(f'<li class="diff-{_diff_kind(line)}">{marked}</li>')
    return f'<ul class="difflist">{"".join(rows)}</ul>'


def _state_diff_html(ep: Episode, cfg: dict) -> str:
    """Exact before/after diff for the episode's first failing step, if the run was replayed with
    state snapshots. The live loop also writes them when it finishes, so this is absent only for runs
    that predate that. The report then shows the command to generate them.
    """
    snap_dir = _snapshot_dir(ep.run, cfg)
    before = _load_snapshot(snap_dir / f"step_{ep.start}_before.json")
    after = _load_snapshot(snap_dir / f"step_{ep.start}_after.json")

    if after is None:
        return (
            '<div class="note">No exact state diff yet for this candidate. Replay this run once '
            'to capture the before/after app state for its failing step(s) - the next time this '
            f'report is generated, this section fills in automatically:<br><code>{_replay_command_hint(ep)}'
            '</code></div>'
        )

    rows = []
    for facet in _SNAPSHOT_FACETS:
        b = (before or {}).get(facet)
        a = after.get(facet)
        if b == a:
            continue
        if DeepDiff is not None:
            delta = DeepDiff(b, a, ignore_order=True, verbose_level=1)
            body = _diff_list_html(delta) if delta else '<p class="muted small">(values differ, but DeepDiff found no field-level detail)</p>'
        else:
            body = ('<p class="muted small">DeepDiff is not installed (pip install deepdiff) -- showing full '
                    'before/after instead:</p>'
                    f'<pre>Before:\n{esc(json.dumps(b, indent=2, default=str))}\n\n'
                    f'After:\n{esc(json.dumps(a, indent=2, default=str))}</pre>')
        rows.append(f'<p class="small"><code>{esc(facet)}</code> changed:</p>{body}')

    if not rows:
        return (
            '<div class="note">A replay captured this step\'s state, but none of the tracked facets '
            '(de_data/de_schema/se_schema/se_data/de_text/se_text) actually differ before vs. after - '
            'the failure likely depends on something outside those (e.g. GUI-only state not mirrored '
            'into the reactive store, or timing).</div>'
        )
    before_note = '' if before is not None else (
        '<p class="muted small">No "before" state was recorded (this was the very first replayed step, '
        'or the step before it failed to execute) -- only the failing state itself is shown.</p>'
    )
    return before_note + "".join(rows)


# HTML report

CSS = """
:root{--bg:#fdfdfc;--fg:#1c2530;--muted:#6b7785;--card:#f7f8f9;--card2:#f0f2f4;--line:#e3e7eb;
--red:#c62828;--amber:#a3660a;--green:#2e7d32;--blue:#1565c0;--grey:#8593a3;
--pass:#cfe8d2;--fail:#e05a5a;--walk:#f0b64a;--err:#8593a3}
@media(prefers-color-scheme:dark){:root{--bg:#12151a;--fg:#e7ebee;--muted:#a3aeba;--card:#1b2027;--card2:#212730;
--line:#2c333c;--red:#ff8b83;--amber:#f0b64a;--green:#82d29a;--blue:#7fb4ff;--pass:#2c4a33;--fail:#e05a5a;--walk:#b98520}}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px;background:var(--bg);color:var(--fg);
font:16px/1.6 -apple-system,"Segoe UI",system-ui,Roboto,sans-serif;max-width:900px;margin-inline:auto}
h1{font-size:23px;font-weight:650;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:16px;font-weight:650;margin:40px 0 12px;letter-spacing:-.005em}
h3{font-size:14px;font-weight:650;margin:20px 0 8px;color:var(--fg)}
p{margin:8px 0}
.muted{color:var(--muted)}.small{font-size:13px}
.verdict{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--grey);
border-radius:10px;padding:18px 20px;margin:18px 0}
.verdict.bad{border-left-color:var(--red)}.verdict.ok{border-left-color:var(--green)}.verdict.mid{border-left-color:var(--amber)}
.verdict .headline{font-size:18px;font-weight:650;margin-bottom:4px}
.start{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--blue);
border-radius:10px;padding:6px 20px 14px;margin:18px 0}
.counts{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 2px}
.count{background:var(--card2);border-radius:8px;padding:10px 16px;min-width:100px}
.count b{display:block;font-size:21px;font-weight:650}
details.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:14px 0;overflow:hidden}
details.card>summary{cursor:pointer;padding:14px 18px;font-weight:600;font-size:15px;list-style:none;
display:flex;align-items:baseline;gap:2px}
details.inner{margin:16px 0;padding-top:12px;border-top:1px solid var(--line)}
details.inner>summary{cursor:pointer;padding:2px 0 8px;font-weight:550;font-size:13.5px;color:var(--muted);list-style:none}
details>summary::-webkit-details-marker{display:none}
details.card>summary:before{content:"▸  ";color:var(--muted);font-size:12px}
details.card[open]>summary:before{content:"▾  "}
details.inner>summary:before{content:"▸  ";margin-right:2px}
details.inner[open]>summary:before{content:"▾  "}
.card .body{padding:2px 20px 20px}
.badge{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;margin-right:10px;
color:#fff;vertical-align:2px;letter-spacing:.02em;text-transform:uppercase}
.badge.likely{background:var(--red)}.badge.known{background:var(--blue)}.badge.watch{background:var(--amber)}
ol.repro{padding-left:22px}ol.repro li{margin:5px 0}
.hit{background:rgba(224,90,90,.16);border-radius:4px;padding:1px 6px;font-weight:600}
.tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:20px;padding:1px 8px;margin-left:6px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{text-align:left;padding:6px 10px;color:var(--muted);font-weight:600;font-size:12px;
border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.02em}
td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
code{font:12.5px ui-monospace,Consolas,monospace;background:var(--card2);border-radius:4px;padding:1px 5px}
.timeline{display:flex;flex-wrap:wrap;gap:1px;margin:6px 0 16px}
.timeline a,.timeline span{display:block;width:7px;height:18px;border-radius:1px;background:var(--pass)}
.timeline .f{background:var(--fail)}.timeline .w{background:var(--walk)}.timeline .e{background:var(--err)}
.timeline a.onset{outline:2px solid var(--fg);outline-offset:1px}
.legend span{display:inline-block;width:10px;height:10px;margin:0 4px 0 12px;vertical-align:-1px;border-radius:2px}
img.shot{max-width:100%;border:1px solid var(--line);border-radius:8px;margin-top:8px}
.note{background:var(--card2);border-left:4px solid var(--amber);padding:10px 14px;border-radius:6px;margin:12px 0}
.see{background:var(--card2);border-radius:8px;padding:12px 16px;margin:10px 0}
textarea{width:100%;min-height:220px;font:12.5px Consolas,monospace;background:var(--card2);color:var(--fg);
border:1px solid var(--line);border-radius:8px;padding:10px}
.shots{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0}
figure{margin:0;flex:1 1 280px;max-width:100%}
figure img.shot{width:100%;margin-top:0;cursor:zoom-in}
figure.big{flex-basis:100%}
figcaption{font-size:12px;color:var(--muted);margin-top:4px}
tr.onset td{background:rgba(224,90,90,.12)}
ul.seq{padding-left:20px}ul.seq li{margin:4px 0}
button,a.btn{font:inherit;font-size:13px;font-weight:550;padding:6px 14px;border:1px solid var(--blue);
background:transparent;color:var(--blue);border-radius:20px;cursor:pointer;text-decoration:none;
display:inline-block;margin-right:8px}
.whatchanged{background:var(--card2);border-left:4px solid var(--red);border-radius:8px;padding:14px 18px;margin:14px 0 18px}
.whatchanged h3{margin:0 0 10px;color:var(--red);font-size:13.5px;text-transform:uppercase;letter-spacing:.02em}
ul.difflist{list-style:none;padding:0;margin:2px 0 12px}
ul.difflist li{padding:6px 10px;border-radius:6px;margin:3px 0;font-size:13.5px;line-height:1.5}
ul.difflist li.diff-change{background:rgba(163,102,10,.10)}
ul.difflist li.diff-add{background:rgba(46,125,50,.10)}
ul.difflist li.diff-remove{background:rgba(198,40,40,.10)}
ul.difflist code{background:rgba(127,127,127,.12)}
ul.leads{list-style:none;padding:0;margin:8px 0 0}
ul.leads li{padding:8px 12px;border-radius:6px;margin:4px 0;font-size:13.5px;line-height:1.5;background:rgba(21,101,192,.08)}
.explain{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin:12px 0}
.explain h3{margin:0 0 2px;font-size:14.5px}
.explain p.muted{margin-top:0}
"""

COPY_JS = ("<script>document.querySelectorAll('button[data-copy]').forEach(function(b){b.addEventListener('click',function(){"
           "var t=document.getElementById(b.dataset.copy);t.select();"
           "if(navigator.clipboard){navigator.clipboard.writeText(t.value)}else{document.execCommand('copy')}"
           "b.textContent='Copied';});});"
           "document.querySelectorAll('figure img.shot').forEach(function(i){i.addEventListener('click',function(){i.parentElement.classList.toggle('big')})});</script>")


def esc(x: Any) -> str:
    return html.escape(str(x), quote=True)


def rich(text: str) -> str:
    """Escape a label and show the parts in 'quotes' (paths, names) as code."""
    return re.sub(r"&#x27;(.*?)&#x27;", r"<code>\1</code>", esc(text))


def _timeline_html(run: Run, anchors: dict[tuple, str], cfg: dict) -> str:
    cells = []
    for i, s in enumerate(run.steps):
        if run.kinds[i] == "walkback":
            cls = "w"
        elif s.get("action_succeeded") is False:
            cls = "e"
        elif _is_failing(s):
            cls = "f"
        else:
            cls = ""
        state = {"w": "automatic retry", "e": "test could not perform action", "f": "CHECK FAILED", "": "ok"}[cls]
        tip = esc(f"action {i}: {step_label(run, i, cfg)} [{state}]")
        anchor = anchors.get((str(run.path), i))
        if anchor:
            cells.append(f'<a class="{cls} onset" href="#{anchor}" title="{tip}"></a>')
        else:
            cells.append(f'<span class="{cls}" title="{tip}"></span>')
    return f'<div class="timeline">{"".join(cells)}</div>'


def _raw_rows(ep: Episode, cfg: dict) -> str:
    run, rows, i = ep.run, [], ep.start
    while i <= ep.end:
        j = i
        key = (run.kinds[i], run.steps[i].get("action_id"))
        while j + 1 <= ep.end and (run.kinds[j + 1], run.steps[j + 1].get("action_id")) == key:
            j += 1
        fails = sum(1 for k in range(i, j + 1) if _is_failing(run.steps[k]))
        span = f"{i}" if i == j else f"{i} to {j}"
        result = f"{fails} failing of {j - i + 1}" if j > i else ("FAIL" if fails else "ok")
        rows.append(f"<tr><td>{span}</td><td>{esc(_panel(run.steps[i], cfg))}</td>"
                    f"<td><code>{esc(key[1])}</code></td><td>{result}</td></tr>")
        i = j + 1
    return ("<table><tr><th>Action no.</th><th>Panel</th><th>Raw action id</th><th>Result</th></tr>"
            + "".join(rows) + "</table>")


def _repro_list(steps: list[dict], mark_first_failure: bool) -> str:
    out = ['<ol class="repro">']
    for s in steps:
        tail = ' <span class="tag">done automatically by the test tool</span>' if s["auto"] else ""
        label = s["label"].replace("Test tool: ", "")
        panel_tag = "" if s["panel"] in label else f' <span class="tag">{esc(s["panel"])}</span>'
        hint = f' <span class="tag">{esc(s["hint"])}</span>' if s.get("hint") else ""
        text = f'{rich(label)}{panel_tag}{hint}{tail}'
        if s["failed"] and mark_first_failure:
            text = f'<span class="hit">{text} &larr; the checks first fail here</span>'
        out.append(f"<li>{text}</li>")
    out.append("</ol>")
    return "".join(out)


def _rel(path: Path) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:
        return str(path)


def _image_data(path: Path, cfg: dict) -> tuple[bytes, str]:
    sc = cfg["screenshots"]
    if sc.get("max_width_px"):
        try:
            import io
            from PIL import Image
            with Image.open(path) as im:
                if im.width > sc["max_width_px"]:
                    im = im.resize((sc["max_width_px"], round(im.height * sc["max_width_px"] / im.width)))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=sc["jpeg_quality"], optimize=True)
                return buf.getvalue(), "image/jpeg"
        except Exception:
            pass  # Pillow missing or image unreadable: embed the original
    mime = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    return path.read_bytes(), mime


def _img_html(path: Path, caption: str, cfg: dict) -> str:
    if path.stat().st_size / 1e6 > cfg["screenshots"]["embed_max_mb"] and not cfg["screenshots"].get("max_width_px"):
        return f'<figure><a href="{esc(path.resolve().as_uri())}">{esc(caption)} (large file, click to open)</a></figure>'
    data, mime = _image_data(path, cfg)
    b64 = base64.b64encode(data).decode("ascii")
    return (f'<figure><img class="shot" src="data:{mime};base64,{b64}" alt="{esc(caption)}">'
            f'<figcaption>{rich(caption)}<br><code>{esc(path.name)}</code></figcaption></figure>')


def _screenshots_html(ep: Episode, cfg: dict) -> str:
    if not cfg["screenshots"].get("enabled"):
        return ""
    run = ep.run
    wanted = ([(ep.start - 1, "Before")] if ep.start > 0 else []) + [(ep.start, "First failure")]
    if ep.end != ep.start:
        wanted.append((ep.end, "End of this episode"))
    figs = []
    for i, label in wanted:
        for p in shots_for_step(run, i, cfg):
            cap = f"{label}: action {i}, {step_label(run, i, cfg)}"
            figs.append(_img_html(p, cap, cfg))
    if figs:
        return ('<h3>Screenshots (click to enlarge)</h3>'
                '<p class="muted small">Matched to the log by step counter and action name, so each image belongs to the action in its caption.</p>'
                '<div class="shots">' + "".join(figs) + "</div>")
    get_shots(run, cfg)
    found = [f for f, _ in (run.shots or {}).get(run.shot_iters[ep.start], [])][:4]
    return (f'<div class="note"><b>No screenshot could be matched to actions {", ".join(str(i) for i, _ in wanted)}.</b> '
            f'Looked in: {esc(", ".join(_rel(f) for f in _shot_folders(run, cfg)))}. Image files found: {run.shot_image_count}. '
            f'Files with counter {run.shot_iters[ep.start]} (the first failure): {esc(", ".join(found)) or "none"}. '
            f'If the files exist but are named differently, adjust "screenshots.filename_regex" in triage_config.json.</div>')


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


def _source_html(ep: Episode, cfg: dict) -> str:
    run = ep.run
    profile = f", action profile: {esc(run.weights_profile)}" if run.weights_profile else ""
    logs = find_run_logs(run, cfg)
    if logs:
        run_log = "Run log: " + ", ".join(
            f'<a href="{esc(f.resolve().as_uri())}"><code>{esc(_rel(f))}</code></a>' for f in logs)
    else:
        run_log = (f'<span class="muted">Run log: none found for {esc(run.run_id)} under '
                   f'{esc(cfg["run_logs"]["root"])}/</span>')
    return (f'<p class="small">Step history: <a href="{esc(run.path.resolve().as_uri())}"><code>{esc(_rel(run.path))}</code></a> '
            f'&middot; actions <b>{ep.start} to {ep.end}</b> &middot; first failure: action <b>{ep.start}</b> '
            f'&middot; test session {esc(run.run_id)}{profile}<br>{run_log}<br>'
            '<span class="muted">Action numbers are positions in the "steps" list of the history file, counting from 0. '
            'The links work on the computer that holds the logs.</span></p>')


def _sequence_html(ep: Episode, cfg: dict) -> str:
    run, limit = ep.run, cfg["episode_sequence"]["max_rows"]
    rows, i = [], ep.start
    while i <= ep.end:
        if run.kinds[i] == "walkback":
            j = i
            while j + 1 <= ep.end and run.kinds[j + 1] == "walkback":
                j += 1
            ids = [run.steps[k].get("action_id", "") for k in range(i, j + 1)]
            undo = sum(1 for a in ids if a.endswith("undo"))
            redo = sum(1 for a in ids if a.endswith("redo"))
            rows.append(f"<li><span class='muted'>Actions {i} to {j}: the test tool retried automatically "
                        f"(Undo {undo}x, Redo {redo}x) to see whether that clears the problem.</span></li>")
            i = j + 1
            continue
        st = run.steps[i]
        bad = _is_failing(st)
        hint = missing_detail(st.get("action_id", ""))
        hint_html = f' <span class="tag">{esc(hint)}</span>' if hint else ""
        res = '<span class="hit">checks failed</span>' if bad else '<span class="muted">checks ok</span>'
        label = step_label(run, i, cfg).replace("Test tool: ", "Test tool: ")
        rows.append(f"<li>Action {i}: {rich(label)}{hint_html} &rarr; {res}</li>")
        i += 1
    shown = rows[:limit]
    more = f"<li class='muted'>... and {len(rows) - limit} more entries (see the log)</li>" if len(rows) > limit else ""
    return ('<h3>What happened during this failure</h3><ul class="seq">' + "".join(shown) + more + "</ul>"
            f'<p class="small">{esc(outcome_text(ep))}</p>')


def _log_excerpt_html(ep: Episode, cfg: dict) -> str:
    run, ex = ep.run, cfg["log_excerpt"]
    lo, hi = max(0, ep.start - ex["context"]), min(len(run.steps) - 1, ep.start + ex["context"])
    has_value = any(run.steps[i].get("value") for i in range(lo, hi + 1))
    rows = []
    for i in range(lo, hi + 1):
        st = run.steps[i]
        val = str(st.get("value") or "")
        if len(val) > ex["value_max_chars"]:
            val = val[:ex["value_max_chars"]] + "..."
        note = st.get("description", "") if run.kinds[i] != "action" else ""
        cls = ' class="onset"' if i == ep.start else ""
        rows.append(f"<tr{cls}><td>{i}</td><td>{esc(_panel(st, cfg))}</td><td><code>{esc(st.get('action_id', ''))}</code></td>"
                    f"<td>{esc(st.get('testid') or '')}</td>" + (f"<td>{esc(val)}</td>" if has_value else "") +
                    f"<td>{'yes' if st.get('action_succeeded') is not False else 'NO'}</td>"
                    f"<td>{'ok' if st.get('passed') is not False else 'FAIL'}</td><td>{esc(note)}</td></tr>")
    head = ("<tr><th>Action</th><th>Panel</th><th>Raw action id</th><th>Test id</th>"
            + ("<th>Value typed</th>" if has_value else "") + "<th>Action worked</th><th>Checks</th><th>Note</th></tr>")
    foot = "" if has_value else '<p class="muted small">The log has no typed values for these actions (its "value" field is empty).</p>'
    return "<table>" + head + "".join(rows) + "</table>" + foot


def _what_changed_html(ep: Episode, cfg: dict) -> str:
    """Top section of a card: the exact diff of what changed in the data or schema (from the state
    snapshots, if any), and, when the run has cause-tracking fields, one sentence per broken rule
    naming the earlier action most likely responsible.
    """
    diff = _state_diff_html(ep, cfg)
    leads = lead_lines(ep, cfg, lambda i: step_label(ep.run, i, cfg), parse_action, lambda st: _panel(st, cfg))
    if "note" not in diff and "difflist" not in diff:
        pass
    lead_html = ("<ul class=\'leads\'>" + "".join(f"<li>{x}</li>" for x in leads) + "</ul>") if leads else ""
    return f'<div class="whatchanged"><h3>What changed, and why it likely happened</h3>{diff}{lead_html}</div>'


def _card_html(c: Candidate, cfg: dict, open_card: bool) -> str:
    ep, run = c.primary, c.primary.run
    runs_seen = sorted({str(e.run.path) for e in c.episodes})
    steps = repro_steps(ep, cfg)
    full = repro_steps(ep, cfg, full=True)
    why, tech = episode_evidence(ep, cfg)
    proj = cfg["project"]

    out = [f'<details class="card" id="c{c.gid}"{" open" if open_card else ""}><summary>'
           f'<span class="badge {c.tier}">{TIER_LABEL[c.tier]}</span>#{c.gid}  {esc(episode_title(ep, cfg))}</summary>'
           '<div class="body">']
    out.append(f'<p class="muted small">Schema: <b>{esc(run.schema)}</b> &middot; Data file: <b>{esc(run.data_file)}</b></p>')
    out.append(_source_html(ep, cfg))

    if ep.known_bug:
        out.append(f'<p><b>Already known:</b> {esc(ep.known_bug["id"])}: {esc(ep.known_bug["summary"])}.</p>')

    # headline evidence first, supporting detail below
    out.append(_what_changed_html(ep, cfg))

    out.append('<h3>What you should see if this is real</h3><div class="see">')
    if ep.invariants:
        for inv_id, name in sorted(ep.invariants.items()):
            out.append(f"<p><b>Expected:</b> {esc(rule_text(inv_id, name, cfg))}.<br>"
                       f"<b>Look for:</b> {esc(look_for_text(inv_id, cfg))}</p>")
    else:
        out.append(f"<p><b>Look for:</b> {esc(look_for_text('', cfg))} "
                   "Compare the panels after the last action: they should show the same content.</p>")
    out.append("</div>")

    out.append("<h3>Why this was flagged</h3><ul>" + "".join(f"<li>{esc(w)}</li>" for w in why) + "</ul>")

    full_causal = causal_html(ep, cfg, lambda i: step_label(ep.run, i, cfg), parse_action, lambda s: _panel(s, cfg))
    if full_causal:
        out.append(f'<details class="inner"><summary>Full analysis: every action considered, and why '
                   f'it was or wasn\'t the lead</summary>{full_causal}</details>')
    out.append(f'<details class="inner"><summary>Action-by-action timeline for this failure '
               f'({ep.end - ep.start + 1} actions)</summary>{_sequence_html(ep, cfg)}</details>')
    out.append(_screenshots_html(ep, cfg))

    out.append(f'<h3>How to reproduce</h3><p>Open <b>{esc(proj["app_url"])}</b>, choose the example schema '
               f'<b>{esc(run.schema)}</b> and upload the data file <b>{esc(run.data_file)}</b>, then do these actions in order:</p>')
    out.append(_repro_list(steps, True))
    out.append(f'<details class="inner"><summary>Doesn\'t reproduce? Full sequence from a fresh load ({len(full)} actions)</summary>'
               '<p class="muted small">Every action of the test session up to the first failure. '
               'Some changes are only visible after earlier edits, so the short list above can miss them.</p>'
               + _repro_list(full, True) + "</details>")
    if needs_panel_caveat(steps, run, cfg):
        out.append(f'<div class="note">{esc(cfg["caveats"]["undo_redo_across_panels"])}</div>')

    out.append(f'<details class="inner"><summary>Log excerpt around the first failure (actions '
               f'{max(0, ep.start - cfg["log_excerpt"]["context"])} to {min(len(run.steps) - 1, ep.start + cfg["log_excerpt"]["context"])})</summary>'
               f'{_log_excerpt_html(ep, cfg)}</details>')

    issue = draft_issue(ep, cfg, steps)
    out.append(f'<details class="inner"><summary>Draft GitHub issue (unconfirmed until you reproduce it)</summary>'
               f'<textarea id="issue{c.gid}" readonly>{esc(issue)}</textarea>'
               f'<p><button data-copy="issue{c.gid}">Copy text</button>'
               f'<a class="btn" href="{esc(proj["new_issue_url"])}" target="_blank" rel="noopener">Open new GitHub issue</a></p></details>')

    tech_html = "".join(f"<li>{esc(t)}</li>" for t in tech)
    seen = (f"<p>Also seen in {len(runs_seen)} test sessions: " + ", ".join(f"<code>{esc(r)}</code>" for r in runs_seen) + "</p>") if len(runs_seen) > 1 else ""
    ids = ", ".join(f"<code>{esc(i)}</code>" for i in sorted(ep.invariants)) or "not recorded"
    out.append('<details class="inner"><summary>Technical details (test tool internals)</summary>'
               f'<p class="small">Rule IDs: {ids}. For an exact replay use <code>replay_runner.py</code> up to action {ep.start}.</p>'
               f'<ul>{tech_html}</ul>{seen}{_raw_rows(ep, cfg)}</details>')
    out.append("</div></details>")
    return "".join(out)


def _group_table(rows: list[tuple[str, int, str]], limit: int, headers: tuple[str, str, str]) -> str:
    rows = sorted(rows, key=lambda r: -r[1])
    body = "".join(f"<tr><td>{esc(a)}</td><td>{n}</td><td>{esc(ex)}</td></tr>" for a, n, ex in rows[:limit])
    more = f'<tr><td colspan="3" class="muted">... and {len(rows) - limit} more</td></tr>' if len(rows) > limit else ""
    return f"<table><tr><th>{headers[0]}</th><th>{headers[1]}</th><th>{headers[2]}</th></tr>{body}{more}</table>"


def render_html(runs: list[Run], all_eps: list[Episode], cands: list[Candidate], cfg: dict,
                window_note: str = "", mode: str = "single") -> str:
    by_tier = Counter(c.tier for c in cands)
    blips = [e for e in all_eps if e.tier == "blip"]
    artifacts = [e for e in all_eps if e.tier == "artifact"]
    total_steps = sum(len(r.steps) for r in runs)
    limit = cfg["report"]["table_max_rows"]

    harness: dict[str, list[str]] = defaultdict(list)
    for r in runs:
        for i, s in enumerate(r.steps):
            if s.get("action_succeeded") is False:
                harness[step_label(r, i, cfg, include_path=False)].append(f"{r.run_id}, action {i}")

    if by_tier["likely"]:
        css, head = "bad", f"{by_tier['likely']} likely bug candidate(s) to check"
    elif by_tier["watch"] or by_tier["known"]:
        css, head = "mid", "No new likely bugs, but some findings are worth a glance"
    else:
        css, head = "ok", "No bug candidates found in this scan"
    schemas = sorted({r.schema for r in runs})
    session_word = "session" if len(runs) == 1 else "sessions"
    sub = (f"{len(runs)} automated test {session_word}, {total_steps} actions in total. Schema(s): "
           + esc(", ".join(schemas[:4]) + (" ..." if len(schemas) > 4 else ""))
           + (f"<br>{esc(window_note)}" if window_note else ""))

    anchors = {(str(c.primary.run.path), c.primary.start): f"c{c.gid}" for c in cands}
    for c in cands:
        for e in c.episodes[1:]:
            anchors[(str(e.run.path), e.start)] = f"c{c.gid}"

    first = next((c for c in cands if c.tier == "likely"), cands[0] if cands else None)
    pick = (f'<li>Pick a card marked <b>Likely bug</b>. Good first choice: <a href="#c{first.gid}">#{first.gid}</a>.</li>'
            if first else "<li>There is nothing to pick this time.</li>")
    proj = cfg["project"]

    P = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><title>MetaConfigurator consistency test report</title>"
         f"<style>{CSS}</style></head><body>",
         "<h1>MetaConfigurator consistency test report</h1>",
         f'<div class="verdict {css}"><div class="headline">{esc(head)}</div><div class="muted">{sub}</div>'
         '<div class="counts">'
         f'<div class="count"><b>{by_tier["likely"]}</b>Likely bugs</div>'
         f'<div class="count"><b>{by_tier["watch"]}</b>Worth a look</div>'
         f'<div class="count"><b>{by_tier["known"]}</b>Known bugs seen again</div>'
         f'<div class="count"><b>{len(blips)}</b>Brief glitches</div></div></div>',
         '<div class="start"><h2>New here? Start here</h2>'
         "<p>An automated tool clicked through MetaConfigurator at random (editing the schema and data, switching views, "
         "Undo/Redo) and, after every action, checked that the different views still agree with each other. "
         "Wherever they disagreed, the report lists a <b>candidate</b> bug. Candidates are "
         "<b>not confirmed</b>: the tool can raise false alarms.</p><ol>"
         f"{pick}"
         f"<li>Open <b>{esc(proj['app_url'])}</b>, load the schema and data file named on the card, and follow the numbered actions.</li>"
         "<li>If you see what the card describes under <i>What you should see</i>, it is real. Use the drafted GitHub issue text on the card. "
         "If you do not, that is useful too: it helps us improve the tool.</li></ol></div>",
         f"<h2>Timeline of the test {session_word}</h2><p class='muted small legend'>Each block is one action. "
         "<span style='background:var(--pass)'></span>checks ok<span style='background:var(--fail)'></span>checks failed"
         "<span style='background:var(--walk)'></span>automatic Undo/Redo retry<span style='background:var(--err)'></span>test could not perform the action. "
         "Outlined blocks link to their card.</p>"]

    for r in runs:
        P.append(f"<div class='small'><b>{esc(r.schema)}</b> <span class='muted'>session {esc(r.run_id)}</span></div>")
        P.append(_timeline_html(r, anchors, cfg))

    if mode == "multi":
        explain = invariant_explanations_html([e for e in all_eps if e.tier in TIER_ORDER], cfg,
                                              parse_action, lambda s: _panel(s, cfg))
        if explain:
            P.append(explain)

    P.append("<h2>Candidates</h2>")
    if not cands:
        P.append("<p>None.</p>")
    for n, c in enumerate(cands):
        P.append(_card_html(c, cfg, open_card=(n == 0 and c.tier == "likely")))

    P.append(f"<h2>Brief glitches ({len(blips)})</h2>")
    if blips:
        g: dict[str, list[Episode]] = defaultdict(list)
        for e in blips:
            g[step_label(e.run, trigger_index(e), cfg, include_path=False)].append(e)
        rows = [(k, len(v), f"{v[0].run.run_id}, action {v[0].start}") for k, v in g.items()]
        P.append("<details class='card'><summary>Short failures that fixed themselves (usually not worth a look)</summary><div class='body'>"
                 + _group_table(rows, limit, ("First failed after", "Times", "Example")) + "</div></details>")
    else:
        P.append("<p>None.</p>")

    P.append(f"<h2>Ignored ({len(artifacts)})</h2>")
    if artifacts:
        rows = [("; ".join(sorted(e.invariants)), 1, f"{e.run.run_id}, action {e.start}") for e in artifacts]
        P.append("<details class='card'><summary>Failures of checks that have been retired</summary><div class='body'>"
                 + _group_table(rows, limit, ("Check", "Times", "Example")) + "</div></details>")
    else:
        P.append("<p>None.</p>")

    P.append("<h2>Problems in the test automation</h2>")
    if harness:
        rows = [(k, len(v), v[0]) for k, v in harness.items()]
        P.append("<details class='card'><summary>Actions the test tool could not perform (not MetaConfigurator bugs)</summary>"
                 "<div class='body'>" + _group_table(rows, limit, ("Action", "Times", "Example")) + "</div></details>")
    else:
        P.append("<p>None.</p>")

    seen = {}
    for e in all_eps:
        seen.update(e.invariants)
    P.append("<h2>Consistency rules checked in this scan</h2><table><tr><th>ID</th><th>What must always hold</th></tr>")
    for inv_id, name in sorted(seen.items()):
        P.append(f"<tr><td><code>{esc(inv_id)}</code></td><td>{esc(rule_text(inv_id, name, cfg))}</td></tr>")
    P.append(f"</table>{COPY_JS}</body></html>")
    return "".join(P)


# Entry point

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Triage mutation-testing history logs into an HTML report.")
    parser.add_argument("paths", type=Path, nargs="*", help="Folders and/or history.json files (folders are scanned recursively). Default: mutation_history.")
    parser.add_argument("--last-hours", type=float, default=None, help="Only runs that started within the last N hours, e.g. 12.")
    parser.add_argument("--no-open", action="store_true", help="Do not open the report in the browser afterwards.")
    parser.add_argument("--since", default=None, help="Only runs that started at or after this time, e.g. \"2026-09-27 20:00\".")
    parser.add_argument("--until", default=None, help="Only runs that started at or before this time, e.g. \"2026-09-28 09:00\".")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to triage_config.json.")
    parser.add_argument("--html", type=Path, default=None, help="Output HTML path (default from config).")
    parser.add_argument("--out", type=Path, default=None, help="Optional path to also write the findings as JSON.")
    parser.add_argument("--mode", choices=("auto", "single", "multi"), default="auto",
                        help="Report layout. 'single': per-failure detail (what changed, which invariant, "
                             "screenshots) -- no cross-session material. 'multi': adds a section that "
                             "correlates each invariant's failures across sessions and states what's common "
                             "about them. 'auto' (default): 'single' when exactly one run is scanned, "
                             "'multi' when more than one is.")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.last_hours is not None and (args.since or args.until):
        print("[!] Use either --last-hours or --since/--until, not both.", file=sys.stderr)
        return 2
    try:
        since = parse_time_arg(args.since, cfg) if args.since else None
        until = parse_time_arg(args.until, cfg) if args.until else None
    except ValueError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2
    if args.last_hours is not None:
        since = datetime.now() - timedelta(hours=args.last_hours)

    files: list[Path] = []
    for p in (args.paths or [Path(x) for x in cfg["defaults"]["paths"]]):
        try:
            files.extend(find_log_files(p))
        except FileNotFoundError as e:
            print(f"[!] {e}", file=sys.stderr)
    files = sorted(dict.fromkeys(f.resolve() for f in files))
    if not files:
        print("No history.json files found.", file=sys.stderr)
        return 1

    runs: list[Run] = []
    outside = 0
    for p in files:
        try:
            r = load_run(p, cfg)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[!] Skipping {p}: {e}", file=sys.stderr)
            continue
        if (since and r.started < since) or (until and r.started > until):
            outside += 1
            continue
        runs.append(r)
    if not runs:
        print(f"No runs inside the time window ({outside} run(s) found outside it).", file=sys.stderr)
        return 1
    runs.sort(key=lambda r: r.started)

    window_note = ""
    if since or until:
        window_note = (f"Time window: {since.strftime('%Y-%m-%d %H:%M') if since else 'beginning'} to "
                       f"{until.strftime('%Y-%m-%d %H:%M') if until else 'now'}. "
                       f"{len(runs)} run(s) included, {outside} outside the window.")

    all_eps: list[Episode] = []
    for r in runs:
        eps = build_episodes(r, cfg)
        for e in eps:
            assign_tier(e, cfg)
        all_eps.extend(eps)

    cands = group_candidates(all_eps, cfg)
    mode = args.mode if args.mode != "auto" else ("single" if len(runs) == 1 else "multi")
    out_path = args.html or Path(cfg["report"]["default_html_path"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(runs, all_eps, cands, cfg, window_note, mode), encoding="utf-8")

    tiers = Counter(c.tier for c in cands)
    print(f"Report written to {out_path.resolve()} ({mode}-session mode)")
    if not args.no_open and cfg["report"].get("open_in_browser"):
        webbrowser.open(out_path.resolve().as_uri())
    if window_note:
        print(window_note)
    print(f"Scanned {len(runs)} run(s), {sum(len(r.steps) for r in runs)} steps: "
          f"{tiers['likely']} likely bug(s), {tiers['watch']} worth a look, {tiers['known']} known-bug sighting(s), "
          f"{sum(1 for e in all_eps if e.tier == 'blip')} blip(s), "
          f"{sum(1 for e in all_eps if e.tier == 'artifact')} filtered.")

    if args.out:
        payload = [{
            "run": e.run.run_id, "schema": e.run.schema, "start": e.start, "end": e.end,
            "outcome": e.outcome, "tier": e.tier, "invariants": e.invariants,
            "known_bug": e.known_bug["id"] if e.known_bug else None,
            "title": episode_title(e, cfg),
        } for e in all_eps]
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Findings JSON written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())