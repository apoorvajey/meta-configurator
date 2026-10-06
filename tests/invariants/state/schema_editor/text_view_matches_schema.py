"""INV-S1. Loaded by invariants_core.load_invariant_plugins()."""
from __future__ import annotations
from tests.shared.python.utils_code_editor import read_text_editor_content
from tests.invariants.invariants_core import InvariantResult, _diagnose_unparseable_text

ID = "INV-S1"
NAME = "_inv_s1_text_view_reflects_schema"


def check(ctx) -> InvariantResult:
    """The schema Text View content must parse to exactly the internal schema. Same as INV-D1 but reads
    get_schema() and code-editor-schemaEditor. Only json and yaml are selectable.

    Selector: [id^="code-editor-schemaEditor"]
    """
    name = "INV-S1 Text View reflects internal schema"

    ground_truth = ctx.tp_data.get_schema()
    text_data = read_text_editor_content(ctx.page, ctx.mode)

    if text_data is None:
        diagnosis, details = _diagnose_unparseable_text(ctx.page, ctx.mode, ground_truth=ground_truth)
        return InvariantResult(name, False,
                               f"Schema Text View could not be parsed - {diagnosis}",
                               details)

    passed = ground_truth == text_data
    evidence = [
        f"Compared internal schema ({len(ground_truth) if isinstance(ground_truth, dict) else '?'} keys) "
        f"against Schema Text View → {'match ✓' if passed else 'mismatch ✗'}"
    ]
    return InvariantResult(
        name, passed,
        "Schema Text View matches internal schema ✓" if passed
        else "Schema Text View diverges from internal schema",
        {} if passed else {
            "internal_schema": ground_truth,
            "text_view": text_data,
        },
        evidence=evidence,
    )
