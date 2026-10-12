"""Schema validity against MetaConfigurator's own (simplified) meta-schema, with one cached validator.

Used by INV-D6 (skipped while the edited schema is invalid), INV-S4 and MutationLoop (valid -> invalid
transition detection). The meta-schema is the one the app uses for the Schema Editor; set the environment
variable METACONFIG_META_SCHEMA_PATH to a JSON file to override it (reproducible runs, offline tests).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

META_SCHEMA_ENV = "METACONFIG_META_SCHEMA_PATH"
_MAX_REPORTED = 20

_validator: Draft202012Validator | None = None


def _load_meta_schema(page) -> dict:
    path = os.environ.get(META_SCHEMA_ENV)
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    # Imported lazily: discovery.schema_resolution must not be a load-time dependency of the invariants package.
    from tests.mutation_testing.discovery.schema_resolution import _resolve_governing_schema
    return _resolve_governing_schema(page, "schemaEditor")


def _get_validator(page) -> Draft202012Validator:
    global _validator
    if _validator is None:
        meta = _load_meta_schema(page)
        _validator = Draft202012Validator(meta)
    return _validator


def _app_compile_problems(node, path: str = "") -> list[dict]:
    """Schemas the meta-schema accepts but the app's Ajv refuses to compile (so it enforces nothing)."""
    found: list[dict] = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if k == "enum" and isinstance(v, list) and len(v) == 0:
                found.append({"path": here, "deep_path": here,
                              "message": "enum must have a non-empty array (Ajv refuses to compile it)"})
            found.extend(_app_compile_problems(v, here))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(_app_compile_problems(v, f"{path}.{i}" if path else str(i)))
    return found


# Keys whose values are instance data, not subschemas: a "$ref" inside them is not a reference.
_NON_SCHEMA_KEYS = frozenset({"enum", "const", "default", "examples", "example"})


def _local_pointer_resolves(root, ref: str) -> bool:
    """True if the local reference `ref` ("#", "#/definitions/X", ...) points at something inside `root`.
    Anchors ("#foo") cannot be followed here and count as resolvable (unknown is not a finding).
    """
    if ref == "#":
        return True
    if not ref.startswith("#/"):
        return True
    node = root
    for raw in ref[2:].split("/"):
        token = unquote(raw).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return False
    return True


def unresolvable_local_refs(root) -> list[dict]:
    """Local "$ref"s ("#/...") in `root` that point at nothing, e.g. after a definition was renamed.

    The meta-schema accepts such a schema, but the app's validator throws and enforces nothing, so no validation
    icons appear. Items: {"path", "deep_path", "message"}.
    """
    return _walk_refs(root, root, "")


def _walk_refs(root, node, path: str) -> list[dict]:
    # `node` may be any JSON value, null included (a definition can be edited to null), so it has no default.
    found: list[dict] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not _local_pointer_resolves(root, ref):
            where = path or "(root)"
            found.append({"path": where, "deep_path": where,
                          "message": f"$ref '{ref}' does not resolve (the app cannot compile the schema, so it enforces nothing)"})
        for k, v in node.items():
            if k in _NON_SCHEMA_KEYS:
                continue
            found.extend(_walk_refs(root, v, f"{path}.{k}" if path else k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(_walk_refs(root, v, f"{path}.{i}" if path else str(i)))
    return found


def schema_meta_violations(page, schema, include_app_compile_problems: bool = True) -> list[dict] | None:
    """Violations of `schema` against the meta-schema.

    []   -> schema is valid
    [..] -> invalid; each item {"path", "deep_path", "message"}. `path` is where the failing
            subschema sits; `deep_path`/`message` come from the deepest matching branch, because
            the meta-schema's oneOf otherwise hides the actual cause.
    None -> validity could not be determined (callers must NOT treat this as valid or invalid).
    include_app_compile_problems=False -> meta-schema only (what the Schema GUI icons reflect).
    """
    try:
        validator = _get_validator(page)
        out: list[dict] = []
        for err in validator.iter_errors(schema):
            deepest = best_match([err]) or err
            out.append({
                "path": ".".join(str(p) for p in err.absolute_path) or "(root)",
                "deep_path": ".".join(str(p) for p in deepest.absolute_path) or "(root)",
                "message": deepest.message[:200],
            })
            if len(out) >= _MAX_REPORTED:
                break
        if include_app_compile_problems:
            out.extend(_app_compile_problems(schema))
            out.extend(unresolvable_local_refs(schema))
        return out
    except Exception as e:  # unresolvable $ref, meta-schema unavailable, ...
        print(f"   [schema_validity] could not evaluate schema validity: {type(e).__name__}: {e}")
        return None