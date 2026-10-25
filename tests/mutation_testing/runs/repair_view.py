"""The repairs of a recorded run, laid out as steps for the reports.

history.json keeps a repair on the step that triggered it ("repairs", see loop/repair_engine.repair_block). The run
summary, the batch summary and the triage reports count and classify a repair click as a step of its own (an undo that
left a rule failing is an "auto-undo" that failed), so they read the history through expand_repairs().
"""
from __future__ import annotations


def expand_repairs(steps: list[dict]) -> list[dict]:
    """`steps` with one extra record after each step for every click of its repairs ("repair:<testid>" records).

    The new records carry what the reports read from a step (description, passed, failed/checked invariants, the step
    number of the step that triggered them); they are not replayable and not written back to history.json.
    """
    out: list[dict] = []
    for s in steps:
        out.append(s)
        for block in s.get("repairs") or []:
            clicks = block.get("clicks") or []
            for k, c in enumerate(clicks):
                rec = {
                    "action_id": f"repair:{c.get('testid', '')}",
                    "description": c.get("description", ""),
                    "testid": c.get("testid", ""),
                    "value": "",
                    "mode": s.get("mode", ""),
                    "action_succeeded": True,
                    "passed": c.get("passed"),
                    "step": s.get("step"),
                    "role": c.get("role"),
                    "persisting_failures": block.get("persisting_failures", []),
                    "failed_invariants": c.get("failed_invariants", []),
                    "checked_invariants": c.get("checked_invariants", []),
                }
                if c.get("recovery_reason"):
                    rec["recovery_reason"] = c["recovery_reason"]
                if s.get("replayed"):
                    rec["replayed"] = True
                    rec["replay_passed"] = c.get("passed")
                if k == len(clicks) - 1 and block.get("facet_hashes"):
                    rec["facet_hashes"] = block["facet_hashes"]   # the state the repair left behind
                out.append(rec)
    return out
