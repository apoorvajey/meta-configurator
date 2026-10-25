"""INV-G2."""
from __future__ import annotations
import time
from tests.invariants.invariants_core import InvariantResult
from tests.invariants.schema_validity import schema_meta_violations
from tests.shared.python.test_panel import TestPanel
from tests.shared.python.utils import SessionMode

ID = "INV-G2"
NAME = "_inv_g2_gui_not_stuck_loading"
RUN_LAST = True   # the other checks take time too, and that time counts towards the wait below

_LOADING_SELECTOR = ".p-treetable-loading-icon"   # PrimeVue TreeTable spinner, seen in the stuck Schema GUI
_SETTLE_MS = 2500   # the app shows the spinner ~1.5 s after the edit (measured), so look only after this
_WAIT_MS = 3000
_POLL_MS = 250


def _schema_is_invalid(page) -> bool:
    """True if the edited schema is invalid against the meta-schema. False if it is valid or cannot be determined."""
    try:
        schema = TestPanel(page, SessionMode.DataEditor).get_schema()
        return bool(schema_meta_violations(page, schema))
    except Exception:
        return False


def check(ctx) -> InvariantResult:
    """The GUI is not stuck on a loading spinner.

    The app reloads the schema from a debounced callback, so a stuck spinner shows about 1.5 s after the edit.
    The check first waits until _SETTLE_MS have passed since this step's checks started (it runs last, so their
    time counts); a spinner still there _WAIT_MS later means the GUI froze.
    Oracle: the TreeTable loading icon is absent from the page.

    Skipped while the edited schema is invalid: the GUI cannot load an invalid schema, so a missing GUI or a
    spinner is expected there (same rule as INV-D6).
    """
    name = "INV-G2 GUI not stuck loading"
    page = ctx.page
    if _schema_is_invalid(page):
        return InvariantResult(name, True,
                               "SKIPPED: schema is invalid - the GUI cannot load it by design",
                               skipped=True)
    started = getattr(ctx, "run_started", None)
    already_ms = int((time.monotonic() - started) * 1000) if started else 0
    page.wait_for_timeout(max(0, _SETTLE_MS - already_ms))
    count = page.locator(_LOADING_SELECTOR).count()
    waited = 0
    while count and waited < _WAIT_MS:
        page.wait_for_timeout(_POLL_MS)
        waited += _POLL_MS
        count = page.locator(_LOADING_SELECTOR).count()
    if not count:
        return InvariantResult(name, True, "no loading spinner on the page ✓")
    return InvariantResult(
        name, False,
        f"GUI still shows {count} loading spinner(s) after {_WAIT_MS} ms",
        details={"spinners": count, "waited_ms": waited},
        evidence=[f"{count} element(s) matching {_LOADING_SELECTOR} still present after {_WAIT_MS} ms"],
    )