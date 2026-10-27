"""Mode-specific invariant checkers: shared helpers, result classes, the two checker classes and the dispatcher.

InvariantDispatcher reads the active mode from the UI and runs InvariantCheckerDataEditor,
InvariantCheckerSchemaEditor, or nothing (Settings). Each invariant is one file in
invariants/state/<data_editor|schema_editor|any_editor>/, loaded by load_invariant_plugins() at the bottom.

Data Editor:   INV-D1 Text View = data, D3 required stars, D4 GUI rows for all data keys, D5 GUI strings = data,
               D6 validation icons = schema violations, INV-GUI-ERR-01 icon for the additionalProperties error.
Schema Editor: INV-S0 schema is an object, S1 Text View = schema, S2 GUI rows for top-level keys,
               S3 title field = schema, S4 validation icons = meta-schema violations.
Both:          INV-G1 no uncaught app errors, INV-G2 GUI not stuck loading.

There is no INV-D2: "required fields present in data" only holds as a transition postcondition (TEMP-ADD-03),
because undo can legitimately break it as a standing rule.
"""

from __future__ import annotations
import re
import time
from dataclasses import dataclass, field
from playwright.sync_api import Page
from jsonschema import validate, Draft202012Validator, RefResolver, FormatChecker
from tests.shared.python.utils import SessionMode
from tests.shared.python.test_panel import TestPanel
from tests.shared.python.utils_code_editor import read_text_editor_raw


_ADDITIONAL_PROPS_MSG = "must NOT have additional properties"
_JSON_POINTER_RE = re.compile(r"(/[^\s\"']+)")


def _path_to_testid_string(path: list) -> str:
    """Mirrors discovery.paths.path_to_testid_string() without importing mutation_testing.
    Keep in sync with pathToString() in MetaConfigurator's pathUtils.ts.
    """
    result = ""
    for p in path:
        if isinstance(p, int):
            result += f"[{p}]"
        else:
            result += f".{p}"
    return result[1:] if result.startswith(".") else result


def _find_additional_properties_violations(data, schema, path: list | None = None) -> list[tuple[list, str]]:
    """(parent_path, offending_key) for every key of an object whose schema has additionalProperties: false
    and lists the key under neither properties nor patternProperties.

    Follows properties and patternProperties through plain dict schemas only; does not resolve $ref, allOf or oneOf.
    """
    path = path or []
    violations: list[tuple[list, str]] = []
    if not isinstance(data, dict) or not isinstance(schema, dict):
        return violations

    properties = schema.get("properties", {}) or {}
    pattern_properties = schema.get("patternProperties", {}) or {}
    additional_properties = schema.get("additionalProperties", True)

    if additional_properties is False:
        for key in data:
            if key in properties:
                continue
            if any(re.search(pat, key) for pat in pattern_properties):
                continue
            violations.append((list(path), key))

    for key, value in data.items():
        child_schema = properties.get(key)
        if child_schema is None:
            for pat, pschema in pattern_properties.items():
                if re.search(pat, key):
                    child_schema = pschema
                    break
        if isinstance(child_schema, dict) and isinstance(value, dict):
            violations.extend(
                _find_additional_properties_violations(value, child_schema, path + [key])
            )

    return violations


def _value_violates_schema(value, prop_schema, root_schema=None) -> bool:
    """True if `value` violates `prop_schema`. $refs resolve against `root_schema`; errors count as valid."""
    if not prop_schema:
        return False
    try:
        if root_schema:
            resolver = RefResolver.from_schema(root_schema)
            validator = Draft202012Validator(prop_schema, resolver=resolver, format_checker=FormatChecker())
            return not validator.is_valid(value)
        validate(instance=value, schema=prop_schema)
        return False
    except Exception:
        return False


def _get_ace_annotations(page: Page, mode: SessionMode) -> list[dict]:
    """Ace's gutter annotations for the editor, [{row, column, text, type}, ...]. Assumed to be how AJV errors
    reach the Text View; not checked against every app version.
    """
    mode_str = "dataEditor" if mode == SessionMode.DataEditor else "schemaEditor"
    try:
        annotations = page.evaluate(f"""
            () => {{
                const editorEl = [...document.querySelectorAll('[id^="code-editor-{mode_str}"]')][0];
                if (!editorEl) return null;
                const editor = window.ace?.edit(editorEl.id);
                if (!editor) return null;
                return editor.session.getAnnotations();
            }}
        """)
    except Exception:
        return []
    return annotations or []


def _text_view_error_for_key(page: Page, mode: SessionMode, raw_text: str | None,
                             key: str, message_substr: str = _ADDITIONAL_PROPS_MSG) -> list[dict]:
    """Ace annotations containing `message_substr` that can be attributed to `key` by either:
    (a) a JSON pointer in the annotation text whose last segment is `key`, or
    (b) the annotation's row, looked up in the raw text for `"key"` (the likelier case: AJV keeps instancePath
        apart from the message).
    """
    annotations = _get_ace_annotations(page, mode)
    lines = raw_text.split("\n") if raw_text else []
    matches = []
    for ann in annotations:
        text = ann.get("text") or ""
        if message_substr not in text:
            continue
        row = ann.get("row")
        line = lines[row] if isinstance(row, int) and 0 <= row < len(lines) else ""
        pointer_match = _JSON_POINTER_RE.search(text)
        key_in_pointer = bool(pointer_match) and pointer_match.group(1).rstrip("/").split("/")[-1] == key
        key_in_line = f'"{key}"' in line
        if key_in_pointer or key_in_line:
            matches.append({
                "message": text,
                "row": row,
                "column": ann.get("column"),
                "line_text": line.strip(),
                "matched_via": "json_pointer" if key_in_pointer else "line_scan",
            })
    return matches


def _gui_error_icon_and_tooltip_for_path(page: Page, full_path: list) -> dict:
    """Path-aware version of InvariantCheckerDataEditor's _property_has_error_icon() and
    _get_validation_error_messages(), which only handle top-level keys. Nested paths are untested.
    """
    path_str = _path_to_testid_string(full_path)
    testid = f"property-metadata-{path_str}"
    container = page.get_by_test_id(testid)
    if container.count() == 0:
        return {"row_found": False, "icon_shown": False, "tooltip_text": None}

    icon_shown = container.get_by_test_id("validation-error-icon").count() > 0
    tooltip_text = None
    if icon_shown:
        try:
            container.hover()
            page.wait_for_timeout(1000)
            texts_loc = page.locator(".p-popover .text-red-600")
            texts = [texts_loc.nth(i).inner_text().strip() for i in range(texts_loc.count())]
            tooltip_text = " | ".join(t for t in texts if t)
        finally:
            page.keyboard.press("Escape")
            page.mouse.move(0, 0)
            try:
                page.wait_for_selector(".p-popover", state="hidden", timeout=1000)
            except Exception:
                pass
    return {"row_found": True, "icon_shown": icon_shown, "tooltip_text": tooltip_text}


def _diagnose_unparseable_text(page: Page, mode, ground_truth=None) -> tuple[str, dict]:
    """(message, details). If the content parses under the other format than the selected one (a known desync)
    and `ground_truth` is given, the alternate parse is diffed against it to show whether the data diverged
    too or only the format label is wrong.
    """
    import json as _json
    import yaml as _yaml

    raw = read_text_editor_raw(page, mode)
    if raw is None:
        return "Text View content could not be read at all (editor not found)", {}

    try:
        current_format = page.get_by_test_id("format-selector").inner_text().lower().strip()
    except Exception:
        current_format = "unknown"

    preview = raw[:300].replace("\n", "\\n")

    def _compare(parsed, label: str) -> dict:
        details = {"raw_preview": preview, "actually_parses_as": label}
        if ground_truth is not None:
            diffs = _diff_nested(ground_truth, parsed)
            if diffs:
                details["verdict"] = (f"{len(diffs)} field(s) differ - this is REAL data "
                                      f"divergence, not just a stale label")
                details["diffs"] = [f"{p}: {a!r}  →  {b!r}" for p, a, b in diffs]
            else:
                details["verdict"] = ("content matches internal state exactly - this is a "
                                      "COSMETIC label desync only, the data itself is fine")
        return details

    if "json" in current_format:
        try:
            parsed_yaml = _yaml.safe_load(raw)
            try:
                _json.loads(raw)
                is_also_json = True
            except Exception:
                is_also_json = False
            if not is_also_json:
                return ("content is valid YAML but format selector shows 'json' - likely "
                        "stale Ace undo history desynced from the format label",
                        _compare(parsed_yaml, "YAML"))
        except Exception:
            pass
        try:
            _json.loads(raw)
        except Exception as e:
            return f"format=json, JSON parse error: {e}", {"raw_preview": preview}
    elif "yaml" in current_format:
        try:
            parsed_json = _json.loads(raw)
            return ("content is valid JSON but format selector shows 'yaml' - likely "
                    "stale Ace undo history desynced from the format label",
                    _compare(parsed_json, "JSON"))
        except Exception:
            pass
        try:
            _yaml.safe_load(raw)
        except Exception as e:
            return f"format=yaml, YAML parse error: {e}", {"raw_preview": preview}

    return f"format={current_format}, content unparseable in either JSON or YAML", {"raw_preview": preview}


@dataclass
class InvariantResult:
    name: str
    passed: bool
    message: str
    details: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    # The check did not apply (precondition not met). passed stays True, but a skipped result is not
    # evidence that the invariant held: repair and statistics treat it as not evaluated.
    skipped: bool = False

    def __str__(self):
        icon = "skip" if self.skipped else ("✓" if self.passed else "✗")
        return f"  [{icon}] {self.name}: {self.message}"


def _print_detail_value(key: str, value) -> None:
    """Pretty-print details[key]: lists of dicts (e.g. INV-D6's per-property mismatches) one item per line with
    field labels, anything else on one line.
    """
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        print(f"      {key} ({len(value)}):")
        for i, item in enumerate(value):
            changes = item.get("changes")
            if isinstance(changes, list) and changes and all(isinstance(c, dict) and "path" in c for c in changes):
                # inferred-rule violation: one line per differing field instead of a cut-off before/after blob
                total = item.get("changed_fields", len(changes))
                print(f"        [{i}] {total} field(s) differ:")
                for c in changes:
                    print(f"            {c['path']}: {c.get('before')}  →  {c.get('after')}")
                if total > len(changes):
                    print(f"            … and {total - len(changes)} more")
                for k, v in item.items():
                    if k not in ("changes", "changed_fields"):
                        print(f"            {k}: {v!r}")
                continue
            fields = ", ".join(f"{k}={v!r}" for k, v in item.items())
            print(f"        [{i}] {fields}")
    elif isinstance(value, list) and value:
        print(f"      {key} ({len(value)}):")
        for item in value:
            print(f"        • {item}")
    else:
        print(f"      {key}: {value}")


def _diff_nested(a, b, path: str = "") -> list[tuple[str, object, object]]:
    """(path, a_value, b_value) for every difference between two values (INV-D1 and INV-S1 details).
    Recurses into dicts only; any other difference, lists included, is one leaf.
    """
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            sub_path = f"{path}.{key}" if path else key
            if key not in a:
                diffs.append((sub_path, "<missing>", b[key]))
            elif key not in b:
                diffs.append((sub_path, a[key], "<missing>"))
            else:
                diffs.extend(_diff_nested(a[key], b[key], sub_path))
    elif a != b:
        diffs.append((path, a, b))
    return diffs


@dataclass
class InvariantReport:
    results: list[InvariantResult] = field(default_factory=list)
    mode: str = ""

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[InvariantResult]:
        return [r for r in self.results if not r.passed]

    @property
    def skipped(self) -> list[InvariantResult]:
        return [r for r in self.results if r.skipped]

    def summary(self) -> str:
        n = len(self.results)
        f = len(self.failures)
        prefix = f"[{self.mode}] " if self.mode else ""
        k = len(self.skipped)
        skipped_note = f" ({k} skipped)" if k else ""
        return (
            f"{prefix}All {n} invariants passed ✓{skipped_note}"
            if f == 0
            else f"{prefix}{f}/{n} FAILED ✗"
        )

    def print(self):
        if self.passed:
            print(f"\n  {self.summary()}")
        else:
            for r in self.failures:
                print(r)
            print(f"\n  {self.summary()}")
            for r in self.failures:
                if r.evidence:
                    print(f"\n    Evidence for {r.name}:")
                    for line in r.evidence:
                        print(f"      • {line}")
                if r.details:
                    print(f"    Details for {r.name}:")
                    left_key = ("internal_state" if "internal_state" in r.details
                                else "internal_schema" if "internal_schema" in r.details
                    else None)
                    if left_key and "text_view" in r.details:
                        diffs = _diff_nested(r.details[left_key], r.details["text_view"])
                        if diffs:
                            print(f"      {len(diffs)} field(s) differ ({left_key} vs text_view):")
                            for path, a_val, b_val in diffs:
                                print(f"        {path}: {a_val!r}  →  {b_val!r}")
                        else:
                            print("      No field-level differences found by shallow diff - "
                                  "check for key ordering or a type mismatch it didn't catch")
                        for k, v in r.details.items():
                            if k not in (left_key, "text_view"):
                                _print_detail_value(k, v)
                    else:
                        for k, v in r.details.items():
                            _print_detail_value(k, v)


_ONLY_IDS: set[str] | None = None   # None = run every state invariant (the default)


def set_invariant_filter(ids) -> None:
    """Run only the state invariants whose id is in `ids` (e.g. {"INV-S1"}); None restores the default (all).
    An empty set runs none of them. Temporal and inferred rules are not affected: they work on snapshots.
    Used by the test case reducer, where every step only needs to know whether ONE invariant failed.
    """
    global _ONLY_IDS
    _ONLY_IDS = None if ids is None else set(ids)


class _BaseInvariantChecker:
    """Runs every method whose name starts with _inv_. Subclasses define the invariants."""

    def __init__(self, page: Page, mode: SessionMode):
        self.page = page
        self.mode = mode
        self.tp = TestPanel(page, mode)

    def run_all(self) -> InvariantReport:
        report = InvariantReport(mode=self.mode.value)
        self.run_started = time.monotonic()   # lets a RUN_LAST check count the time earlier checks already took
        # Name order, except checks with RUN_LAST (see load_invariant_plugins), which go last.
        names = sorted((n for n in dir(self) if n.startswith("_inv_")),
                       key=lambda n: (getattr(getattr(self, n), "run_last", False), n))
        for name in names:
            if _ONLY_IDS is not None:
                plugin = INVARIANT_REGISTRY.get(name)
                if plugin is not None and plugin.id not in _ONLY_IDS:
                    continue
            try:
                result: InvariantResult = getattr(self, name)()
                report.results.append(result)
            except Exception as e:
                _toks = []
                for _t in name[len("_inv_"):].split("_"):
                    _toks.append(_t.upper())
                    if re.fullmatch(r"[a-z]?\d+", _t):
                        break
                report.results.append(InvariantResult(
                    name=f"CHK-ERR-INV-{'-'.join(_toks)} check crashed",
                    passed=False,
                    message=f"Exception during check ({type(e).__name__}): {e}",
                ))
        return report


class InvariantCheckerDataEditor(_BaseInvariantChecker):
    """Invariants for Data Editor mode. Ground truth: TestPanel(page, SessionMode.DataEditor).get_data()"""

    def __init__(self, page: Page):
        super().__init__(page, SessionMode.DataEditor)

    def _gui_row_has_value(self, key: str) -> bool:
        """True if the top-level row for `key` shows content, False if it is an empty schema placeholder.

        Object properties are pre-rendered with a nested form for their required sub-properties even when the key
        is absent from data, so a nested property-data-* row is not evidence of a value. Array rows only exist
        when the data has items.
        """
        row = self.page.locator(f'[data-testid="property-data-{key}"]')
        if row.count() == 0:
            return False

        try:
            for el in row.locator('input, textarea, select').all():
                if el.get_attribute('type', timeout=1000) == 'checkbox':
                    if el.is_checked(timeout=1000):
                        return True
                    continue
                if (el.input_value(timeout=1000) or '').strip():
                    return True
        except Exception:
            # The row was re-rendered or removed while being read (GUI render lag): nothing stale to report.
            return False

        if row.locator(f'[data-testid^="property-data-{key}["]').count() > 0:
            return True

        return False

    def _property_has_error_icon(self, key: str) -> dict:
        """{"row_found", "icon_shown"}, kept separate so INV-D6 evidence can tell a row that never rendered
        from a row without an icon.
        """
        try:
            container = self.page.get_by_test_id(f"property-metadata-{key}")
            if container.count() == 0:
                return {"row_found": False, "icon_shown": False}
            icon = container.get_by_test_id("validation-error-icon")
            return {"row_found": True, "icon_shown": icon.count() > 0}
        except Exception as e:
            print(f"   [INV-D6] error icon check for '{key}' raised: {e}")
            return {"row_found": False, "icon_shown": False}

    def _get_validation_error_messages(self, key: str) -> list[str]:
        """Hover over the property metadata row to open the overlay, then read the red error messages."""
        try:
            metadata = self.page.get_by_test_id(f"property-metadata-{key}")
            if not metadata.is_visible():
                return []
            metadata.hover()
            self.page.wait_for_timeout(1000)
            errors = self.page.locator(".p-popover .text-red-600")
            messages = []
            for i in range(errors.count()):
                text = errors.nth(i).inner_text().strip()
                if text:
                    messages.append(text)
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(100)
            self.page.mouse.move(0, 0)
            self.page.wait_for_timeout(300)
            try:
                self.page.wait_for_selector(".p-popover", state="hidden", timeout=1000)
            except Exception:
                pass
            return messages
        except Exception:
            return []

class InvariantCheckerSchemaEditor(_BaseInvariantChecker):
    """Invariants for Schema Editor mode. Ground truth: TestPanel(page, SessionMode.SchemaEditor).get_schema()"""

    def __init__(self, page: Page):
        super().__init__(page, SessionMode.SchemaEditor)
        # the user's schema is exposed through the DataEditor test panel
        self.tp_data = TestPanel(page, SessionMode.DataEditor)


class InvariantDispatcher:
    """Reads the active editor mode from the UI and runs the matching checker (Settings: an empty passing report)."""

    def __init__(self, page: Page):
        self.page = page

    def current_mode(self) -> SessionMode | None:
        try:
            active = self.page.get_by_test_id("mode-active-true")
            text = active.inner_text().strip()
            if "Data" in text:
                return SessionMode.DataEditor
            if "Schema" in text:
                return SessionMode.SchemaEditor
            if "Settings" in text:
                return SessionMode.Settings
        except Exception:
            pass
        return None

    def run_all(self) -> InvariantReport:
        mode = self.current_mode()

        if mode == SessionMode.DataEditor:
            return InvariantCheckerDataEditor(self.page).run_all()

        if mode == SessionMode.SchemaEditor:
            return InvariantCheckerSchemaEditor(self.page).run_all()

        if mode == SessionMode.Settings:
            report = InvariantReport(mode="settings")
            report.results.append(InvariantResult(
                name="Settings mode",
                passed=True,
                message="Settings mode - invariant checks skipped",
            ))
            return report

        report = InvariantReport(mode="unknown")
        report.results.append(InvariantResult(
            name="Mode detection",
            passed=False,
            message="Could not detect current editor mode",
        ))
        return report


class InvariantChecker(InvariantDispatcher):
    """InvariantDispatcher that accepts a `mode` argument but ignores it; the mode is read from the UI."""

    def __init__(self, page: Page, mode: SessionMode = SessionMode.DataEditor):
        super().__init__(page)


# Invariant plugins: one file per invariant in tests/invariants/state/<data_editor|schema_editor>/
@dataclass(frozen=True)
class InvariantPlugin:
    id: str         # e.g. "INV-D4", as shown in reports
    name: str       # method name, e.g. "_inv_d4_gui_rows_exist_for_data_keys"; stays stable for logs/reports
    mode: str       # "data", "schema" or "any" (both editors)
    check: object   # callable(ctx) -> InvariantResult, ctx is the checker instance
    module: str     # path below tests/invariants/state, e.g. "data_editor/gui_rows_match_data_keys"


INVARIANT_REGISTRY: dict[str, InvariantPlugin] = {}

# The folder below tests/invariants/state decides which editor mode an invariant runs in.
MODE_FOLDERS = {"data_editor": "data", "schema_editor": "schema", "any_editor": "any"}


def _bound_method(check, run_last: bool = False):
    """Wrap a plugin's check(ctx) as a method, so _BaseInvariantChecker.run_all() finds it via dir()."""
    def method(self):
        return check(self)
    method.__doc__ = check.__doc__
    method.run_last = run_last
    return method


def load_invariant_plugins() -> dict[str, InvariantPlugin]:
    """Import every module below invariants/state and attach its check() to the checker class of its folder.
    Safe to call more than once. A broken plugin raises: a silently missing invariant would make a run look
    healthier than it is.

    A plugin defines ID (e.g. "INV-D4"), NAME (method name starting with "_inv_"; appears in history.json and
    reports, so keep it stable) and check(ctx). Optional RUN_LAST = True runs it after all others in its editor.
    """
    import importlib
    import pkgutil
    from tests.invariants import state as package

    classes = {"data": InvariantCheckerDataEditor, "schema": InvariantCheckerSchemaEditor}
    depth = len(package.__name__.split("."))
    for info in sorted(pkgutil.walk_packages(package.__path__, f"{package.__name__}."), key=lambda i: i.name):
        parts = info.name.split(".")[depth:]          # e.g. ["data_editor", "gui_rows_match_data_keys"]
        if info.ispkg or any(p.startswith("_") for p in parts):
            continue
        label = "/".join(parts)
        mode = MODE_FOLDERS.get(parts[0]) if len(parts) == 2 else None
        if mode is None:
            raise ImportError(f"{label}: must be in a folder named {' or '.join(sorted(MODE_FOLDERS))}")
        module = importlib.import_module(info.name)
        inv_id, name, check = (getattr(module, a, None) for a in ("ID", "NAME", "check"))
        if not (isinstance(inv_id, str) and isinstance(name, str) and name.startswith("_inv_") and callable(check)):
            raise ImportError(f"{label}: needs ID (str), NAME starting with '_inv_' and a callable check(ctx)")
        for other in INVARIANT_REGISTRY.values():
            if other.module != label and (inv_id == other.id or name == other.name):
                raise ImportError(f"{label}: ID {inv_id} or NAME {name} is already defined in {other.module}")
        INVARIANT_REGISTRY[name] = InvariantPlugin(inv_id, name, mode, check, label)
        run_last = bool(getattr(module, "RUN_LAST", False))
        for cls in (classes.values() if mode == "any" else [classes[mode]]):
            setattr(cls, name, _bound_method(check, run_last))
    return INVARIANT_REGISTRY


def list_invariants() -> list[InvariantPlugin]:
    """All loaded invariants, data mode first. Used by the CLI command `invariants`."""
    return sorted(load_invariant_plugins().values(), key=lambda p: (p.mode, p.id))


load_invariant_plugins()   # must stay the last statement: the plugins import names defined above