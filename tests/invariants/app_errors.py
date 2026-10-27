"""Records errors thrown in the app page, in two independent records so that reading one never empties the other.

INV-G1 (uncaught JavaScript exceptions only): attach_app_error_log(page), take_new_app_errors(page).
State snapshot (console errors and uncaught exceptions): attach_console_log(page),
console_errors_since_last_snapshot(page), which returns None if the page was not being recorded yet
(the recording starts with that call).

The snapshot is built after the invariants have run, so it cannot share INV-G1's list.
"""
from __future__ import annotations


# Console messages written when the app's validator rejects the edited schema: it calls it invalid, or cannot resolve
# a reference (Ajv's MissingRefError after a renamed definition). The UI shows nothing for either. Matched anywhere in
# the line because the app prefixes it differently ("Validation worker error: ...", minified names such as "KVt: ...").
INVALID_SCHEMA_LOG = "schema is invalid"
UNRESOLVED_REF_LOG = "can't resolve reference"
APP_REJECTED_SCHEMA_LOGS = (INVALID_SCHEMA_LOG, UNRESOLVED_REF_LOG)


def is_app_schema_rejection(message) -> bool:
    """True if a console message says the app's own validator rejected the edited schema."""
    text = str(message)
    return any(marker in text for marker in APP_REJECTED_SCHEMA_LOGS)

_MAX_MESSAGE_CHARS = 300


_PENDING: dict[int, list[str]] = {}   # id(page) -> first line of each uncaught error not yet reported


def attach_app_error_log(page) -> None:
    key = id(page)
    if key in _PENDING:
        return
    _PENDING[key] = []

    def _on_error(err) -> None:
        _PENDING[key].append(str(getattr(err, "message", err)).strip().splitlines()[0])

    page.on("pageerror", _on_error)


def take_new_app_errors(page) -> list[str]:
    pending = _PENDING.get(id(page), [])
    taken, pending[:] = list(pending), []
    return taken


# What the snapshot's own record keeps, per kind: "error" (console errors and uncaught exceptions), "warning" (console
# warnings) and "failed_request" (network requests that failed, such as a blocked $ref download).
EVENT_KINDS = ("error", "warning", "failed_request")

_CONSOLE: dict[int, dict[str, list[str]]] = {}    # id(page) -> kind -> every message since recording started
_CONSOLE_CURSOR: dict[int, dict[str, int]] = {}   # id(page) -> kind -> how many of them earlier snapshots already reported


def _first_line(text) -> str:
    lines = str(text).strip().splitlines()
    return (lines[0] if lines else "")[:_MAX_MESSAGE_CHARS]


def attach_console_log(page) -> None:
    key = id(page)
    if key in _CONSOLE:
        return
    _CONSOLE[key] = {kind: [] for kind in EVENT_KINDS}
    _CONSOLE_CURSOR[key] = {kind: 0 for kind in EVENT_KINDS}

    def _on_console(msg) -> None:
        if msg.type == "error":
            _CONSOLE[key]["error"].append(_first_line(msg.text))
        elif msg.type == "warning":
            _CONSOLE[key]["warning"].append(_first_line(msg.text))

    def _on_page_error(err) -> None:
        _CONSOLE[key]["error"].append("pageerror: " + _first_line(getattr(err, "message", err)))

    def _on_request_failed(request) -> None:
        _CONSOLE[key]["failed_request"].append(_first_line(f"{request.method} {request.url} - {request.failure}"))

    page.on("console", _on_console)
    page.on("pageerror", _on_page_error)
    page.on("requestfailed", _on_request_failed)


def events_since_last_snapshot(page) -> dict[str, list[str]] | None:
    """{kind: messages} recorded since the previous call (kinds: EVENT_KINDS).

    None means the page was not being recorded before this call (so the messages of the step are unknown, not "none");
    the recording starts now. Call attach_console_log() right after the page is created to avoid that.
    """
    key = id(page)
    if key not in _CONSOLE:
        attach_console_log(page)
        return None
    out: dict[str, list[str]] = {}
    for kind in EVENT_KINDS:
        log, start = _CONSOLE[key][kind], _CONSOLE_CURSOR[key][kind]
        _CONSOLE_CURSOR[key][kind] = len(log)
        out[kind] = log[start:]
    return out


def console_errors_since_last_snapshot(page) -> list[str] | None:
    """Only the console errors and uncaught exceptions of events_since_last_snapshot() (all kinds are cut by the call)."""
    events = events_since_last_snapshot(page)
    return None if events is None else events["error"]


# Two-stage actions (add-item-required: click adds a blank entry, then the test fills it) pass through an intermediate
# state that the app validates and logs about, but that is gone when the step's snapshot is taken. The action marks the
# boundary between its stages; build_snapshot() then attributes the console errors before the mark to the intermediate
# state and the ones after it to the final state.
_MIDSTEP: dict[int, dict] = {}   # id(page) -> {"error_count": errors of this step before the mark, "schema_valid_meta": bool | None}


def mark_midstep(page, schema_valid_meta: bool | None) -> None:
    """Called between the stages of a two-stage action, once the app has had time to log about the intermediate state.

    schema_valid_meta is the meta-schema verdict on the schema as it is right now (None if it could not be determined).
    Does nothing if the page is not being recorded.
    """
    key = id(page)
    if key not in _CONSOLE:
        return
    _MIDSTEP[key] = {"error_count": len(_CONSOLE[key]["error"]) - _CONSOLE_CURSOR[key]["error"],
                     "schema_valid_meta": schema_valid_meta}


def take_midstep(page) -> dict | None:
    """The mark set during this step (and forget it), or None if the step had none."""
    return _MIDSTEP.pop(id(page), None)

# "Rename conflict" dialog (PrimeVue confirm dialog the app opens when a property name is committed and the parent
# already has that key). The executor dismisses it right away, so it is recorded in the page itself: a MutationObserver
# (installed on every document, also after a reload) appends the dialog text to window.__mcRenameConflicts. Reading it
# with take_rename_conflicts() empties it, so each snapshot gets only the dialogs of its own step.
_RENAME_CONFLICT_JS = """
(() => {
  if (window.__mcRenameConflictObserver) return;
  window.__mcRenameConflictObserver = true;
  window.__mcRenameConflicts = window.__mcRenameConflicts || [];
  const seen = new WeakSet();
  const scan = (root) => {
    if (!root || root.nodeType !== 1) return;
    const nodes = root.matches && root.matches('.p-confirmdialog') ? [root] : [...root.querySelectorAll('.p-confirmdialog')];
    for (const n of nodes) {
      if (seen.has(n)) continue;
      const text = (n.innerText || '').trim();
      if (!/Rename conflict/.test(text)) continue;   // the text is filled in a moment after the node appears
      seen.add(n);
      window.__mcRenameConflicts.push(text.replace(/\\s+/g, ' ').slice(0, 300));
    }
  };
  const start = () => new MutationObserver(ms => {
    for (const m of ms) { m.addedNodes.forEach(scan); if (m.type === 'characterData' || m.type === 'childList') scan(m.target.nodeType === 1 ? m.target : m.target.parentElement); }
  }).observe(document.documentElement, {childList: true, subtree: true, characterData: true});
  if (document.documentElement) start(); else document.addEventListener('DOMContentLoaded', start);
})();
"""

_RENAME_CONFLICT_ATTACHED: set[int] = set()


def attach_rename_conflict_log(page) -> None:
    """Start recording 'Rename conflict' dialogs on this page (current document and every later one)."""
    key = id(page)
    if key in _RENAME_CONFLICT_ATTACHED:
        return
    _RENAME_CONFLICT_ATTACHED.add(key)
    page.add_init_script(_RENAME_CONFLICT_JS)
    try:
        page.evaluate(_RENAME_CONFLICT_JS)
    except Exception:
        pass   # page not ready yet; the init script covers the next document


def take_rename_conflicts(page) -> list[str] | None:
    """The texts of the 'Rename conflict' dialogs opened since the previous call (empties the record).
    None if they could not be read (the step's dialogs are unknown, not "none")."""
    if id(page) not in _RENAME_CONFLICT_ATTACHED:
        attach_rename_conflict_log(page)
        return None
    try:
        return page.evaluate("() => { const r = window.__mcRenameConflicts || []; window.__mcRenameConflicts = []; return r; }")
    except Exception:
        return None