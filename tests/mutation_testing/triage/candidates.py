"""Drafts a GitHub issue for an episode and locates its state snapshots."""
from __future__ import annotations

from pathlib import Path
from tests.mutation_testing.triage.episodes import (
    Episode,
    look_for_text,
    rule_text,
    short_title,
)
from tests.mutation_testing.triage.run_loading import Run


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


def _snapshot_dir(run: Run, cfg: dict) -> Path:
    root = cfg.get("state_snapshots", {}).get("root", "replay_snapshots")
    return Path(root) / run.run_id
