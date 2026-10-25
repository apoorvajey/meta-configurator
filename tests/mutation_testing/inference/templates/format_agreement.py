"""Template format_agreement: the Text View content is in the format shown by the selector."""
from __future__ import annotations

import json
from tests.mutation_testing.inference.common import Observation
from tests.mutation_testing.inference.facets import TEXT_VIEWS


def detect_text_format(raw: str | None) -> str | None:
    """Format the text is actually in: 'json', 'yaml' or None if it cannot be judged.
    Text that looks like JSON but fails json.loads is a half-edited document, not YAML.
    """
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
        if not value:  # {} and [] are identical in JSON and YAML, so they can't be judged
            return None
        return "json" if isinstance(value, (dict, list)) else None
    except ValueError:
        pass
    if raw.lstrip().startswith(("{", "[")):
        return None
    try:
        import yaml
        value = yaml.safe_load(raw)
    except ImportError:
        raise ImportError("PyYAML is required for format_agreement: pip install pyyaml")
    except Exception:
        return None
    return "yaml" if isinstance(value, (dict, list)) else None


def _label_and_content(snapshot: dict, view: str) -> tuple[str | None, str | None]:
    text = snapshot.get(TEXT_VIEWS[view][0]) or {}
    return text.get("format"), detect_text_format(text.get("raw"))


def rule_format_agreement(before: dict, after: dict, view: str,
                          transition: bool = False) -> list[Observation]:
    """After the step, the detected format of the Text View content equals the format selector's label.

    With transition=True the rule only applies if label and content also agreed before the step,
    so only the action that introduced a mismatch is blamed.
    """
    label, content = _label_and_content(after, view)
    if label is None or content is None:
        return []
    if transition:
        b_label, b_content = _label_and_content(before, view)
        if b_label is None or b_content is None or b_label != b_content:
            return []
    if label == content:
        return [(True, {})]
    return [(False, {"label": label, "content": content})]


NAME = "format_agreement"
RULE = rule_format_agreement
