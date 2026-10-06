"""One entry point for all mutation-testing tools. Run from the repository root.

    python -m tests.cli                  menu: pick an action, then optionally its options from a second menu
    python -m tests.cli run              one mutation loop run
    python -m tests.cli run --runs 24    24 unattended runs, then the report of exactly those runs
    python -m tests.cli report           the distinct problems found across all runs
    python -m tests.cli replay           replay the latest run (--history <file> for another one)
    python -m tests.cli infer            mine invariants from the recorded traces
    python -m tests.cli sweep            how the promotion threshold changes the mined invariants
    python -m tests.cli invariants       list every invariant that is loaded

Everything after a command is passed unchanged to the tool behind it, so
`python -m tests.cli report --last-hours 12` is the same as `python -m tests.mutation_testing.issue_report
--last-hours 12`, and `python -m tests.cli replay --help` shows that tool's own options.
Exceptions: `run --runs N` starts the unattended batch runner instead of a single run and builds the report
afterwards (`--no-report` skips it), and `invariants` runs inside this script.
To add a tool: add one line to COMMANDS.
"""
from __future__ import annotations

import importlib.util
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_TOOL = 3
EXIT_INTERRUPTED = 130

# command -> (description, candidate modules; the first one that exists is used)
COMMANDS: dict[str, tuple[str, list[str]]] = {
    "run": ("Run the mutation loop once (--runs N: N unattended runs, then the report)",
            ["tests.mutation_testing.mutation_loop"]),
    "report": ("Show the distinct problems found across all runs",
               ["tests.mutation_testing.issue_report"]),
    "replay": ("Replay the latest run (--history <history.json> for another one)",
               ["tests.mutation_testing.replay_runner"]),
    "infer": ("Mine invariants from the recorded traces",
              ["tests.mutation_testing.invariant_inference_engine"]),
    "sweep": ("Show how the promotion threshold changes the mined invariants",
              ["tests.mutation_testing.threshold_sweep"]),
}
BATCH_MODULES = ["tests.mutation_testing.overnight_runner"]
LIST_COMMAND = "invariants"
LIST_DESCRIPTION = "List every invariant that is loaded (--mode data|schema|temporal)"
HELP_FLAGS = ("-h", "--help", "help")

RUNS_FLAG = "--runs"
NO_REPORT_FLAG = "--no-report"
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
    return any(a == RUNS_FLAG or a.startswith(RUNS_FLAG + "=") for a in args)


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


def _first_sentence(doc: str | None) -> str:
    if not doc:
        return ""
    paragraph = " ".join(doc.strip().split("\n\n")[0].split())
    return paragraph.split(". ")[0].rstrip(".")


def list_invariants_command(args: list[str]) -> int:
    modes = ("data", "schema", "temporal")
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


# ---------------------------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------------------------

def _split_args(text: str) -> list[str]:
    """Split typed options like a shell, but keep backslashes (Windows paths) and drop surrounding quotes."""
    return [token.strip("\"'") for token in shlex.split(text, posix=False)]


@dataclass(frozen=True)
class Opt:
    """One option offered in the second menu. flag=None means a positional argument."""
    label: str
    flag: str | None
    kind: str = "text"          # "flag" (on/off), "text", "int", "float" or "choice"
    hint: str = ""
    choices: tuple[str, ...] = ()


TIME_HINT = "(YYYY-MM-DD HH:MM)"
BATCH_DEFAULT_HINT = "(Enter = the batch runner's default)"

BATCH_OPTIONS = (
    Opt("Number of runs", "--runs", "int", BATCH_DEFAULT_HINT),
    Opt("Time limit per run in seconds", "--timeout", "int", BATCH_DEFAULT_HINT),
    Opt("Only these schemas", "--datasets", hint="(comma-separated, e.g. Strenda Schema,EnzymeML Schema)"),
    Opt("Shuffle the schema order in each round", "--shuffle-laps", "flag"),
    Opt("Do not build the report afterwards", NO_REPORT_FLAG, "flag"),
)
REPORT_OPTIONS = (
    Opt("Folder or history.json to read", None, hint="(Enter = all runs in mutation_history)"),
    Opt("Only runs from the last N hours", "--last-hours", "float"),
    Opt("Only runs since", "--since", hint=TIME_HINT),
    Opt("Only runs until", "--until", hint=TIME_HINT),
    Opt("Write the report to this file", "--html"),
    Opt("Do not open the report in the browser", "--no-open", "flag"),
)
REPLAY_OPTIONS = (
    Opt("Replay this history file instead of the latest run", "--history"),
    Opt("Replay every run since", "--since", hint=TIME_HINT),
    Opt("Replay every run in this folder", "--folder"),
    Opt("Run quietly up to step N, then step through by hand", "--until", "int"),
    Opt("Step through by hand from the start", "--interactive", "flag"),
    Opt("Pause when a step fails", "--pause-on-failure", "flag"),
    Opt("Also write an HTML log", "--html", "flag"),
    Opt("Without a visible browser window", "--headless", "flag"),
)
INFER_OPTIONS = (
    Opt("Folder with the recorded traces", "--snapshots-dir"),
    Opt("Only these datasets", "--datasets", hint="(comma-separated)"),
    Opt("Minimum observations per dataset", "--min-obs", "int"),
    Opt("Promotion threshold", "--threshold", "float", "(0 to 1)"),
    Opt("Settings profile", "--profile", hint="(a profile name from inference_config.json)"),
    Opt("Write the dashboard to this file", "--dashboard"),
    Opt("Write the Markdown report to this file", "--report"),
    Opt("Print the raw per-dataset details", "--verbose", "flag"),
)
INVARIANT_OPTIONS = (
    Opt("Only one kind of invariant", "--mode", "choice", choices=("data", "schema", "temporal")),
)

# (label, command name shown in the echo, options of the second menu, action(options typed or chosen),
#  flag the typed command needs but the menu may leave out: shown as <default> in the echo)
MENU = [
    ("Run the mutation loop once", "run", (), lambda args: run_command(args), None),
    ("Run an unattended batch, then show the report", "run", BATCH_OPTIONS, lambda args: batch_command(args),
     RUNS_FLAG),
    ("Show the problem report for all runs", "report", REPORT_OPTIONS,
     lambda args: _launch(COMMANDS["report"][1], args), None),
    ("Replay the latest run", "replay", REPLAY_OPTIONS, lambda args: _launch(COMMANDS["replay"][1], args), None),
    ("Mine invariants from the recorded traces", "infer", INFER_OPTIONS,
     lambda args: _launch(COMMANDS["infer"][1], args), None),
    ("List the invariants", LIST_COMMAND, INVARIANT_OPTIONS, lambda args: list_invariants_command(args), None),
]


def _problem(opt: Opt, value: str) -> str | None:
    """What is wrong with `value` for this option, or None if it is fine."""
    if opt.kind == "int" and not (value.isdigit() and int(value) > 0):
        return "a positive whole number"
    if opt.kind == "float":
        try:
            float(value)
        except ValueError:
            return "a number"
    if opt.kind == "choice" and value not in opt.choices:
        return f"one of {', '.join(opt.choices)}"
    return None


def _read_value(opt: Opt, ask, preset: str | None = None) -> list[str]:
    """The arguments for one option. `preset` is a value typed together with the option number. Asks (again)
    until the value is valid; an empty answer skips the option."""
    value = preset
    while True:
        if value is None:
            if opt.kind == "choice":
                prompt = f"{opt.label} ({'/'.join(opt.choices)}): "
            else:
                prompt = f"{opt.label} {opt.hint}: " if opt.hint else f"{opt.label}: "
            value = ask(prompt)
        value = value.strip().strip("\"'")
        if not value:
            return []
        problem = _problem(opt, value)
        if problem is None:
            return [opt.flag, value] if opt.flag else [value]
        print(f"Please enter {problem}.", file=sys.stderr)
        value = None


def _parse_pick(answer: str, count: int) -> tuple[int, str | None] | None:
    """'2' -> option 2. Anything typed after the number is taken as its value ('2 12'), so a user who types the
    value right away is not rejected. None if the number is not an option."""
    number, _, value = answer.partition(" ")
    if not (number.isdigit() and 1 <= int(number) <= count):
        return None
    return int(number), value.strip() or None


def _typed(command: str, args: list[str], implied_flag: str | None) -> str:
    """The command line that does the same as the menu choices. Arguments with spaces are quoted."""
    shown = list(args)
    if implied_flag and implied_flag not in args:
        shown = [implied_flag, "<default>", *shown]
    quoted = [f'"{a}"' if " " in a else a for a in shown]
    return " ".join(["python -m tests.cli", command, *quoted])


def _choose_options(options: tuple[Opt, ...], ask) -> list[str]:
    """Second menu: one option number, then its value if it needs one. The action starts right after."""
    print()
    for number, opt in enumerate(options, start=1):
        print(f"  {number:>2}  {opt.label}")
    print("\nType an option number, or press Enter to run with the defaults.")
    while True:
        answer = ask("Option: ").strip()
        if not answer:
            return []
        pick = _parse_pick(answer, len(options))
        if pick is not None:
            break
        print(f"'{answer}' is not an option number (1-{len(options)}).", file=sys.stderr)
    number, preset = pick
    opt = options[number - 1]
    return [opt.flag] if opt.kind == "flag" else _read_value(opt, ask, preset)


def interactive_menu(ask=input) -> int:
    print("MetaConfigurator mutation testing\n")
    for number, (label, *_rest) in enumerate(MENU, start=1):
        print(f"  {number}  {label}")
    print("  q  Quit\n")
    print("After the number, options can be typed directly, e.g. `3 --last-hours 12`.\n")
    tokens = _split_args(ask("Choose: "))
    choice, typed = (tokens[0].lower() if tokens else ""), tokens[1:]
    if choice in ("", "q"):
        return EXIT_OK
    if not (choice.isdigit() and 1 <= int(choice) <= len(MENU)):
        print(f"Unknown choice '{choice}'.", file=sys.stderr)
        return EXIT_USAGE
    _label, command, options, action, implied_flag = MENU[int(choice) - 1]
    args = typed if typed or not options else _choose_options(options, ask)
    print(f"\nSame as typing: {_typed(command, args, implied_flag)}\n")
    return action(args)


# ---------------------------------------------------------------------------------------------

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