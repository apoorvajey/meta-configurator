"""Template text_store_agreement: the parsed Text View equals the editor's store."""
from __future__ import annotations

from deepdiff import DeepDiff

import re
from typing import Any
from tests.mutation_testing.inference.common import Observation
from tests.mutation_testing.inference.facets import TEXT_VIEWS


_ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$"
)


def _normalise_timestamps(value: Any) -> Any:
    """Rewrite ISO-8601 timestamps to one UTC form. Needed for YAML only: capture parses it into datetimes, which serialise differently from the text."""
    from datetime import datetime, timezone
    if isinstance(value, dict):
        return {k: _normalise_timestamps(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise_timestamps(v) for v in value]
    if isinstance(value, str) and _ISO_DATETIME.match(value):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else dt.isoformat()
    return value


def rule_text_store_agreement(before: dict, after: dict, view: str) -> list[Observation]:
    text_key, store_key = TEXT_VIEWS[view]
    text = after.get(text_key) or {}
    parsed = text.get("parsed")
    if parsed is None:
        return []
    store = after.get(store_key)
    same = parsed == store
    if not same and text.get("format") == "yaml":
        same = _normalise_timestamps(parsed) == _normalise_timestamps(store)
    if same:
        return [(True, {})]
    try:
        diff = DeepDiff(store, parsed, ignore_order=True)
        first = next((p for v in diff.values() if isinstance(v, (dict, set, list)) for p in v), "")
    except Exception:
        first = "?"
    return [(False, {"format": text.get("format"), "first_diff": str(first)[:120]})]


NAME = "text_store_agreement"
RULE = rule_text_store_agreement
