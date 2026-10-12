"""Shared pieces of the templates: observations and access to schema keyword paths."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote


SCHEMA_KEYWORDS = {
    "$schema", "$defs", "$id", "$ref", "title", "description",
    "type", "properties", "required", "additionalProperties",
    "definitions", "items", "allOf", "anyOf", "oneOf", "not",
    "if", "then", "else", "format", "enum", "const", "minimum",
    "maximum", "minLength", "maxLength", "pattern", "propertyNames",
    "multipleOf", "exclusiveMinimum", "exclusiveMaximum",
    "dependentSchemas", "dependentRequired", "patternProperties", "prefixItems", "contains",
    "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties",
    "unevaluatedProperties", "unevaluatedItems", "deprecated", "examples", "default",
    "readOnly", "writeOnly",
}


# Keywords whose children are names, not keywords; not descended into, so a property called 'type' is not taken for a keyword.
NAME_MAP_KEYWORDS = {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}


_MISSING = object()


def _is_schema_key(key: str) -> bool:
    return key in SCHEMA_KEYWORDS or key.startswith("$")


def schema_path_value(snapshot: dict, keys: list[str]) -> Any:
    """de_schema[k1][k2]... of a snapshot, or _MISSING if a step is absent."""
    node = snapshot.get("de_schema")
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return _MISSING
        node = node[k]
    return node


def _enumerate_schema_keyword_paths(schema: Any) -> set[tuple[str, ...]]:
    """Every keyword-only dict path of depth 1 or 2 in a schema."""
    paths: set[tuple[str, ...]] = set()
    if not isinstance(schema, dict):
        return paths
    for k1, v1 in schema.items():
        if not _is_schema_key(k1):
            continue
        paths.add((k1,))
        if isinstance(v1, dict) and k1 not in NAME_MAP_KEYWORDS:
            for k2 in v1:
                if _is_schema_key(k2):
                    paths.add((k1, k2))
    return paths


Observation = tuple[bool, dict]


def _short(value: Any, n: int = 100) -> str:
    return "<missing>" if value is _MISSING else str(value)[:n]


def _fmt_value(value: Any, n: int = 300) -> str:
    """Compact JSON of a value for a diff row, cut at n characters."""
    if value is _MISSING:
        return "<missing>"
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= n else text[:n] + "…"


def leaf_changes(a: Any, b: Any, path: str = "", limit: int = 25) -> tuple[list[dict], int]:
    """Exact differences between two values as ({"path", "before", "after"}, ...) rows plus the total count.

    Recurses into dicts and into lists (index by index; an extra or missing item is one row). Order matters, as in
    ==. A type change (1 vs True) is a difference. At most `limit` rows are returned; the count is always complete.
    """
    rows: list[dict] = []
    total = 0

    def walk(x, y, p):
        nonlocal total
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y), key=str):
                key = str(k) or '""'   # an empty key is shown as "", not mistaken for the root
                walk(x.get(k, _MISSING), y.get(k, _MISSING), f"{p}.{key}" if p else key)
        elif isinstance(x, list) and isinstance(y, list):
            for i in range(max(len(x), len(y))):
                walk(x[i] if i < len(x) else _MISSING, y[i] if i < len(y) else _MISSING, f"{p}[{i}]")
        elif x is _MISSING or y is _MISSING or x != y or type(x) is not type(y):
            total += 1
            if len(rows) < limit:
                rows.append({"path": p or "(root)", "before": _fmt_value(x), "after": _fmt_value(y)})

    walk(a, b, path)
    return rows, total


def _keys(obj: Any) -> set:
    return set(obj.keys()) if isinstance(obj, dict) else set()



# data and schema paths (the keys of de_gui rows)

_PATH_TOKEN = re.compile(r"[^.\[\]]+")


def path_tokens(path: str) -> list[str]:
    """GUI row key to path steps: 'address.zip' gives ['address', 'zip'], 'nickNames[0]' gives ['nickNames', '0']."""
    return _PATH_TOKEN.findall(path)


def value_at(data: Any, tokens: list[str]) -> Any:
    """The value of data at the path, or _MISSING."""
    node = data
    for t in tokens:
        if isinstance(node, dict) and t in node:
            node = node[t]
        elif isinstance(node, list) and t.isdigit() and int(t) < len(node):
            node = node[int(t)]
        else:
            return _MISSING
    return node


def _follow_ref(node: Any, root: Any) -> Any:
    """node with a local '$ref' replaced by its target (repeatedly); None for a remote or unresolvable ref or a ref cycle."""
    seen: set[str] = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if not ref.startswith("#") or ref in seen:
            return None
        seen.add(ref)
        target = root
        for part in [p for p in ref[1:].split("/") if p]:
            part = unquote(part).replace("~1", "/").replace("~0", "~")
            if isinstance(target, dict) and part in target:
                target = target[part]
            elif isinstance(target, list) and part.isdigit() and int(part) < len(target):
                target = target[int(part)]
            else:
                return None
        node = target
    return node if isinstance(node, dict) else None


def _expand(node: Any, root: Any, conditional: bool = False, seen: frozenset = frozenset()) -> list[tuple[dict, bool]]:
    """The schema plus everything that also applies to it: allOf members (unconditional), anyOf/oneOf/then/else (conditional)."""
    node = _follow_ref(node, root)
    if node is None or id(node) in seen:
        return []
    seen = seen | {id(node)}
    out = [(node, conditional)]
    for keyword, cond in (("allOf", False), ("anyOf", True), ("oneOf", True)):
        members = node.get(keyword)
        for sub in members if isinstance(members, list) else []:
            out += _expand(sub, root, conditional or cond, seen)
    for keyword in ("then", "else"):
        out += _expand(node.get(keyword), root, True, seen)
    dep = node.get("dependentSchemas")
    for sub in dep.values() if isinstance(dep, dict) else []:
        out += _expand(sub, root, True, seen)
    return out


def _children(parents: list[tuple[dict, bool]], token: str) -> list[tuple[Any, bool]]:
    """Schemas declared for one path step below each parent: properties, patternProperties, additionalProperties, items."""
    out: list[tuple[Any, bool]] = []
    for node, cond in parents:
        props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        if token in props:
            out.append(({} if props[token] is True else props[token], cond))
        else:
            for pattern, sub in (node.get("patternProperties") or {}).items() if isinstance(node.get("patternProperties"), dict) else []:
                if re.search(pattern, token):
                    out.append((sub, cond))
            if isinstance(node.get("additionalProperties"), dict):
                out.append((node["additionalProperties"], cond))
        if token.isdigit():
            prefix = node.get("prefixItems")
            if isinstance(prefix, list) and int(token) < len(prefix):
                out.append((prefix[int(token)], cond))
            items = node.get("items")
            if isinstance(items, dict):
                out.append((items, cond))
            elif isinstance(items, list) and int(token) < len(items):
                out.append((items[int(token)], cond))
    return out


def walk_schema(schema: Any, tokens: list[str]) -> tuple[list[dict], list[tuple[dict, bool]], bool]:
    """Follow the path in the schema through $ref, allOf, anyOf, oneOf, then and else.
    Returns (declared, expanded, opaque):
      declared  the schemas declared for the path, as written (they may still contain a $ref)
      expanded  each of them expanded into the schemas that also apply, as (schema, conditional) pairs
      opaque    True if the path runs through a $ref that cannot be followed (remote or circular), so what lies
                below it is unknown
    """
    expanded = _expand(schema, schema)
    declared: list[tuple[Any, bool]] = [(schema, False)]
    opaque = not expanded
    for t in tokens:
        declared = [(d, c) for d, c in _children(expanded, t) if isinstance(d, dict)]
        expanded = []
        for node, cond in declared:
            found = _expand(node, schema, cond)
            opaque = opaque or not found
            expanded += found
    return [d for d, _ in declared], expanded, opaque


def schema_has_path(schema: Any, tokens: list[str]) -> bool:
    """True if the schema declares the path, or if the path runs into a $ref that cannot be followed."""
    declared, _, opaque = walk_schema(schema, tokens)
    return bool(declared) or opaque


def required_names(expanded: list[tuple[dict, bool]]) -> tuple[set, set]:
    """(always required, required only under a condition) property names of an expanded schema."""
    always: set = set()
    conditional: set = set()
    for node, cond in expanded:
        names = {n for n in node.get("required", []) if isinstance(n, str)} if isinstance(node.get("required"), list) else set()
        (conditional if cond else always).update(names)
        deps = node.get("dependentRequired")
        for listed in deps.values() if isinstance(deps, dict) else []:
            if isinstance(listed, list):
                conditional.update(n for n in listed if isinstance(n, str))
    return always, conditional