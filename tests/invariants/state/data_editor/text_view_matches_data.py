"""INV-D1."""
from __future__ import annotations
import json
from tests.shared.python.utils_code_editor import read_text_editor_content, read_text_editor_raw
from tests.invariants.invariants_core import InvariantResult, _diagnose_unparseable_text

ID = "INV-D1"
NAME = "_inv_d1_text_view_reflects_data"


def _text_is_json_null(ctx) -> bool:
    """read_text_editor_content() returns None for "could not parse" and also for a document that is
    literally `null`, which is valid JSON and valid YAML. Tell them apart from the raw text."""
    raw = read_text_editor_raw(ctx.page, ctx.mode)
    if raw is None:
        return False
    if not raw.strip():  # blank YAML document = null
        return True
    try:
        return json.loads(raw) is None
    except ValueError:
        return False


def check(ctx) -> InvariantResult:
    """The Text View content must parse to exactly the internal data. An unparseable Text View is always
    a failure. Only json and yaml are selectable formats.

    Selector: [id^="code-editor-dataEditor"]. See panelTextEditor.spec.ts.
    """
    name = "INV-D1 Text View reflects internal data"

    ground_truth = ctx.tp.get_data()
    text_data = read_text_editor_content(ctx.page, ctx.mode)

    if text_data is None and not _text_is_json_null(ctx):
        diagnosis, details = _diagnose_unparseable_text(ctx.page, ctx.mode, ground_truth=ground_truth)
        return InvariantResult(name, False,
                               f"Text View could not be parsed - {diagnosis}",
                               details)

    passed = ground_truth == text_data
    evidence = [
        f"Compared internal data ({len(ground_truth) if isinstance(ground_truth, dict) else '?'} keys) "
        f"against Text View content → {'match ✓' if passed else 'mismatch ✗'}"
    ]
    return InvariantResult(
        name, passed,
        "Text View matches internal data ✓" if passed
        else "Text View diverges from internal data",
        {} if passed else {
            "internal_state": ground_truth,
            "text_view": text_data,
        },
        evidence=evidence,
    )