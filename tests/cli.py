"""Entry point for all mutation-testing tools; run from the repository root.

    python -m tests.cli                 interactive menu
    python -m tests.cli <command> ...   options after a command are passed to the tool behind it

`run --runs N` starts the batch runner and then builds the report; `invariants` runs in this script.
To add a tool, add one line to COMMANDS.
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_TOOL = 3
EXIT_INTERRUPTED = 130

# command -> (description, candidate modules; the first one that exists is used)
COMMANDS: dict[str, tuple[str, list[str]]] = {
    "run": ("Run the mutation loop once (--runs N: N unattended runs, then the report)",
            ["tests.mutation_testing.loop.mutation_loop"]),
    "report": ("Show the distinct problems found across all runs",
               ["tests.mutation_testing.reports.issue_report"]),
    "replay": ("Replay the latest run (--history <history.json> for another one)",
               ["tests.mutation_testing.replay.replay_runner"]),
    "infer": ("Mine invariants from the recorded traces",
              ["tests.mutation_testing.inference.invariant_inference_engine"]),
    "sweep": ("Show how the promotion threshold changes the mined invariants",
              ["tests.mutation_testing.inference.threshold_sweep"]),
    "archive": ("Archive recorded traces (after a major code change, before a batch)",
                ["tests.mutation_testing.traces.trace_store"]),
    "corpus": ("HTML report of the runs in the recorded traces",
               ["tests.mutation_testing.traces.corpus_report"]),
    "review": ("Review the inferred rules (html / export / import)",
               ["tests.mutation_testing.inference.review_tool"]),
    "bugs": ("Known bugs page: view, add, remove (import = apply a downloaded update)",
             ["tests.mutation_testing.reports.known_bugs_tool"]),
    "stats": ("Numbers for the evaluation: rule activity, bug detection, invariant usage, template yield, sweep, corpus (stats <report>)",
              ["tests.mutation_testing.stats"]),
}
BATCH_MODULES = ["tests.mutation_testing.loop.batch_runner"]
LIST_COMMAND = "invariants"
LIST_DESCRIPTION = "List every invariant, hand-written and inferred, and refresh the HTML report (opens the HTML report)"
HELP_FLAGS = ("-h", "--help", "help")
OPTION_HELP_FLAGS = ("-h", "--help")   # after a command: show that command's help instead of running it

# command -> (what it does, example command lines); shown by `python -m tests.cli <command> --help`, above the tool's own option list
COMMAND_HELP: dict[str, tuple[str, list[str]]] = {
    "run": ("One mutation-loop run, or an unattended batch with --runs / --max-minutes (then the issue report follows; --no-report skips it).",
            ["python -m tests.cli run",
             "python -m tests.cli run --runs 40 --max-minutes 480"]),
    "report": ("HTML report of the problems found in the recorded runs.",
               ["python -m tests.cli report --last-hours 12"]),
    "replay": ("Replay a recorded run (default: the newest) and check its failures again.",
               ["python -m tests.cli replay --history tests/output/runs/<run_id>/history.json"]),
    "infer": ("Mine invariants from the traces. --export-only rewrites inferred_rules.json from your review without mining.",
              ["python -m tests.cli infer", "python -m tests.cli infer --export-only"]),
    "sweep": ("Show how the promotion threshold changes the number of mined invariants.",
              ["python -m tests.cli sweep --thresholds 0.8,0.9"]),
    "archive": ("Move the recorded traces into a timestamped archive, or list / prune them.",
                ["python -m tests.cli archive --label before-batch", "python -m tests.cli archive --list"]),
    "corpus": ("HTML report of the runs in the recorded traces.", ["python -m tests.cli corpus"]),
    "review": ("Rule review in one go: opens the page; click Download there, press Enter here, and the review is moved to rule_reviews and applied (checked rules, learned-bug checks, bugs logged).",
               ["python -m tests.cli review", "python -m tests.cli review html   (only build the page)"]),
    "bugs": ("Known bugs page (log, edit, group, remove): click Update there, press Enter here; the file is applied and moved to bug_updates. `bugs import` does it without the page.",
             ["python -m tests.cli bugs", "python -m tests.cli bugs import"]),
    "stats": ("Read-only reports with the numbers for the evaluation chapter. `stats` alone lists them. Every report saves a report page (sweep: files) in tests/output/reports/stats/; rules, bugs and invariants take --csv to print CSV instead. "
              "`sweep` and `corpus` also work as commands of their own.",
              ["python -m tests.cli stats", "python -m tests.cli stats rules --csv",
               "python -m tests.cli stats bugs --by profile", "python -m tests.cli stats bugs --by invariant"]),
    "invariants": (LIST_DESCRIPTION + ". No options.", ["python -m tests.cli invariants"]),
}

RUNS_FLAG = "--runs"
MAX_MINUTES_FLAG = "--max-minutes"   # a time budget also makes it a batch, with or without --runs
NO_REPORT_FLAG = "--no-report"
ORIGIN_ENV = "MT_ORIGIN_COMMAND"   # the command line the user ran; the report prints it so the run can be repeated
RUNS_DEFAULT = "15"                # default of batch_runner --runs; written out when the menu left it open
SINCE_FORMAT = "%Y%m%d_%H%M%S"   # the timestamp format of run ids; the report accepts it for --since


def usage() -> str:
    rows = [(name, desc) for name, (desc, _) in COMMANDS.items()] + [(LIST_COMMAND, LIST_DESCRIPTION)]
    width = max(len(name) for name, _ in rows)
    lines = ["usage: python -m tests.cli [command] [options]", "",
             "Without a command, a menu asks what to do.", "", "commands:"]
    lines += [f"  {name:<{width}}  {desc}" for name, desc in rows]
    lines += ["", "Description, examples and options of one command: python -m tests.cli <command> --help"]
    return "\n".join(lines)


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _launch(candidates: list[str], args: list[str]) -> int:
    """Run the first existing module of `candidates` with `args`; keyboard and screen are shared."""
    module = next((m for m in candidates if _module_exists(m)), None)
    if module is None:
        print(f"None of these modules exist: {', '.join(candidates)}. Fix COMMANDS in tests/cli.py.",
              file=sys.stderr)
        return EXIT_MISSING_TOOL
    return subprocess.call([sys.executable, "-m", module, *args])


def command_help(command: str, rest: list[str] | None = None) -> int:
    """What the command does, examples, then the option list of the tool behind it. Never starts the tool's work."""
    description, examples = COMMAND_HELP[command]
    print(f"python -m tests.cli {command}\n")
    print(textwrap.fill(description, width=90))
    print("\nExamples:")
    print("\n".join(f"  {e}" for e in examples))
    if command == LIST_COMMAND:
        return EXIT_OK
    print()
    if command == "run":
        print("Batch options (a single run has none; see run_config.json):\n")
        return _launch(BATCH_MODULES, ["--help"])
    sub = ["import"] if (command == "bugs" and rest and rest[0] == "import") else []
    if command == "stats" and rest and not rest[0].startswith("-"):
        sub = [rest[0]]   # python -m tests.cli stats bugs --help: the options of that report
    return _launch(COMMANDS[command][1], sub + ["--help"])


def _is_batch(args: list[str]) -> bool:
    return any(a in (RUNS_FLAG, MAX_MINUTES_FLAG) or a.startswith((RUNS_FLAG + "=", MAX_MINUTES_FLAG + "="))
               for a in args)


def batch_and_report(batch_args: list[str], report: bool = True) -> int:
    """Unattended batch of runs, then the report restricted to the runs this batch produced."""
    started = datetime.now().strftime(SINCE_FORMAT)
    code = _launch(BATCH_MODULES, batch_args)
    if code != EXIT_OK or not report:
        return code
    print("\nBatch finished. Building the report for these runs ...")
    return _launch(COMMANDS["report"][1], ["--since", started, "--no-wait"])


def batch_command(args: list[str]) -> int:
    return batch_and_report([a for a in args if a != NO_REPORT_FLAG], report=NO_REPORT_FLAG not in args)


def run_command(args: list[str]) -> int:
    return batch_command(args) if _is_batch(args) else _launch(COMMANDS["run"][1], args)


def _remember_command(command_line: str) -> None:
    """Hand the command line to the tools started below (environment variable); the report shows it."""
    os.environ[ORIGIN_ENV] = command_line


def list_invariants_command(args: list[str]) -> int:
    """Rebuild the invariants report (hand-written and inferred) and open it in the browser."""
    if args:
        print("usage: python -m tests.cli invariants", file=sys.stderr)
        return EXIT_USAGE
    import webbrowser
    from tests.mutation_testing.reports.invariant_list import collect_invariants, write_report
    rows = collect_invariants()
    out = write_report(rows)
    n_inferred = sum(1 for r in rows if r["origin"] == "inferred")
    print(f"{len(rows)} invariant(s): {len(rows) - n_inferred} hand-written, {n_inferred} inferred -> {out}")
    webbrowser.open(out.resolve().as_uri())
    return EXIT_OK


# Menu. Each action asks only what it needs; Enter uses the tool's default. Settings that cannot be combined are
# the alternatives of one Pick; rarely needed ones sit behind "More options?".

def _split_args(text: str) -> list[str]:
    """Split typed options like a shell, but keep backslashes (Windows paths) and drop surrounding quotes."""
    return [token.strip("\"'") for token in shlex.split(text, posix=False)]


@dataclass(frozen=True)
class Ask:
    """One question; Enter skips it (the tool's default applies) unless `required`.

    flag=None is a positional argument. kind: "text", "int", "float", "fraction" (0 to 1), "choice",
    "yes" (y adds the flag), "no" (n adds the flag, for --no-... flags), "pick" (one of a numbered list)
    or "multi" (several, e.g. 1,3). `options` returns the list for pick/multi when the question is asked;
    if it returns nothing, the question falls back to free text.
    """
    label: str
    flag: str | None
    kind: str = "text"
    hint: str = ""
    choices: tuple[str, ...] = ()
    required: bool = False
    options: Callable[[], list[str]] | None = None
    default: str | None = None      # used when Enter is pressed on an optional question (e.g. a positional argument)
    omit: str | None = None         # an answer that adds no argument (it only names the tool's no-argument mode)


@dataclass(frozen=True)
class Pick:
    """Alternatives that cannot be combined: (label, fixed arguments, follow-up questions). The first is the default."""
    label: str
    options: tuple[tuple[str, tuple[str, ...], tuple[object, ...]], ...]


@dataclass(frozen=True)
class Check:
    """Not a question: code that runs when its turn comes in the questions. It gets the arguments collected so far
    and may ask something itself; it returns extra arguments, or raises _Back to leave without running."""
    run: Callable[[list[str], list[str], object], list[str]]


@dataclass(frozen=True)
class Action:
    group: str                      # heading in the menu
    label: str
    summary: str                    # one line next to the label
    command: str                    # name shown in the echoed command line
    run: Callable[[list[str]], int]
    steps: tuple[object, ...] = ()  # the questions that matter for most runs (Ask / Pick)
    more: tuple[object, ...] = ()   # rarely needed questions, asked only after "More options?"
    more_hint: str = ""             # what is behind "More options?"
    implied_flag: str | None = None  # flag the typed command needs but the questions may leave out
    confirm: bool = True            # show the command line and wait for Enter before running


def _batch_schemas() -> list[str]:
    """The schemas listed in "_schema_options" of run_config.json (the batch runner reads the same list)."""
    try:
        from tests.mutation_testing.loop.batch_runner import read_configured_options
        return read_configured_options()[0]
    except Exception:
        return []


def _batch_profiles() -> list[str]:
    """The profiles listed in "_action_weights_profile_options" of run_config.json."""
    try:
        from tests.mutation_testing.loop.batch_runner import read_configured_options
        return read_configured_options()[1]
    except Exception:
        return []


def _inference_profiles() -> list[str]:
    """The profiles defined in inference_config.json (keys starting with "_" are documentation)."""
    try:
        import json
        from tests.mutation_testing.inference.inference_config import DEFAULT_CONFIG_PATH
        raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        return [name for name, section in raw.items() if not name.startswith("_") and isinstance(section, dict)]
    except Exception:
        return []


TIME_HINT = "YYYY-MM-DD HH:MM"
DATASETS = Ask("Only these datasets", "--datasets", hint="comma-separated")
PROFILE = Ask("Settings profile", "--profile", "pick", hint="Enter = default", options=_inference_profiles)
MIN_OBS = Ask("Minimum observations per dataset", "--min-obs", "int")
TRACES = Ask("Folder with the recorded traces", "--snapshots-dir")
HEADLESS = Ask("Hide the browser window", "--headless", "yes")

def _clean_run_gate(positional: list[str], flagged: list[str], ask) -> list[str]:
    """Replay of one run: look at the run first. If it is already clean (nothing to reproduce), ask yes/no right here,
    before "How should it run?". Yes passes --clean-runs include so the tool does not ask again; no goes back."""
    try:
        from pathlib import Path
        from tests.mutation_testing.runs.history_io import find_latest_history
        from tests.mutation_testing.runs.run_layout import is_clean, resolve_history_path
        if "--history" in flagged:
            history = resolve_history_path(Path(flagged[flagged.index("--history") + 1]))
        else:
            history = find_latest_history()
        clean = history is not None and is_clean(history)
    except Exception:
        return []                       # cannot tell: the tool itself still checks and asks
    if not clean:
        return []
    folder = history.resolve().parent.name
    if _read(ask, f"\n  {folder} is already clean (no failures). Replay it anyway? [y/N]: ").lower() in ("y", "yes"):
        return ["--clean-runs", "include"]
    print("  Skipped.")
    raise _Back


# Replaying one run: how it is stepped through. These are alternatives because --headless cannot be combined
# with the manual modes, and --interactive and --until are two ways to start stepping by hand.
REPLAY_HOW = Pick("How should it run?", (
    ("Automatically", (), ()),
    ("Automatically, pausing when a step fails", ("--pause-on-failure",), ()),
    ("Automatically, with the browser window hidden", ("--headless",), ()),
    ("Step through by hand from the start", ("--interactive",), ()),
    ("Quietly up to a step, then step through by hand", (),
     (Ask("Step number", "--until", "int", required=True),)),
))
# Several runs: what to do with runs that have no failures left (replay_runner --clean-runs). For a single run there
# is no menu question: replay_runner checks the run itself and asks "Replay it anyway? [y/N]" only if it is clean.
CLEAN_RUNS = Ask("If a run is already clean", "--clean-runs", "choice", hint="Enter = skip",
                 choices=("ask", "skip", "include"))
# Several runs are replayed without waiting for input, so the step-through modes do not apply to them.
REPLAY_WHAT = Pick("What should be replayed?", (
    ("The latest run", (), (Check(_clean_run_gate), REPLAY_HOW)),
    ("One specific run", (), (Ask("Run folder or history.json", "--history",
                                  hint="path to a run folder or to its history.json", required=True),
                              Check(_clean_run_gate), REPLAY_HOW)),
    ("Every run since a time", (), (CLEAN_RUNS,
                                    Ask("Since", "--since", hint=TIME_HINT + ", or an age like 48h", required=True),
                                    Ask("Only from this folder", "--folder"), HEADLESS)),
    ("Every run in a folder", (), (CLEAN_RUNS,
                                   Ask("Folder", "--folder", required=True),
                                   Ask("Only runs since", "--since", hint=TIME_HINT + ", or an age like 48h"),
                                   HEADLESS)),
))
REPORT_PERIOD = Pick("Which runs?", (
    ("All of them", (), ()),
    ("The last few hours", (), (Ask("Number of hours", "--last-hours", "float", required=True),)),
    ("A time window", (), (Ask("From", "--since", hint=TIME_HINT, required=True),
                           Ask("Until", "--until", hint=TIME_HINT))),
))
SWEEP_THRESHOLDS = Pick("Which thresholds?", (
    ("The range from the settings profile", (), ()),
    ("A list of my own", (), (Ask("Thresholds", "--thresholds", hint="e.g. 0.5,0.7,0.9", required=True),)),
    ("A custom range", (), (Ask("From", "--start", "fraction"), Ask("To", "--end", "fraction"),
                            Ask("Step", "--step", "float"))),
))

MENU = [
    Action("Experiment", "Run once", "one mutation loop", "run", lambda args: run_command(args), confirm=False),
    Action("Experiment", "Run a batch", "unattended runs, then the report", "run", lambda args: batch_command(args),
           steps=(Ask("Number of runs", "--runs", "int", hint="default: 15, or as many as the time budget allows"),
                  Ask("Time budget for the whole batch, in minutes", "--max-minutes", "float",
                      hint="Enter = none; no new run starts after it"),
                  Ask("Time limit per run, in seconds", "--timeout", "int", hint="default: 900 = 15 min")),
           more=(Ask("Number of steps per run (the loop length)", "--steps", "int",
                     hint="Enter = num_steps in run_config.json"),
                 Ask("Only these schemas", "--datasets", "multi", hint="Enter = all", options=_batch_schemas),
                 Ask("Action-weight profiles, one per round (in the order given)", "--profiles", "multi",
                     hint="Enter = the one in run_config.json", options=_batch_profiles),
                 Ask("Build the report afterwards", NO_REPORT_FLAG, "no"),
                 Ask("Archive the recorded traces first", "--archive-first", "yes"),
                 Ask("Show the complete output of every run (default: only invariant failures and progress)",
                     "--verbose", "yes")),
           more_hint="steps per run, schemas, profiles, report, archive, full output", implied_flag=RUNS_FLAG),
    Action("Experiment", "Replay", "re-execute a recorded run", "replay",
           lambda args: _launch(COMMANDS["replay"][1], args), steps=(REPLAY_WHAT,)),
    Action("Analysis", "Report", "the distinct problems found across runs", "report",
           lambda args: _launch(COMMANDS["report"][1], args),
           steps=(Ask("Folder or history.json to read", None, hint="default: all of runs"),
                  REPORT_PERIOD),
           more=(Ask("Save the report as", "--html", hint="default: tests/output/reports/issues/issues_<time>.html"),
                 Ask("Open the report in the browser", "--no-open", "no")),
           more_hint="output file, browser"),
    Action("Analysis", "Infer invariants", "mine rules from the recorded traces", "infer",
           lambda args: _launch(COMMANDS["infer"][1], args),
           steps=(DATASETS, Ask("Promotion threshold", "--threshold", "fraction", hint="0 to 1")),
           more=(PROFILE, MIN_OBS, TRACES,
                 Ask("Save the Markdown report as", "--report"),
                 Ask("Print the raw per-dataset details", "--verbose", "yes")),
           more_hint="profile, minimum observations, folders, output files"),
    Action("Analysis", "Review rules", "review the rules in the browser and apply them (session), or import a downloaded review (import)", "review",
           lambda args: _launch(COMMANDS["review"][1], args),
           steps=(Ask("What to do", None, "choice", choices=("session", "import"),
                     hint="Enter = session",
                     default="session", omit="session"),),
           confirm=False),
    Action("Analysis", "Archive traces", "move the recorded traces aside before a fresh batch", "archive",
           lambda args: _launch(COMMANDS["archive"][1], args),
           steps=(Ask("Label for the archive", "--label", hint="optional, e.g. before-sampler-fix"),
                  Ask("Remove only unfinished runs (no history.json) instead", "--drop-incomplete", "yes"),
                  Ask("Only show what would move", "--dry-run", "yes"))),
    Action("Analysis", "List invariants", "every invariant that is loaded", LIST_COMMAND,
           lambda args: list_invariants_command(args), steps=(), confirm=False),
    Action("Stats", "Rule activity", "how often each inferred rule was checked; which are dead weight", "stats rules",
           lambda args: _launch(COMMANDS["stats"][1], ["rules", *args]), more=(Ask("Print as CSV on the terminal instead of the report page", "--csv", "yes"),), more_hint="CSV output", confirm=False),
    Action("Stats", "Bug detection", "steps to the first known bug, bugs per run, which invariants flag them", "stats bugs",
           lambda args: _launch(COMMANDS["stats"][1], ["bugs", *args]),
           more=(Ask("Print as CSV on the terminal instead of the report page", "--csv", "yes"),), more_hint="CSV output", confirm=False),
    Action("Stats", "Invariant usage", "which invariants are never checked or never fire", "stats usage",
           lambda args: _launch(COMMANDS["stats"][1], ["usage", *args]),
           more=(Ask("Print as CSV on the terminal instead of the report page", "--csv", "yes"),), more_hint="CSV output", confirm=False),
    Action("Stats", "Template yield", "per template: candidates, exported, reviewed, accepted, trivial, rejected", "stats yield",
           lambda args: _launch(COMMANDS["stats"][1], ["yield", *args]),
           more=(Ask("candidates.json to use", "--candidates", hint="Enter = newest run"),
                 Ask("Folder with the review files", "--review-dir", hint="Enter = rule_reviews")),
           more_hint="candidates file, review folder", confirm=False),
    Action("Stats", "Threshold sweep", "how the threshold changes the mined rules", "stats sweep",
           lambda args: _launch(COMMANDS["stats"][1], ["sweep", *args]),
           steps=(DATASETS, SWEEP_THRESHOLDS),
           more=(PROFILE, MIN_OBS, TRACES, Ask("Save the table as", "--csv", hint="a .csv file"),
                 Ask("Save the Markdown report as", "--report"), Ask("Save the figure as", "--figure")),
           more_hint="profile, minimum observations, folders, output files"),
    Action("Stats", "Corpus report", "HTML overview of every run in the traces", "stats corpus",
           lambda args: _launch(COMMANDS["stats"][1], ["corpus", *args]), steps=(), confirm=False),
    Action("Analysis", "Known bugs", "one page: all bugs and their runs, add or remove a bug (Update button)",
           "bugs", lambda args: _launch(COMMANDS["bugs"][1], args), steps=(), confirm=False),
]

RULE = "-" * 64


class _QuitMenu(Exception):
    """Raised when the user types q at any question."""


class _Back(Exception):
    """Raised when the user types b at any question: back to the menu."""


def _read(ask, prompt: str) -> str:
    answer = ask(prompt).strip()
    if answer.lower() in ("b", "back"):
        raise _Back
    if answer.lower() in ("q", "quit"):
        raise _QuitMenu
    return answer


def _problem(q: Ask, value: str) -> str | None:
    """What is wrong with `value` for this question, or None if it is fine."""
    if q.kind == "int" and not (value.isdigit() and int(value) > 0):
        return "a positive whole number"
    if q.kind in ("float", "fraction"):
        try:
            number = float(value)
        except ValueError:
            return "a number"
        if q.kind == "fraction" and not 0 <= number <= 1:
            return "a number from 0 to 1"
    if q.kind == "choice" and value not in q.choices:
        return f"one of {', '.join(q.choices)}"
    return None


def _ask_listed(q: Ask, names: list[str], ask) -> str | None:
    """A numbered list; returns the chosen names joined by commas, or None if Enter was pressed."""
    many = q.kind == "multi"
    print(f"\n  {q.label}")
    for number, name in enumerate(names, start=1):
        print(f"    {number}  {name}")
    example = "numbers separated by commas, e.g. 1,3" if many else "a number"
    while True:
        answer = _read(ask, f"  Choose {example}" + (f" [{q.hint}]" if q.hint else "") + ": ")
        if not answer:
            return None
        tokens = [t for t in answer.replace(" ", ",").split(",") if t]
        if tokens and (many or len(tokens) == 1) and all(t.isdigit() and 1 <= int(t) <= len(names) for t in tokens):
            return ",".join(names[int(t) - 1] for t in tokens)
        print(f"  Please enter {example}, each from 1 to {len(names)}.", file=sys.stderr)


def _ask_one(q: Ask, ask) -> str | None:
    """The answer to one question, asked again until valid. None means skipped (or 'no' to a yes/no question)."""
    if q.kind in ("pick", "multi") and q.options is not None:
        names = q.options()
        if names:
            return _ask_listed(q, names, ask)
    if q.kind in ("yes", "no"):
        shown = "[y/N]" if q.kind == "yes" else "[Y/n]"
        while True:
            answer = _read(ask, f"  {q.label}? {shown}: ").lower()
            if answer in ("", "y", "yes", "n", "no"):
                break
            print("  Please answer y or n.", file=sys.stderr)
        flag_wanted = answer in ("y", "yes") if q.kind == "yes" else answer in ("n", "no")
        return "on" if flag_wanted else None
    suffix = (f" ({' / '.join(q.choices)})" if q.kind == "choice" else "") \
        + (" (required)" if q.required else "") + (f" [{q.hint}]" if q.hint else "")
    while True:
        value = _read(ask, f"  {q.label}{suffix}: ").strip("\"'")
        if not value:
            if not q.required:
                return q.default
            print("  This one is needed.", file=sys.stderr)
            continue
        problem = _problem(q, value)
        if problem is None:
            return value
        print(f"  Please enter {problem}.", file=sys.stderr)


def _choose(pick: Pick, ask) -> tuple[list[str], tuple[object, ...]]:
    """Show the alternatives; returns the fixed arguments and the follow-up questions of the chosen one."""
    count = len(pick.options)
    print(f"\n  {pick.label}")
    for number, (label, _fixed, _steps) in enumerate(pick.options, start=1):
        print(f"    {number}  {label}")
    while True:
        answer = _read(ask, f"  Choose 1-{count} (Enter = 1): ")
        if not answer:
            number = 1
        elif answer.isdigit() and 1 <= int(answer) <= count:
            number = int(answer)
        else:
            print(f"  Please enter a number from 1 to {count}.", file=sys.stderr)
            continue
        _label, fixed, steps = pick.options[number - 1]
        return list(fixed), steps


def _collect(steps: tuple[object, ...], ask, positional: list[str], flagged: list[str]) -> None:
    for step in steps:
        if isinstance(step, Check):
            flagged.extend(step.run(positional, flagged, ask))
            continue
        if isinstance(step, Pick):
            fixed, follow_up = _choose(step, ask)
            flagged.extend(fixed)
            _collect(follow_up, ask, positional, flagged)
            continue
        value = _ask_one(step, ask)
        if value is None or value == step.omit:
            continue
        if step.flag is None:
            positional.append(value)
        elif step.kind in ("yes", "no"):
            flagged.append(step.flag)
        else:
            flagged += [step.flag, value]


def _typed(command: str, args: list[str], implied_flag: str | None) -> str:
    """The command line that does the same as the menu choices. Arguments with spaces are quoted."""
    shown = list(args)
    if implied_flag and implied_flag not in args and MAX_MINUTES_FLAG not in args:
        shown = [implied_flag, "<default>", *shown]
    quoted = [f'"{a}"' if " " in a else a for a in shown]
    return " ".join(["python -m tests.cli", command, *quoted])


def _converse(action: Action, ask) -> list[str]:
    """Ask what this action needs and return its arguments. Raises _Back / _QuitMenu."""
    print(f"\n{action.label}   (Enter = default, b = back to the menu, q = quit)")
    positional: list[str] = []
    flagged: list[str] = []
    _collect(action.steps, ask, positional, flagged)
    if action.more and _read(ask, f"\n  More options ({action.more_hint})? [y/N]: ").lower() in ("y", "yes"):
        _collect(action.more, ask, positional, flagged)
    args = positional + flagged
    if action.confirm:
        print(f"\n  {_typed(action.command, args, action.implied_flag)}")
        while _read(ask, "  Press Enter to run, b to go back: ") not in ("", "y"):
            pass
    return args


SUBMENUS = {   # menu groups opened from one line of the top menu: group -> what is in it
    "Analysis": "report, infer invariants, review rules, archive traces, list invariants, known bugs",
    "Stats": "rule activity, bug detection, invariant usage, template yield, threshold sweep, corpus",
}
_BACK = object()


def _entries() -> list:
    """The top menu: the actions that are not in a submenu, and one line per submenu (its group name), in menu order."""
    entries, seen = [], set()
    for action in MENU:
        if action.group not in SUBMENUS:
            entries.append(action)
        elif action.group not in seen:
            seen.add(action.group)
            entries.append(action.group)
    return entries


def _select(entries: list, title: str, ask, back: bool = False):
    """Show a numbered menu. Returns (entry, options typed after the number), _BACK (submenu: b or Enter), or None (quit)."""
    labels = [e.label if isinstance(e, Action) else e + "  >" for e in entries]
    width = max(len(label) for label in labels)
    while True:
        print(f"\n{title}\n" + RULE)
        for number, (entry, label) in enumerate(zip(entries, labels), start=1):
            summary = entry.summary if isinstance(entry, Action) else SUBMENUS[entry]
            print(f"    {number}  {label:<{width}}   {summary}")
        print(("\n    b  Back" if back else "") + f"\n    q  Quit\n{RULE}")
        tokens = _split_args(ask(f"Choose [1-{len(entries)}, {'b, ' if back else ''}q]: "))
        choice, typed = (tokens[0].lower() if tokens else ""), tokens[1:]
        if back and choice in ("", "b"):
            return _BACK
        if choice in ("", "q"):
            return None
        if not (choice.isdigit() and 1 <= int(choice) <= len(entries)):
            print(f"Unknown choice '{choice}'.", file=sys.stderr)
            continue
        return entries[int(choice) - 1], typed


def interactive_menu(ask=input) -> int:
    entries = _entries()
    while True:
        picked = _select(entries, "MetaConfigurator mutation testing", ask)
        if picked is None:
            return EXIT_OK
        action, typed = picked
        if isinstance(action, str):     # a submenu: choose one of its actions
            picked = _select([a for a in MENU if a.group == action], action, ask, back=True)
            if picked is None:
                return EXIT_OK
            if picked is _BACK:
                continue
            action, typed = picked
        echo = True
        if typed or not (action.steps or action.more):
            args = typed                # options typed after the number, e.g. `4 --last-hours 12`
        else:
            try:
                args = _converse(action, ask)
            except _Back:
                continue
            except _QuitMenu:
                return EXIT_OK
            echo = not action.confirm   # a confirmed action has just shown its command line
        _remember_command(_typed(action.command, args, action.implied_flag).replace("<default>", RUNS_DEFAULT))
        if echo:
            print(f"\nRunning: {_typed(action.command, args, action.implied_flag)}\n")
        else:
            print()
        return action.run(args)


def _dispatch(argv: list[str], ask) -> int:
    if not argv:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return interactive_menu(ask)
        print(usage())
        return EXIT_OK
    command, rest = argv[0], argv[1:]
    if command in HELP_FLAGS:
        if rest and rest[0] in COMMAND_HELP:   # python -m tests.cli help bugs
            return command_help(rest[0], rest[1:])
        print(usage())
        return EXIT_OK
    if (command in COMMAND_HELP) and any(a in OPTION_HELP_FLAGS for a in rest):
        return command_help(command, rest)
    if command == LIST_COMMAND:
        return list_invariants_command(rest)
    if command == "run" or command in COMMANDS:
        _remember_command(_typed(command, rest, None))
    if command == "run":
        return run_command(rest)
    if command in COMMANDS:
        return _launch(COMMANDS[command][1], rest)
    print(f"Unknown command '{command}'.\n\n{usage()}", file=sys.stderr)
    return EXIT_USAGE


def main(argv: list[str] | None = None, ask=input) -> int:
    try:
        return _dispatch(sys.argv[1:] if argv is None else argv, ask)
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted.")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())