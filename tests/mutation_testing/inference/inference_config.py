"""Loader for inference_config.json, which holds every tunable number of the inference engine and the threshold sweep.

InferenceConfig has no defaults, so a parameter missing from the file is an error, not a silent fallback. The file
has "_parameters" (documentation), "default" (the complete set) and one entry per named profile with only the
parameters it changes; keys starting with "_" are ignored.

    cfg = load_config(profile="strict")
    cfg = with_overrides(cfg, promotion_threshold=0.9)  # CLI overrides; None keeps the value
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "inference_config.json"
BASE_PROFILE = "default"


@dataclass(frozen=True)
class InferenceConfig:
    # inference / promotion
    min_observations: int                  # per dataset: pairs a category needs before templates run on it
    min_rule_observations: int             # per dataset: applicable observations a guarded rule needs
    min_observations_for_promotion: int    # per dataset: observations a candidate needs to be promotable
    promotion_threshold: float             # per-dataset support a candidate needs (0 < t <= 1)
    change_count_min_share: float          # change_count: share of steps the usual count k must cover
    max_examples: int                      # hold/fail examples kept per finding and shown per report line
    guard_min_gain: float                  # guarded rules: support a guard must add over the unguarded rule, in some dataset
    edit_distance_work_limit: int          # edit_distance: units of work one pair may cost before it gives no observation
    # threshold sweep
    sweep_start: float
    sweep_end: float
    sweep_step: float
    # metadata (set by the loader, not part of the JSON values)
    profile: str = BASE_PROFILE

    def __post_init__(self) -> None:
        for name in ("min_observations", "min_rule_observations",
                     "min_observations_for_promotion", "max_examples", "edit_distance_work_limit"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"config '{name}' must be an integer >= 1, got {v!r}")
        for name in ("promotion_threshold", "change_count_min_share", "sweep_start", "sweep_end",
                     "guard_min_gain"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 1:
                raise ValueError(f"config '{name}' must be a number in (0, 1], got {v!r}")
            object.__setattr__(self, name, float(v))
        if isinstance(self.sweep_step, bool) or not isinstance(self.sweep_step, (int, float)) \
                or self.sweep_step <= 0:
            raise ValueError(f"config 'sweep_step' must be a number > 0, got {self.sweep_step!r}")
        object.__setattr__(self, "sweep_step", float(self.sweep_step))
        if self.sweep_start > self.sweep_end:
            raise ValueError("config 'sweep_start' must not exceed 'sweep_end'")


_VALUE_KEYS = {f.name for f in fields(InferenceConfig)} - {"profile"}


def _strip_docs(section: dict) -> dict:
    return {k: v for k, v in section.items() if not k.startswith("_")}


def load_config(path: str | Path | None = None, profile: str | None = None) -> InferenceConfig:
    """Loads `profile` (default: "default") from the JSON file at `path`."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"Inference config not found: {cfg_path}")
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    if BASE_PROFILE not in raw:
        raise KeyError(f"{cfg_path}: missing the '{BASE_PROFILE}' profile")

    values = _strip_docs(raw[BASE_PROFILE])
    name = profile or BASE_PROFILE
    if name != BASE_PROFILE:
        if name not in raw or name.startswith("_"):
            available = sorted(k for k in raw if not k.startswith("_"))
            raise KeyError(f"{cfg_path}: unknown profile '{name}' (available: {available})")
        values.update(_strip_docs(raw[name]))

    unknown, missing = set(values) - _VALUE_KEYS, _VALUE_KEYS - set(values)
    if unknown:
        raise KeyError(f"{cfg_path}: unknown parameter(s) {sorted(unknown)}")
    if missing:
        raise KeyError(f"{cfg_path}: profile '{BASE_PROFILE}' must define {sorted(missing)}")
    return InferenceConfig(profile=name, **values)


def with_overrides(cfg: InferenceConfig, **overrides) -> InferenceConfig:
    """Copy of `cfg` with every non-None override applied (and re-validated)."""
    return replace(cfg, **{k: v for k, v in overrides.items() if v is not None})