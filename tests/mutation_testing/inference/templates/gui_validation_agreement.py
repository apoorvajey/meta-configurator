"""Template gui_validation_agreement: the GUI error icon of a root-level property is shown exactly when its value violates its schema."""
from __future__ import annotations

from jsonschema import Draft202012Validator, RefResolver, validate
from tests.mutation_testing.inference.common import Observation
from tests.mutation_testing.inference.facets import _safe_dict


def _value_violates_schema(value, prop_schema, root_schema=None) -> bool:
    """Copy of the function of the same name in tests.invariants.invariants_core. It is duplicated so this offline engine does not
    import that browser-facing module; keep the two in sync.
    """
    if not prop_schema:
        return False
    try:
        if root_schema:
            resolver = RefResolver.from_schema(root_schema)
            validator = Draft202012Validator(prop_schema, resolver=resolver)
            return not validator.is_valid(value)
        validate(instance=value, schema=prop_schema)
        return False
    except Exception:
        return False


def rule_gui_validation_agreement(before: dict, after: dict, transition: bool = False) -> list[Observation]:
    """Compare, for one root-level property, whether the GUI shows its validation-error icon with whether the value violates its declared schema.

    The icon comes from de_gui[key]['icon_shown']; the violation is computed with _value_violates_schema, the check
    INV-GUI-ERR-01 uses, so $ref properties resolve alike. Root-level properties only; nested ones would need
    schema_path_enumerator.resolve_subschema_at_path.

    transition=False checks the state after the step; transition=True also requires icon and value to have agreed
    before it, which isolates the action that introduced the mismatch (as in rule_format_agreement).
    """
    gui = _safe_dict(after.get("de_gui"))
    data = _safe_dict(after.get("de_data"))
    schema = _safe_dict(after.get("de_schema"))
    props = _safe_dict(schema.get("properties"))

    obs: list[Observation] = []
    for key, facts in gui.items():
        if "." in key or "[" in key or not isinstance(facts, dict) or "icon_shown" not in facts:
            continue  # nested path or incomplete row
        if key not in props:
            continue  # not a declared root property - nothing to check against
        icon_shown = bool(facts["icon_shown"])
        actually_invalid = _value_violates_schema(data.get(key), props[key], schema)

        if transition:
            before_gui = _safe_dict(_safe_dict(before.get("de_gui")).get(key))
            before_data = _safe_dict(before.get("de_data"))
            before_schema = _safe_dict(before.get("de_schema"))
            before_props = _safe_dict(before_schema.get("properties"))
            if key not in before_props or "icon_shown" not in before_gui:
                continue
            before_icon = bool(before_gui["icon_shown"])
            before_invalid = _value_violates_schema(before_data.get(key), before_props[key], before_schema)
            if before_icon != before_invalid:
                continue  # already mismatched before this step - not the introducing action

        agree = (icon_shown == actually_invalid)
        obs.append((agree, {} if agree else
                    {"key": key, "icon_shown": icon_shown, "actually_invalid": actually_invalid}))
    return obs


NAME = "gui_validation_agreement"
RULE = rule_gui_validation_agreement
