"""Reads config/run_config.json and resolves it into the settings of a mutation-loop run:
action-weights profile, schema/data pair, step count, screenshots and run mode.

Each resolver falls back to a default and prints why on a missing file, malformed JSON, missing key
or unrecognised value, so a config typo does not crash a run.

    from tests.mutation_testing.profile_config import (
        load_run_config, apply_configured_profile, resolve_schema_choice,
        resolve_run_mode, resolve_num_steps, resolve_screenshots,
    )

    run_config = load_run_config()
    apply_configured_profile(run_config)
    schema_option_name, data_file = resolve_schema_choice(SCHEMA_DATA_PAIRS, run_config)
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from tests.mutation_testing.mutation_loop import set_action_weights_profile

_CONFIG_PATH = Path(__file__).parent / "config" / "run_config.json"

# Source of truth for accepted profiles. Keep in sync with a new action_weights_*.json file.
_KNOWN_PROFILES = {"default", "textview_focus", "schema_focus", "data_focus", "random"}
_KNOWN_RUN_MODES = {"background", "watch_failures", "interactive"}


def load_run_config() -> dict:
    """Read run_config.json into a dict. A missing file or malformed JSON returns {}, which every
    resolver treats as "nothing configured".
    """
    if not _CONFIG_PATH.exists():
        print(f"[config] {_CONFIG_PATH.name} not found in the config folder - using all defaults")
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[config] Could not parse {_CONFIG_PATH.name} ({e}) - using all defaults")
        return {}


def apply_configured_profile(config: dict | None = None) -> str:
    """Apply the "action_weights_profile" setting via set_action_weights_profile() and return the profile
    name that was applied.

    config: an already-loaded run_config dict; if omitted, load_run_config() is called.
    """
    config = config if config is not None else load_run_config()
    profile = config.get("action_weights_profile") or "default"

    if profile not in _KNOWN_PROFILES:
        print(f"[config] Unrecognized action_weights_profile '{profile}' "
              f"(known: {sorted(_KNOWN_PROFILES)}) - using default profile instead")
        set_action_weights_profile(None)
        return "default"

    set_action_weights_profile(None if profile == "default" else profile)
    print(f"[config] Applied action-weights profile: '{profile}'")
    return profile


def resolve_schema_choice(
    schema_data_pairs: list[tuple[str, Path]], config: dict | None = None
) -> tuple[str, Path]:
    """Resolve "schema" against schema_data_pairs (passed in because mutation_loop imports this module).

    - "random", missing or empty: a random pair
    - an exact schema option name: that pair
    - anything else: a random pair, with a warning

    Returns (schema_option_name, data_file_path).
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
          f"(known: {sorted(valid_names)}) - falling back to random")
    picked = random.choice(schema_data_pairs)
    print(f"[config]   -> picked '{picked[0]}'")
    return picked


def resolve_run_mode(config: dict | None = None) -> str | None:
    """Return "background", "watch_failures" or "interactive" if "run_mode" is set to one of them, else
    None. None tells the caller to ask at the terminal.
    """
    config = config if config is not None else load_run_config()
    mode = config.get("run_mode")
    if mode is None:
        return None
    if mode not in _KNOWN_RUN_MODES:
        print(f"[config] Unrecognized run_mode '{mode}' (known: {sorted(_KNOWN_RUN_MODES)}) "
              f"- falling back to the interactive prompt")
        return None
    return mode


def resolve_num_steps(config: dict | None = None, default: int = 100) -> int:
    """Read "num_steps"; `default` if missing or invalid."""
    config = config if config is not None else load_run_config()
    n = config.get("num_steps", default)
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        print(f"[config] Invalid num_steps '{n}' - using default {default}")
        return default
    return n


def resolve_screenshots(config: dict | None = None, default: bool = True) -> bool:
    """Read "screenshots"; `default` if missing or not a boolean."""
    config = config if config is not None else load_run_config()
    val = config.get("screenshots", default)
    if not isinstance(val, bool):
        print(f"[config] Invalid screenshots value '{val}' - using default {default}")
        return default
    return val