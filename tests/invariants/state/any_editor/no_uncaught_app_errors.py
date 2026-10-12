"""INV-G1."""
from __future__ import annotations
from tests.invariants.app_errors import take_new_app_errors
from tests.invariants.invariants_core import InvariantResult

ID = "INV-G1"
NAME = "_inv_g1_no_uncaught_app_errors"
# Other invariants wait up to seconds for the GUI to settle (INV-S4, INV-D4). Running after them means an
# exception the app throws during those waits is counted on this step, not on the next one.
RUN_LAST = True


def check(ctx) -> InvariantResult:
    """The app throws no uncaught JavaScript exception while handling an action.

    An exception in the GUI code leaves the GUI blank, half-drawn or frozen, and then every GUI-based invariant
    fails with a confusing symptom; this one reports the cause. It fails on a step when the app throws during it
    and keeps failing only while the app keeps throwing. A debounced schema reload can record the exception one
    step after its cause.
    Oracle: Playwright's page "pageerror" event, recorded by invariants/app_errors.py.
    """
    name = "INV-G1 No uncaught app exception"
    errors = take_new_app_errors(ctx.page)
    if not errors:
        return InvariantResult(name, True, "no uncaught exception since the previous check ✓")
    distinct = list(dict.fromkeys(errors))
    return InvariantResult(
        name, False,
        f"app threw {len(errors)} uncaught exception(s): {distinct[0]}",
        details={"app_errors": distinct[:5], "count": len(errors)},
        evidence=[f"uncaught: {m}" for m in distinct[:5]],
    )