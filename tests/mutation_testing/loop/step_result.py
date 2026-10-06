"""Result record of one mutation step."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from tests.invariants.invariants_core import InvariantReport


@dataclass
class StepResult:
    action_id: str
    description: str
    kwargs: dict
    action_succeeded: bool
    action_error: str | None
    report: InvariantReport | None
    screenshot_path: str | None = None
    available_actions: list | None = None
    timestamp: float = field(default_factory=time.time)
    snapshot: dict | None = None

    @property
    def invariants_passed(self) -> bool:
        if not self.action_succeeded or self.report is None:
            return False
        return self.report.passed
