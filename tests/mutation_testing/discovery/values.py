"""Schema-aware generation of values (valid and deliberately invalid) for the actions."""
from __future__ import annotations

import random
import re
import string
import rstr
from typing import Any
from tests.mutation_testing.discovery.element import ElementDescriptor
from tests.mutation_testing.discovery.schema_resolution import _resolve_prop_schema_dict
from tests.mutation_testing.discovery.paths import testid_path_string_to_list

# Chance that a generated value is valid, set from run_config.json (see profile_config.apply_configured_valid_probability).
# "gui": GUI View fills/selects. "text_view": Text View edit-text.
_VALID_P = {"gui": 0.5, "text_view": 0.8}


def set_valid_probability(gui: float, text_view: float) -> None:
    _VALID_P["gui"], _VALID_P["text_view"] = gui, text_view


def get_valid_probability(kind: str = "gui") -> float:
    return _VALID_P[kind]


def roll_valid(kind: str = "gui") -> bool:
    """True with the configured probability of generating a valid value."""
    return random.random() < _VALID_P[kind]


def _generate_typed_value(allowed_kinds: list[str]) -> tuple[str, Any]:
    """Pick a random kind from allowed_kinds and generate a value of it (shared by add-child-text, change-type-text and array item insertion)."""
    kind = random.choice(allowed_kinds)
    if kind == "object":
        return kind, {}
    elif kind == "array":
        return kind, []
    elif kind == "boolean":
        return kind, random.choice([True, False])
    elif kind in ("number", "integer"):
        # "integer" (from infer_base_type) shares the "number" branch, which generates ints
        return kind, random.randint(1, 1000)
    elif kind == "string":
        return kind, "".join(random.choices(string.ascii_letters, k=8))
    else:  # "null"
        return kind, None


_FAKE_STRINGS = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo",
    "Test_123", "hello world", "foo-bar", "value_42",
    "", "x" * 50,
]


_INVALID_TYPE_STRINGS = [
    "NOT_A_NUMBER", "null", "undefined", "true",
    "<script>alert(1)</script>", "12345678901234567890",
    "  ",
]


# Readable word pool for generated strings; domain-agnostic because the loop runs against unrelated schemas
_WORD_BANK = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Falcon", "Garnet",
    "Harbor", "Indigo", "Juniper", "Kestrel", "Lumen", "Meadow",
    "Nimbus", "Orchid", "Pioneer", "Quartz", "Raven", "Summit",
    "Terra", "Umber", "Vertex", "Willow", "Xenon", "Yonder", "Zephyr",
]


def _meaningful_name(min_len: int, max_len: int) -> str:
    """Readable Word_Word-style name, clamped to [min_len, max_len]."""
    name = random.choice(_WORD_BANK)
    if random.random() < 0.5:
        name += "_" + random.choice(_WORD_BANK)
    if len(name) < min_len:
        name += "_" + str(random.randint(1, 999))
    return name[:max_len] if max_len else name


class ValueGenerator:

    def __init__(self, schema: dict | None = None, instance: Any = None):
        """schema: the governing schema (the user's schema in the Data Editor, the meta-schema in the Schema Editor).
        instance: the current document, needed to resolve if/then/else branches.
        """
        self.governing_schema = schema or {}
        self.instance = instance if instance is not None else {}

    def _prop_schema(self, property_name: str) -> dict:
        if not property_name or not self.governing_schema:
            return {}
        path = testid_path_string_to_list(property_name)
        return _resolve_prop_schema_dict(self.governing_schema, path, self.instance)

    def value_for_type(self, prop_schema: dict, declared_type: str | None, valid: bool) -> Any:
        """Generate a value for an already-resolved prop_schema and type, for callers (Text View edit-text) that resolve those themselves.

        Returns a real Python value suitable for json.dumps(), not the button label _boolean_value returns.
        """
        if declared_type == "string":
            return self._string_value(prop_schema, valid)
        if declared_type in ("number", "integer"):
            return self._number_value({**prop_schema, "type": declared_type}, valid)
        if declared_type == "boolean":
            return self._boolean_value_typed(prop_schema, valid)
        return None

    def for_element(self, el: ElementDescriptor, force_valid: bool | None = None) -> Any:
        # Generate values for both fill and select interactions on string fields
        if el.interaction == "select" and el.testid.startswith("input-select-"):
            prop_name = el.testid[len("input-select-"):]
            prop_schema = self._prop_schema(prop_name)
            if prop_schema.get("type") == "string":
                use_valid = force_valid if force_valid is not None else roll_valid()
                return self._string_value(prop_schema, use_valid)
            # For meta-schema string fields with no schema definition (e.g. $id, description, title)
            if el.tag == "input" and not prop_schema:
                use_valid = force_valid if force_valid is not None else roll_valid()
                return self._string_value({}, use_valid)
        if el.interaction != "fill":
            return None

        use_valid = force_valid if force_valid is not None else roll_valid()
        prop_name   = el.property_name
        prop_schema = self._prop_schema(prop_name) if prop_name else {}

        if el.input_type == "string":
            return self._string_value(prop_schema, use_valid)
        if el.input_type == "number":
            return self._number_value(prop_schema, use_valid)
        if el.input_type == "boolean":
            return self._boolean_value(prop_schema, use_valid)

        return random.choice(_FAKE_STRINGS) if use_valid else random.choice(_INVALID_TYPE_STRINGS)

    def _string_value(self, prop_schema: dict, valid: bool) -> str:
        enum_vals    = prop_schema.get("enum")
        pattern      = prop_schema.get("pattern")
        real_min_len = prop_schema.get("minLength")   # None = schema doesn't actually constrain this
        real_max_len = prop_schema.get("maxLength")
        gen_min_len  = real_min_len if real_min_len is not None else 1
        gen_max_len  = real_max_len if real_max_len is not None else 30

        if valid:
            if enum_vals:
                return random.choice(enum_vals)
            if pattern:
                candidate = self._satisfy_pattern(pattern)
                if candidate is not None:
                    return candidate
                print(f" Could not build a value guaranteed to satisfy pattern {pattern!r} "
                      f"— using a plain name instead; NOT verified schema-valid")
            return _meaningful_name(gen_min_len, gen_max_len)

        # Invalid values: only where there is a constraint to break
        if enum_vals:
            candidates = [v for v in _INVALID_TYPE_STRINGS if v not in enum_vals]
            return random.choice(candidates) if candidates else "NOT_IN_ENUM_" + str(random.randint(1, 999))

        if pattern:
            for candidate in _INVALID_TYPE_STRINGS:
                if not re.search(pattern, candidate):
                    return candidate
            return "###PATTERN_VIOLATION###"

        if real_min_len:
            return "x" * max(real_min_len - 1, 0)
        if real_max_len is not None:
            return "x" * (real_max_len + 1)

        # No enum, pattern or length bound: nothing can make this string invalid
        return _meaningful_name(gen_min_len, gen_max_len)

    def _satisfy_pattern(self, pattern: str) -> str | None:
        """A string matching an arbitrary regex pattern, generated with rstr. None if rstr cannot handle the pattern or the result does not match."""
        try:
            candidate = rstr.xeger(pattern)
        except Exception:
            return None
        return candidate if re.search(pattern, candidate) else None

    def _number_value(self, prop_schema: dict, valid: bool) -> float | int | str:
        is_integer   = prop_schema.get("type") == "integer"
        real_minimum = prop_schema.get("minimum")
        real_maximum = prop_schema.get("maximum")
        minimum = real_minimum if real_minimum is not None else 0
        maximum = real_maximum if real_maximum is not None else 1000

        if not valid:
            # Only where there is a real bound to violate (a blind -999999 might still be valid)
            if real_minimum is not None:
                return real_minimum - (1 if is_integer else 0.01)
            if real_maximum is not None:
                return real_maximum + (1 if is_integer else 0.01)
            return random.choice(["NOT_A_NUMBER", "null", ""])

        if is_integer:
            return random.randint(int(minimum), int(maximum))
        val = minimum + random.random() * (maximum - minimum)
        return round(val, 2)

    def _boolean_value(self, prop_schema: dict, valid: bool) -> str:
        """Boolean for GUI View fill, which can only click a 'true' or 'false' button; returns the button label.

        An invalid pick is possible only if the schema narrows the field to a single value (const or one-item enum).
        Otherwise both buttons are valid and a valid pick is returned.
        """
        const_val = prop_schema.get("const")
        enum_vals = prop_schema.get("enum")
        restricted = None
        if const_val is not None and isinstance(const_val, bool):
            restricted = [const_val]
        elif enum_vals and set(enum_vals) <= {True, False} and len(enum_vals) == 1:
            restricted = list(enum_vals)

        if restricted:
            disallowed = [b for b in (True, False) if b not in restricted]
            if not valid and disallowed:
                return str(random.choice(disallowed)).lower()
            return str(random.choice(restricted)).lower()

        return random.choice(["true", "false"])

    def _boolean_value_typed(self, prop_schema: dict, valid: bool) -> Any:
        """Text View counterpart of _boolean_value for value_for_type: raw JSON text can express a type violation. Returns a Python value."""
        const_val = prop_schema.get("const")
        enum_vals = prop_schema.get("enum")
        allowed = [const_val] if const_val is not None else (enum_vals or [True, False])

        if valid:
            return random.choice(allowed)

        disallowed_bool = [b for b in (True, False) if b not in allowed]
        if disallowed_bool:
            return random.choice(disallowed_bool)
        return random.choice(["notabool", 1, 0, None])