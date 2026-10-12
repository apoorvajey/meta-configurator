"""Console output of a mutation-loop run: quiet by default, everything kept in the log file.

While a RunConsole is installed (run_demo does that), everything the run prints goes to the log files
(tests/output/runs/<run_id>/console.log) and reaches the terminal only if verbose or surfaced with show(): new or
changed invariant failures, repair and schema-recovery messages, periodic progress and the end-of-run failure
summary (the amount a quiet replay prints). Without an installed RunConsole, show() is a plain print().

With MT_STEP_MARKERS=1 (set by the batch runner) the child also writes machine-readable "@@..." lines to the
terminal only; the batch runner reads them for its per-run summary and does not display them.
"""
from __future__ import annotations

import contextlib
import os

STEP_MARKERS_ENV = "MT_STEP_MARKERS"
MARKER_PREFIX = "@@"


class _Shown:
    """File-like object whose writes always reach the terminal, for redirect_stdout()."""

    def __init__(self, console: "RunConsole"):
        self._console = console

    def write(self, text: str) -> int:
        self._console.emit(text, to_terminal=True)
        return len(text)

    def flush(self) -> None:
        self._console.flush()


class RunConsole:
    """Replacement for sys.stdout: `files` get everything, `terminal` only if verbose or when surfaced."""

    def __init__(self, terminal, files, verbose: bool):
        self.terminal = terminal
        self.files = list(files)
        self.verbose = verbose
        self.shown = _Shown(self)

    def emit(self, text: str, to_terminal: bool) -> None:
        for f in self.files:
            f.write(text)
            f.flush()
        if to_terminal:
            self.terminal.write(text)
            self.terminal.flush()

    def write(self, text: str) -> int:
        self.emit(text, to_terminal=self.verbose)
        return len(text)

    def flush(self) -> None:
        for f in self.files:
            f.flush()
        self.terminal.flush()

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return getattr(self.terminal, "encoding", None) or "utf-8"


_active: RunConsole | None = None


def install(console: RunConsole) -> None:
    global _active
    _active = console


def uninstall() -> None:
    global _active
    _active = None


def active() -> RunConsole | None:
    return _active


def is_quiet() -> bool:
    """True while an installed console keeps routine output off the terminal."""
    return _active is not None and not _active.verbose


def set_verbose(verbose: bool) -> None:
    if _active is not None:
        _active.verbose = verbose


def show(text: str = "") -> None:
    """Print a line to the terminal even in quiet mode (and to the log files)."""
    if _active is None:
        print(text)
    else:
        _active.emit(text + "\n", to_terminal=True)


@contextlib.contextmanager
def surfaced():
    """Everything printed inside the block reaches the terminal, even in quiet mode."""
    if _active is None or _active.verbose:
        yield
    else:
        with contextlib.redirect_stdout(_active.shown):
            yield


def marker(text: str) -> None:
    """A machine-readable line for the batch runner: terminal only, and only if the batch asked for it."""
    if os.environ.get(STEP_MARKERS_ENV) != "1":
        return
    line = f"{MARKER_PREFIX}{text}\n"
    if _active is not None:
        _active.terminal.write(line)
        _active.terminal.flush()
    else:
        print(line, end="")
