"""Comparison of a replay with the original run: state hashes, snapshots and divergence checks."""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path


def _is_repair_record(step: dict) -> bool:
    """Repair clicks, mode switches around a repair and clear-text auto-undos use the "repair:" prefix and carry no seed or input."""
    return (step.get("action_id") or "").startswith("repair:")


@dataclass
class ReproducibilityCheck:
    """Whether a history.json carries everything replay needs to reproduce the run.

    ok means the recorded data is complete, not that a replay matched; see reproducibility_status().
    """
    ok: bool
    reason: str
    action_steps: int                                     # non-repair steps with a testid
    missing_steps: list[int] = field(default_factory=list)  # 1-based step numbers lacking data


def check_reproducibility(steps: list[dict]) -> ReproducibilityCheck:
    """A log is reproducible if every action step (a testid, not a "repair:" step) has an "input_value" key and a
    non-null "step_seed". The key's presence is what counts, since its value may be None (the executor generated it).
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
        reason = "log has no recorded input values/seeds, replay can only approximate this run"
    else:
        reason = (f"{len(missing)}/{action_steps} action step(s) lack recorded input values/seeds "
                  f"(first at step {missing[0]}), replay is not exact from there")
    return ReproducibilityCheck(False, reason, action_steps, missing)


def reproducibility_status(check: ReproducibilityCheck,
                           outcome: "ReplayOutcome | None" = None) -> str:
    """Verdict for a badge: "not_reproducible" (values missing), "unverified" (complete, not replayed),
    "verified" (replayed without divergence) or "diverged".
    """
    if not check.ok:
        return "not_reproducible"
    if outcome is None:
        return "unverified"
    return "verified" if outcome.diverged_at is None else "diverged"


@dataclass
class ReplayOutcome:
    """Result of one ReplayRunner.run() call.

    steps_run is the number of steps the call was asked to replay; it does not mean they all ran. Use
    quit_early and steps_completed to tell a finished replay from one stopped early with 'q'.
    """
    steps_run: int
    failing_steps: int = 0                # steps where any invariant failed
    diverged_at: int | None = None        # first step where replay provably left the original run
    state_snapshots_saved: int = 0        # failing steps whose before/after state was written to disk
    quit_early: bool = False              # the user pressed 'q' before the end
    steps_completed: int = 0              # steps that actually ran (== steps_run unless the replay was stopped early)

    @property
    def stopped_early(self) -> bool:
        """True if the replay did not reach its last step. Pressing 'q' at the final step does not count: every
        step has run by then, so the replay is complete however it was driven (--until, --interactive, ...)."""
        return self.steps_completed < self.steps_run


def _state_hash(snapshot: dict | None) -> str | None:
    if not snapshot:
        return None
    core = {k: snapshot.get(k) for k in ("de_data", "de_schema", "se_schema", "se_data")}
    return hashlib.sha1(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()


def _dump_state_snapshot(snapshot: dict | None, path: Path) -> None:
    """Write a full build_snapshot() dict (not just its hash) to `path`."""
    if snapshot is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, default=str)