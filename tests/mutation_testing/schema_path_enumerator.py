"""Given a JSON Schema and the value at a node of a conforming instance, works out which properties
or items can still be added at that node. It replaces arbitrary generated names and types in the
"add child" and "add array item" Text View operations.

- Data tab: root_schema is the loaded schema, the instance is the data.
- Schema tab: root_schema is META_SCHEMA_SIMPLIFIED, the instance is the schema.

This is not a full JSON Schema validator. It resolves $ref, allOf, oneOf/anyOf and if/then/else,
using the `jsonschema` library for $ref resolution and condition checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

MAX_REF_DEPTH = 40  # guards against the meta-schema's self-reference via $ref


_UNSET = object()  # sentinel: const:null is a valid value, distinct from no const

@dataclass
class EffectiveSchema:
    types: set[str] = field(default_factory=set)
    properties: dict[str, Any] = field(default_factory=dict)
    required: set[str] = field(default_factory=set)
    additional_properties: Any = True
    items: Any = None
    pattern: str | None = None
    enum: list | None = None
    const: Any = _UNSET
    min_length: int | None = None
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    exclusive_maximum: float | None = None

def _is_ref(node: Any) -> bool:
    return isinstance(node, dict) and "$ref" in node


def _resolve_local_ref(root_schema: dict, ref: str) -> dict:
    """Resolve a local '#/$defs/...' or '#/definitions/...' pointer against root_schema."""
    if not ref.startswith("#/"):
        raise ValueError(f"only local refs are supported by this resolver, got: {ref}")
    node: Any = root_schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


_SYNTHETIC_ROOT_ID = "urn:schema-path-enumerator:root"


def _build_root_validator(root_schema: dict) -> Draft202012Validator:
    """A validator anchored at root_schema, with a resolver whose base URI matches how root_schema is
    registered, so relative $refs in extracted fragments resolve (see _instance_matches). Schemas
    without a top-level $id get a synthetic one. It never appears in results.
    """
    schema_for_registration = root_schema if "$id" in root_schema else {**root_schema, "$id": _SYNTHETIC_ROOT_ID}
    resource = Resource.from_contents(schema_for_registration, default_specification=DRAFT202012)
    registry = resource @ Registry()
    return Draft202012Validator(schema_for_registration, registry=registry)


def _instance_matches(root_validator: Draft202012Validator, condition_schema: Any, instance: Any) -> bool:
    """Whether `instance` validates against `condition_schema`. Used to pick if/then/else branches.

    The condition is usually a fragment of the root document and may contain relative $refs. Validated
    standalone, those refs fail to resolve, so it is swapped into the anchored validator with evolve(),
    which keeps the original base URI.
    """
    if condition_schema is True:
        return True
    if condition_schema is False:
        return False
    validator = root_validator.evolve(schema=condition_schema)
    try:
        return validator.is_valid(instance)
    except Exception:
        # an unresolvable condition counts as non-matching instead of raising mid-run
        return False


def _merge_into(target: EffectiveSchema, src: EffectiveSchema) -> None:
    target.types |= src.types
    target.properties.update(src.properties)  # later/outer branches win on name conflicts
    target.required |= src.required
    if src.additional_properties is False:
        target.additional_properties = False
    elif src.additional_properties is not True and target.additional_properties is True:
        target.additional_properties = src.additional_properties
    if src.items is not None and target.items is None:
        target.items = src.items
    # pattern/enum/const: later/outer branch wins, same approximation as properties/required. Two patterns stacked across allOf branches are not both enforced.
    if src.pattern is not None:
        target.pattern = src.pattern
    if src.enum is not None:
        target.enum = src.enum
    if src.const is not _UNSET:
        target.const = src.const

    # length and range bounds combine exactly with min/max
    target.min_length = _tighter(target.min_length, src.min_length, max)
    target.max_length = _tighter(target.max_length, src.max_length, min)
    target.minimum = _tighter(target.minimum, src.minimum, max)
    target.maximum = _tighter(target.maximum, src.maximum, min)
    target.exclusive_minimum = _tighter(target.exclusive_minimum, src.exclusive_minimum, max)
    target.exclusive_maximum = _tighter(target.exclusive_maximum, src.exclusive_maximum, min)


def _tighter(a, b, fn):
    if a is None: return b
    if b is None: return a
    return fn(a, b)


def resolve_effective_schema(
    root_schema: dict,
    subschema: Any,
    instance: Any,
    _depth: int = 0,
    _root_validator: Draft202012Validator | None = None,
) -> EffectiveSchema:
    """Merge $ref, allOf and if/then/else into one EffectiveSchema describing what is valid at a node
    whose current value is `instance`.

    additionalProperties is approximated: if any applicable branch says false, the node is treated as
    closed. That is enough to decide whether to offer the generic out-of-schema add action.
    """
    if _root_validator is None:
        _root_validator = _build_root_validator(root_schema)

    eff = EffectiveSchema()
    if _depth > MAX_REF_DEPTH:
        return eff
    if subschema is True or subschema is None:
        return eff  # unconstrained
    if subschema is False:
        return eff  # never valid; nothing addable here

    if _is_ref(subschema):
        target = _resolve_local_ref(root_schema, subschema["$ref"])
        _merge_into(eff, resolve_effective_schema(root_schema, target, instance, _depth + 1, _root_validator))
        # 2020-12 allows keywords next to $ref, so fall through

    if "allOf" in subschema:
        for branch in subschema["allOf"]:
            _merge_into(eff, resolve_effective_schema(root_schema, branch, instance, _depth + 1, _root_validator))

    if "if" in subschema:
        matched = _instance_matches(_root_validator, subschema["if"], instance)
        branch = subschema.get("then") if matched else subschema.get("else")
        if branch is not None:
            _merge_into(eff, resolve_effective_schema(root_schema, branch, instance, _depth + 1, _root_validator))

    # oneOf/anyOf: only the branches the instance matches apply, evaluated like an "if" condition
    for composition_key in ("oneOf", "anyOf"):
        for branch in subschema.get(composition_key, []):
            if _instance_matches(_root_validator, branch, instance):
                _merge_into(eff, resolve_effective_schema(root_schema, branch, instance, _depth + 1, _root_validator))

    # keywords declared directly on this node
    if "type" in subschema:
        t = subschema["type"]
        eff.types |= {t} if isinstance(t, str) else set(t)
    if "properties" in subschema:
        eff.properties.update(subschema["properties"])
    if "required" in subschema:
        eff.required |= set(subschema["required"])
    if "additionalProperties" in subschema:
        eff.additional_properties = subschema["additionalProperties"]
    if "items" in subschema:
        eff.items = subschema["items"]
    if "pattern" in subschema:
        eff.pattern = subschema["pattern"]
    if "enum" in subschema:
        eff.enum = subschema["enum"]
    if "const" in subschema:
        eff.const = subschema["const"]
    if "minLength" in subschema:
        eff.min_length = subschema["minLength"]
    if "maxLength" in subschema:
        eff.max_length = subschema["maxLength"]
    if "minimum" in subschema:
        eff.minimum = subschema["minimum"]
    if "maximum" in subschema:
        eff.maximum = subschema["maximum"]
    if "exclusiveMinimum" in subschema:
        eff.exclusive_minimum = subschema["exclusiveMinimum"]
    if "exclusiveMaximum" in subschema:
        eff.exclusive_maximum = subschema["exclusiveMaximum"]

    return eff


def enumerate_addable_properties(
    root_schema: dict, subschema: Any, current_object: dict
) -> list[tuple[str, Any]]:
    """(name, unresolved child subschema) pairs for schema-known properties that are not yet keys of
    current_object.
    """
    eff = resolve_effective_schema(root_schema, subschema, current_object)
    return [(name, child) for name, child in eff.properties.items() if name not in current_object]


def additional_properties_allowed(root_schema: dict, subschema: Any, current_object: dict) -> bool:
    """Whether to offer the generic out-of-schema add-property action here."""
    eff = resolve_effective_schema(root_schema, subschema, current_object)
    return eff.additional_properties is not False


def resolve_items_schema(root_schema: dict, subschema: Any, current_array: list) -> Any:
    """The unresolved subschema for array items at this node, or None."""
    eff = resolve_effective_schema(root_schema, subschema, current_array)
    return eff.items


def infer_base_type(root_schema: dict, subschema: Any, sample_instance: Any = None) -> str | None:
    """Best-effort type for a child schema, used to pick the kind of value to generate. One of
    'object', 'array', 'string', 'integer', 'number', 'boolean', 'null', or None if unconstrained.
    """
    eff = resolve_effective_schema(root_schema, subschema, sample_instance if sample_instance is not None else {})
    for preferred in ("object", "array", "string", "integer", "number", "boolean", "null"):
        if preferred in eff.types:
            return preferred
    if eff.properties:
        return "object"
    if eff.items is not None:
        return "array"
    return None


def resolve_subschema_at_path(root_schema: dict, path: list, root_instance: Any) -> Any:
    """Walk `path` (object keys as str, array indices as int, as in dom_discoverer's path_to_testid_string)
    through root_schema, following 'properties', 'additionalProperties' and 'items' and resolving
    $ref, allOf, if/then/else and oneOf at each step. Returns the (possibly unresolved) subschema that
    governs the node at `path` in `root_instance`.

    Returns None if the schema does not cover `path` (a key not in 'properties' with
    'additionalProperties: false', or an index with no 'items'). Callers treat that as nothing to
    enumerate, not as an error.
    """
    current_schema: Any = root_schema
    current_instance: Any = root_instance
    _root_validator = _build_root_validator(root_schema)
    for segment in path:
        eff = resolve_effective_schema(root_schema, current_schema, current_instance, _root_validator=_root_validator)
        if isinstance(segment, str):
            if segment in eff.properties:
                current_schema = eff.properties[segment]
            elif isinstance(eff.additional_properties, dict):
                current_schema = eff.additional_properties
            elif eff.additional_properties is True:
                current_schema = True
            else:
                return None
        else:  # array index
            if eff.items is None:
                return None
            current_schema = eff.items
        try:
            current_instance = current_instance[segment]
        except (KeyError, IndexError, TypeError):
            current_instance = {}  # only affects if/then resolution deeper down
    return current_schema