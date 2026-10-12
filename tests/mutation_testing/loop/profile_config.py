"""Resolve config/run_config.json into the settings of a mutation-loop run: action-weights profile, schema/data pair,
step count, screenshots, run mode and console verbosity. Each resolver falls back to a default and prints why on a
missing file, malformed JSON or a missing or unrecognised value, so a config typo never crashes a run. The accepted
values are documented in the "_*" keys of run_config.json.
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

from tests.mutation_testing.loop.action_weights import set_action_weights_profile

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "run_config.json"

# Accepted values for run_config.json "action_weights_profile". Each needs a config/action_weights*.json
# file; keep run_config.json's "_action_weights_profile_options" note in sync.
_KNOWN_PROFILES = {"default", "textview_focus", "schema_focus", "data_focus", "random"}
_KNOWN_RUN_MODES = {"background", "watch_failures", "interactive"}


def load_run_config() -> dict:
    """run_config.json as a dict; {} if it is missing or malformed, which every resolver treats as "nothing configured"."""
    if not _CONFIG_PATH.exists():
        print(f"[config] {_CONFIG_PATH.name} not found in the config folder — using all defaults")
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[config] Could not parse {_CONFIG_PATH.name} ({e}) — using all defaults")
        return {}


def apply_configured_profile(config: dict | None = None) -> str:
    """Apply the "action_weights_profile" setting and return the profile name applied.

    Pass an already-loaded `config` to avoid reading the file again.
    """
    config = config if config is not None else load_run_config()
    profile = config.get("action_weights_profile") or "default"

    if profile not in _KNOWN_PROFILES:
        print(f"[config] Unrecognized action_weights_profile '{profile}' "
              f"(known: {sorted(_KNOWN_PROFILES)}) — using default profile instead")
        set_action_weights_profile(None)
        return "default"

    set_action_weights_profile(None if profile == "default" else profile)
    print(f"[config] Applied action-weights profile: '{profile}'")
    return profile


def resolve_schema_choice(
    schema_data_pairs: list[tuple[str, Path]], config: dict | None = None
) -> tuple[str, Path]:
    """Resolve "schema" against schema_data_pairs (passed in, since mutation_loop imports this module).

    "random", missing or empty picks a random pair; an exact schema name picks that pair; anything else falls back to
    random with a warning. Returns (schema_option_name, data_file_path).
    """
    config = config if config is not None else load_run_config()
    choice = (config.get("schema") or "random").strip()

    valid_names = {name for name, _ in schema_data_pairs}

    if choice == "random":
        picked = random.choice(schema_data_pairs)
        print(f"[config] schema: 'random' -> picked '{picked[0]}'")
        return picked

    if choice in valid_names:
        picked = next(pair for pair in schema_data_pairs if pair[0] == choice)
        print(f"[config] schema: fixed selection -> '{picked[0]}'")
        return picked

    print(f"[config] Unrecognized schema '{choice}' in {_CONFIG_PATH.name} "
          f"(known: {sorted(valid_names)}) — falling back to random")
    picked = random.choice(schema_data_pairs)
    print(f"[config]   -> picked '{picked[0]}'")
    return picked


def resolve_run_mode(config: dict | None = None) -> str | None:
    """One of "background", "watch_failures", "interactive" if validly set, else None.

    None tells the caller to ask at the terminal prompt, so the mode can still be chosen per run.
    """
    config = config if config is not None else load_run_config()
    mode = config.get("run_mode")
    if mode is None:
        return None
    if mode not in _KNOWN_RUN_MODES:
        print(f"[config] Unrecognized run_mode '{mode}' (known: {sorted(_KNOWN_RUN_MODES)}) "
              f"— falling back to the interactive prompt")
        return None
    return mode


def resolve_num_steps(config: dict | None = None, default: int = 100) -> int:
    """Reads "num_steps". Falls back to `default` on a missing/invalid value."""
    config = config if config is not None else load_run_config()
    n = config.get("num_steps", default)
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        print(f"[config] Invalid num_steps '{n}' — using default {default}")
        return default
    return n


def resolve_screenshots(config: dict | None = None, default: bool = True) -> bool:
    """Reads "screenshots". Falls back to `default` on a missing/non-boolean value."""
    config = config if config is not None else load_run_config()
    val = config.get("screenshots", default)
    if not isinstance(val, bool):
        print(f"[config] Invalid screenshots value '{val}' — using default {default}")
        return default
    return val


def resolve_screenshots_failures_only(config: dict | None = None, default: bool = False) -> bool:
    """Reads "screenshots_failures_only": with screenshots on, only steps whose invariants failed get one."""
    config = config if config is not None else load_run_config()
    val = config.get("screenshots_failures_only", default)
    if not isinstance(val, bool):
        print(f"[config] Invalid screenshots_failures_only value '{val}' — using default {default}")
        return default
    return val


VERBOSE_ENV = "MT_VERBOSE"


def resolve_verbose(config: dict | None = None, argv: list[str] | None = None, default: bool = False) -> bool:
    """Whether a run prints everything to the terminal (True) or only invariant-related output (False).

    The first of these that is set decides: --verbose / --quiet on the command line, the MT_VERBOSE environment
    variable ("1"/"true"/"yes" or "0"/"false"/"no"; the batch runner sets it for --verbose), the "verbose" key of
    run_config.json, then `default`. The full output of a quiet run is always kept in tests/output/runs/<run_id>/console.log.
    """
    argv = sys.argv[1:] if argv is None else argv
    if "--verbose" in argv:
        return True
    if "--quiet" in argv:
        return False
    env = os.environ.get(VERBOSE_ENV, "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    config = config if config is not None else load_run_config()
    val = config.get("verbose", default)
    if not isinstance(val, bool):
        print(f"[config] Invalid verbose value '{val}' — using default {default}")
        return default
    return val


def _resolve_probability(config: dict | None, key: str, default: float) -> float:
    """Reads a 0.0-1.0 probability. Falls back to `default` on a missing, non-numeric or out-of-range value."""
    config = config if config is not None else load_run_config()
    p = config.get(key, default)
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0.0 <= p <= 1.0:
        print(f"[config] Invalid {key} '{p}' — using default {default}")
        return default
    return float(p)


def apply_configured_valid_probability(config: dict | None = None) -> None:
    """Apply "valid_value_probability" (GUI) and "text_view_valid_value_probability" (Text View edit-text).

    Call before the loop starts: the first value is generated inside run_random().
    """
    from tests.mutation_testing.discovery.values import set_valid_probability
    config = config if config is not None else load_run_config()
    gui = _resolve_probability(config, "valid_value_probability", 0.5)
    text_view = _resolve_probability(config, "text_view_valid_value_probability", 0.8)
    set_valid_probability(gui=gui, text_view=text_view)
    print(f"[config] Applied valid-value probability: gui={gui}, text_view={text_view}")