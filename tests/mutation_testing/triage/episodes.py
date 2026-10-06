"""Episodes of consecutive failing steps, their tiers, titles and evidence."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from tests.mutation_testing.triage.run_loading import QUOTED_RE, Run
from tests.mutation_testing.triage.action_text import missing_detail, translate_action


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
    retired = set(cfg["retired_invariants"]) | set(cfg.get("suspect_invariants", {}))
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


def short_title(ep: Episode, cfg: dict) -> str:
    what, _ = repeated_undo_target(ep, cfg)
    if what:
        return f"Inconsistent state after undoing: {what}"
    return f"Inconsistent state after: {step_label(ep.run, trigger_index(ep), cfg)}"


def _panel(step: dict, cfg: dict) -> str:
    return cfg["panels"].get(step.get("mode", ""), step.get("mode", "") or "?")


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
        "hint": missing_detail(run.steps[i].get("action_id", ""), run.steps[i]),
    } for i in idx]
