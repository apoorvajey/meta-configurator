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