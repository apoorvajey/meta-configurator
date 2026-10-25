"""Limits that bound the repair, invalid-schema recovery and stuck-run handling of a mutation-loop run."""
from __future__ import annotations

from dataclasses import dataclass


# The values are set by the "loop_limits" block of run_config.json (see profile_config.resolve_loop_limits);
# the field defaults below are the fallback when that block is missing or invalid.
@dataclass(frozen=True)
class LoopLimits:
    max_undo_attempts: int = 15                # undos of one walk-back; the known-good state must be reachable within this many edits
    unchanged_undo_limit: int = 2              # a walk-back stops after this many undos in a row that change nothing
    max_stuck_steps: int = 5                   # consecutive steps failing the same invariant before the run is abandoned (0 = never)
    max_lingering_invalid_episodes: int = 0    # valid->invalid schema transitions per run that may persist before being undone
    max_steps_in_invalid_schema: int = 3       # further steps such an episode may persist
    max_recovery_undos: int = 6                # undos of one invalid-schema recovery
