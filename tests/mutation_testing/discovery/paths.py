"""Conversion between data paths, test-id path strings and readable labels, plus helpers for locating values in Text View text."""
from __future__ import annotations

import re
from typing import Any


def path_to_testid_string(path: list) -> str:
    """Path -> testid path string, matching MetaConfigurator's pathToString: numbers become [N], strings become .key.

    Needed for property-data-<path> and property-metadata-<path> testids ('required[0]', not 'required.0').
    """
    result = ""
    for p in path:
        if isinstance(p, int):
            result += f"[{p}]"
        else:
            result += f".{p}"
    return result[1:] if result.startswith(".") else result


def _collect_leaf_paths(value: Any, path: list | None = None) -> list[tuple[list, Any]]:
    """(path, value) of every leaf (non-dict, non-list) node, to pick a random value to edit."""
    path = path or []
    leaves = []
    if isinstance(value, dict):
        for k, v in value.items():
            leaves.extend(_collect_leaf_paths(v, path + [k]))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            leaves.extend(_collect_leaf_paths(v, path + [i]))
    else:
        leaves.append((path, value))
    return leaves


def testid_path_string_to_list(path_str: str) -> list:
    """Inverse of path_to_testid_string(): 'required[0]' -> ['required', 0], '$defs.Point.properties' -> ['$defs', 'Point', 'properties']."""
    tokens = re.findall(r'\.([^.\[]+)|\[(\d+)\]', "." + path_str)
    path = []
    for key_token, index_token in tokens:
        path.append(int(index_token) if index_token else key_token)
    return path


def _humanize_segment(segment: Any) -> str:
    """One path segment in plain English: 'DrivingSpeed' -> 'Driving Speed', 'car1' -> 'Car 1', index N -> 'item N+1'."""
    if isinstance(segment, int):
        return f"item {segment + 1}"
    # Insert a space before each capital that follows a lowercase letter
    # or digit (camelCase/PascalCase -> words), and between a letter and
    # an immediately-following digit run. Hyphens become spaces too.
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', str(segment))
    s = re.sub(r'(?<=[a-zA-Z])(?=[0-9])', ' ', s)
    s = s.replace('-', ' ')
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s


def humanize_value(value: Any) -> str:
    """Plain-language form of a value ("a group with 2 fields inside", "on", "empty") instead of its Python repr."""
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str):
        return f'"{value}"' if value else "an empty piece of text"
    if isinstance(value, dict):
        n = len(value)
        return "an empty group" if n == 0 else f"a group with {n} field{'s' if n != 1 else ''} inside"
    if isinstance(value, list):
        n = len(value)
        return "an empty list" if n == 0 else f"a list with {n} item{'' if n == 1 else 's'} inside"
    return str(value)


def humanize_path(path_str: str) -> str:
    """Readable breadcrumb for a testid path: 'Vehicles.car1.DriverBehavior' -> 'Vehicles → car 1 → Driver Behavior'; '' -> 'the top level'."""
    if not path_str:
        return "the top level"
    path = testid_path_string_to_list(path_str)
    return " → ".join(_humanize_segment(p) for p in path)


def _value_at_path(data: Any, path: list) -> Any:
    """Value at a path (list of str/int), built like _collect_leaf_paths."""
    current = data
    for p in path:
        current = current[p]
    return current


def _find_matching_close_bracket(text: str, open_index: int, open_char: str, close_char: str) -> int | None:
    """Index of the bracket closing the one at `open_index`, ignoring brackets inside strings; None if unmatched.
    Shared by the structural Text View operations.
    """
    if text[open_index] != open_char:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(open_index, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return i
    return None


def _find_array_item_starts(text: str, bracket_start: int, bracket_end: int) -> list[int]:
    """Start index of each top-level item between the brackets at bracket_start and bracket_end.

    A top-level comma separates items; commas inside nested {} / [] or strings do not.
    """
    starts = []
    i = bracket_start + 1
    depth = 0
    in_string = False
    escape = False
    expect_item_start = True  # True right after '[' or after a top-level comma
    while i < bracket_end:
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if expect_item_start and not c.isspace():
            starts.append(i)
            expect_item_start = False
        if c == '"':
            in_string = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        elif c == "," and depth == 0:
            expect_item_start = True
        i += 1
    return starts


def _collect_container_paths(value: Any, path: list | None = None, want_type: type = dict) -> list[tuple[list, Any]]:
    """Like _collect_leaf_paths, but for dict (or list) nodes, including the root (path=[])."""
    path = path or []
    results = []
    if isinstance(value, want_type):
        results.append((path, value))
    if isinstance(value, dict):
        for k, v in value.items():
            results.extend(_collect_container_paths(v, path + [k], want_type))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            results.extend(_collect_container_paths(v, path + [i], want_type))
    return results
