"""Template gui_validation_agreement: the GUI error icon of a property is shown exactly when its value violates its schema."""
from __future__ import annotations

from jsonschema import Draft202012Validator, FormatChecker, RefResolver
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from tests.invariants.schema_validity import unresolvable_local_refs  # pure function; the module's browser parts load lazily
from tests.mutation_testing.inference.common import Observation, _MISSING, path_tokens, value_at, walk_schema
from tests.mutation_testing.inference.facets import _safe_dict


def _value_violates_schema(value, prop_schema, root_schema=None, resolver=None) -> bool:
    """Same check as the function of the same name in tests.invariants.invariants_core (duplicated so this offline engine does
    not import that browser-facing module; keep the two in sync), except that a prepared resolver can be passed in.
    """
    if not prop_schema:
        return False
    try:
        if root_schema:
            resolver = resolver or RefResolver.from_schema(root_schema)
            return not Draft202012Validator(prop_schema, resolver=resolver, format_checker=FormatChecker()).is_valid(value)
        return not Draft202012Validator(prop_schema, format_checker=FormatChecker()).is_valid(value)
    except Exception:
        return False


def _app_can_compile(schema: dict) -> bool:
    """False if MetaConfigurator's Ajv would refuse the schema (addSchema validates it against the JSON Schema meta-schema, e.g.
    duplicate items in 'required') or cannot resolve a local $ref (e.g. a renamed definition). The app then shows no validation
    errors at all, so icon and value cannot be compared.
    """
    try:
        validator_for(schema, default=Draft202012Validator).check_schema(schema)
        return not unresolvable_local_refs(schema)
    except SchemaError:
        return False
    except Exception:
        return True  # unknown: keep the observation rather than silently dropping it


def _readings(snapshot: dict) -> dict[str, tuple[bool, bool]]:
    """{row key: (icon_shown, value violates its schema)} for every row that has a GUI reading, a data value and exactly one declared schema.
    Rows whose schema runs through a $ref that cannot be followed are left out: the validator would have to fetch it.
    """
    gui = _safe_dict(snapshot.get("de_gui"))
    schema = snapshot.get("de_schema")
    data = snapshot.get("de_data")
    if not isinstance(schema, dict) or not gui or not _app_can_compile(schema):
        return {}
    resolver = None
    out: dict[str, tuple[bool, bool]] = {}
    for key, facts in gui.items():
        tokens = path_tokens(key)
        if not tokens or not isinstance(facts, dict) or "icon_shown" not in facts:
            continue
        value = value_at(data, tokens)
        if value is _MISSING:
            continue  # no value to violate the schema
        declared, _, opaque = walk_schema(schema, tokens)
        if opaque or len(declared) != 1:
            continue
        if resolver is None:
            resolver = RefResolver.from_schema(schema)
        out[key] = (bool(facts["icon_shown"]), _value_violates_schema(value, declared[0], schema, resolver))
    return out


def rule_gui_validation_agreement(before: dict, after: dict, transition: bool = False) -> list[Observation]:
    """Compare, for every property row at any depth, whether the GUI shows its validation-error icon with whether the value
    violates its declared schema. The icon comes from de_gui[key]['icon_shown'].
    transition=False checks the state after the step; transition=True also requires icon and value to have agreed
    before it, which isolates the action that introduced the mismatch.
    """
    now = _readings(after)
    prev = _readings(before) if transition else {}
    obs: list[Observation] = []
    for key, (icon_shown, invalid) in now.items():
        if transition and (key not in prev or prev[key][0] != prev[key][1]):
            continue
        agree = icon_shown == invalid
        obs.append((agree, {} if agree else {"key": key, "icon_shown": icon_shown, "actually_invalid": invalid}))
    return obs


NAME = "gui_validation_agreement"
RULE = rule_gui_validation_agreement