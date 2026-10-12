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
}
BATCH_MODULES = ["tests.mutation_testing.loop.batch_runner"]
LIST_COMMAND = "invariants"
LIST_DESCRIPTION = "List every invariant that is loaded (--mode data|schema|any|temporal)"
HELP_FLAGS = ("-h", "--help", "help")

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
    lines += ["", "Options after the command go to that tool: python -m tests.cli <command> --help"]
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
    return _launch(COMMANDS["report"][1], ["--since", started])


def batch_command(args: list[str]) -> int:
    return batch_and_report([a for a in args if a != NO_REPORT_FLAG], report=NO_REPORT_FLAG not in args)


def run_command(args: list[str]) -> int:
    return batch_command(args) if _is_batch(args) else _launch(COMMANDS["run"][1], args)


def _remember_command(command_line: str) -> None:
    """Hand the command line to the tools started below (environment variable); the report shows it."""
    os.environ[ORIGIN_ENV] = command_line


def _first_sentence(doc: str | None) -> str:
    if not doc:
        return ""
    paragraph = " ".join(doc.strip().split("\n\n")[0].split())
    return paragraph.split(". ")[0].rstrip(".")


def list_invariants_command(args: list[str]) -> int:
    modes = ("data", "schema", "any", "temporal")
    mode = None
    if args:
        if len(args) == 2 and args[0] == "--mode" and args[1] in modes:
            mode = args[1]
        else:
            print(f"usage: python -m tests.cli invariants [--mode {'|'.join(modes)}]", file=sys.stderr)
            return EXIT_USAGE
    from tests.invariants.invariants_core import list_invariants
    from tests.invariants.temporal_invariants import list_temporal_invariants
    # (id, mode, one-line description, plugin file)
    rows = [(p.id, p.mode, _first_sentence(p.check.__doc__), f"state/{p.module}.py") for p in list_invariants()]
    rows += [(p.id, "temporal", _first_sentence(p.check.__doc__).removeprefix(f"{p.id}: "), f"temporal/{p.module}.py")
             for p in list_temporal_invariants()]
    rows = [r for r in rows if mode in (None, r[1])]
    for inv_id, row_mode, text, path in rows:
        print(f"{inv_id:<16} {row_mode:<9} {text}")
        print(f"{'':<16} {'':<9} {path}")
    print(f"\n{len(rows)} invariant(s) loaded from tests/invariants/state/ and tests/invariants/temporal/")
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


@dataclass(frozen=True)
class Pick:
    """Alternatives that cannot be combined: (label, fixed arguments, follow-up questions). The first is the default."""
    label: str
    options: tuple[tuple[str, tuple[str, ...], tuple[object, ...]], ...]


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
# What to do with a run that has no failures left (replay_runner --clean-runs). Enter = the tool's own default:
# ask for one run, skip for several.
CLEAN_RUNS = Ask("If the run is already clean", "--clean-runs", "choice", hint="Enter = ask (one run) or skip (several)",
                 choices=("ask", "skip", "include"))
# Several runs are replayed without waiting for input, so the step-through modes do not apply to them.
REPLAY_WHAT = Pick("What should be replayed?", (
    ("The latest run", (), (REPLAY_HOW, CLEAN_RUNS)),
    ("One specific run", (), (Ask("Run folder or history.json", "--history",
                                  hint="path to a run folder or to its history.json", required=True),
                              REPLAY_HOW, CLEAN_RUNS)),
    ("Every run since a time", (), (Ask("Since", "--since", hint=TIME_HINT + ", or an age like 48h", required=True),
                                    Ask("Only from this folder", "--folder"), CLEAN_RUNS, HEADLESS)),
    ("Every run in a folder", (), (Ask("Folder", "--folder", required=True),
                                   Ask("Only runs since", "--since", hint=TIME_HINT + ", or an age like 48h"),
                                   CLEAN_RUNS, HEADLESS)),
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
           more=(Ask("Only these schemas", "--datasets", "multi", hint="Enter = all", options=_batch_schemas),
                 Ask("Action-weight profiles, one per round (in the order given)", "--profiles", "multi",
                     hint="Enter = the one in run_config.json", options=_batch_profiles),
                 Ask("Build the report afterwards", NO_REPORT_FLAG, "no"),
                 Ask("Archive the recorded traces first", "--archive-first", "yes"),
                 Ask("Show the complete output of every run (default: only invariant failures and progress)",
                     "--verbose", "yes")),
           more_hint="schemas, profiles, report, archive, full output", implied_flag=RUNS_FLAG),
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
    Action("Analysis", "Threshold sweep", "how the threshold changes the mined rules", "sweep",
           lambda args: _launch(COMMANDS["sweep"][1], args),
           steps=(DATASETS, SWEEP_THRESHOLDS),
           more=(PROFILE, MIN_OBS, TRACES, Ask("Save the table as", "--csv", hint="a .csv file"),
                 Ask("Save the Markdown report as", "--report"), Ask("Save the figure as", "--figure")),
           more_hint="profile, minimum observations, folders, output files"),
    Action("Analysis", "Corpus report", "HTML overview of every run in the traces", "corpus",
           lambda args: _launch(COMMANDS["corpus"][1], args), steps=(), confirm=False),
    Action("Analysis", "Review rules", "open the HTML review page (html), import the downloaded review (import), or use a CSV sheet (export / import-sheet)", "review",
           lambda args: _launch(COMMANDS["review"][1], args),
           steps=(Ask("What to do", None, "choice", hint="Enter = html", choices=("html", "import", "export", "import-sheet")),),
           confirm=False),
    Action("Analysis", "Archive traces", "move the recorded traces aside before a fresh batch", "archive",
           lambda args: _launch(COMMANDS["archive"][1], args),
           steps=(Ask("Label for the archive", "--label", hint="optional, e.g. before-sampler-fix"),
                  Ask("Remove only unfinished runs (no history.json) instead", "--drop-incomplete", "yes"),
                  Ask("Only show what would move", "--dry-run", "yes"))),
    Action("Analysis", "List invariants", "every invariant that is loaded", LIST_COMMAND,
           lambda args: list_invariants_command(args),
           steps=(Ask("Kind of invariant", "--mode", "choice", hint="Enter = all", choices=("data", "schema", "any", "temporal")),),
           confirm=False),
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
                return None
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
        if isinstance(step, Pick):
            fixed, follow_up = _choose(step, ask)
            flagged.extend(fixed)
            _collect(follow_up, ask, positional, flagged)
            continue
        value = _ask_one(step, ask)
        if value is None:
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


def interactive_menu(ask=input) -> int:
    width = max(len(a.label) for a in MENU)
    while True:
        print("\nMetaConfigurator mutation testing\n" + RULE)
        group = None
        for number, action in enumerate(MENU, start=1):
            if action.group != group:
                group = action.group
                print(f"\n  {group}")
            print(f"    {number}  {action.label:<{width}}   {action.summary}")
        print(f"\n    q  Quit\n{RULE}")
        tokens = _split_args(ask(f"Choose [1-{len(MENU)}, q]: "))
        choice, typed = (tokens[0].lower() if tokens else ""), tokens[1:]
        if choice in ("", "q"):
            return EXIT_OK
        if not (choice.isdigit() and 1 <= int(choice) <= len(MENU)):
            print(f"Unknown choice '{choice}'.", file=sys.stderr)
            continue
        action = MENU[int(choice) - 1]
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
        print(usage())
        return EXIT_OK
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