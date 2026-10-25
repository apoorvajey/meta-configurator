"""python -m tests.mutation_testing.stats [report] [options]   (python -m tests.cli stats ... starts this)
Without a report, lists the reports. A report runs exactly as `python -m <its module> <options>`."""
import runpy
import sys
import time

from tests.mutation_testing.stats import REPORTS


def usage() -> str:
    width = max(len(name) for name in REPORTS)
    lines = ["usage: python -m tests.cli stats <report> [options]", "", "reports:"]
    lines += [f"  {name:<{width}}  {desc}" for name, (desc, _, _) in REPORTS.items()]
    lines += ["", "Options of one report: python -m tests.cli stats <report> --help"]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    name, rest = argv[0], argv[1:]
    if name not in REPORTS:
        print(f"Unknown report '{name}'.\n\n{usage()}", file=sys.stderr)
        return 2
    _, module, preset = REPORTS[name]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    sys.argv = [module, *[a.replace("{stamp}", stamp) for a in preset], *rest]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))