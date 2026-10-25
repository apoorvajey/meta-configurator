"""Is this failure isolated, or part of a bigger issue?

The issue report calls attach_seen() for every problem, so the answer is already in the report. This CLI is the same
lookup for scripts:   python -m tests.mutation_testing.reports.seen_elsewhere P-1a2b3c [--fuzzy] [--csv]

The problem id is the one shown in the report under "Technical details". Grouping is not re-implemented:
fingerprint() and classify() come from issue_report, so "seen" means "would be in the same issue".
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tests.mutation_testing.reports.issue_report import _log_uri, classify, fingerprint, loop_step, loop_steps, uid_of
from tests.mutation_testing.reports.problem_labels import hides, load_labels
from tests.mutation_testing.reports.triage.episodes import assign_tier, build_episodes
from tests.mutation_testing.reports.triage.screenshots import shots_for_step
from tests.mutation_testing.reports.triage.run_loading import DEFAULT_CONFIG_PATH, find_log_files, load_config, load_run
from tests.mutation_testing.runs.run_layout import RUNS_DIR, iter_run_dirs


@dataclass
class Hit:
    step: int        # first failing step (1-based)
    log: str = ""    # run log opened at that step ("" if the run has no log)
    shot: str = ""   # screenshot of that step ("" if there is none)


@dataclass
class RunPrint:
    run_id: str
    path: Path
    total_steps: int
    fps: dict = field(default_factory=dict)     # fingerprint -> Hit
    rules: dict = field(default_factory=dict)   # invariant id -> Hit


def _hit(ep, cfg: dict) -> Hit:
    try:
        shots = shots_for_step(ep.run, ep.start, cfg)   # this step only
    except Exception:
        shots = []
    return Hit(loop_step(ep.run, ep.start), _log_uri(ep, cfg) or "", shots[0].resolve().as_uri() if shots else "")


def _keep_first(hits: dict, key, hit: Hit) -> None:
    if key not in hits or hit.step < hits[key].step:
        hits[key] = hit


def scan(cfg: dict, skip_ids: frozenset | set = frozenset(), root: Path = RUNS_DIR) -> tuple[list[RunPrint], int]:
    """Fingerprints of the failures of every readable run under root, except skip_ids.
    Returns (runs with at least one failure, number of runs looked at including clean ones)."""
    out, total = [], 0
    labels = load_labels()   # irrelevant problems, and resolved tool problems in the runs before the fix, do not count
    for run_dir in iter_run_dirs(root):
        for f in find_log_files(run_dir):          # the run's effective history, as the report reads it
            try:
                run = load_run(f, cfg)
            except (OSError, ValueError) as e:     # unreadable history.json: skip
                print(f"[!] Skipping {f}: {e}", file=sys.stderr)
                continue
            if run.run_id in skip_ids:
                continue
            total += 1
            if (f.parent / "RUN_CLEAN.json").is_file():
                continue
            rp = RunPrint(run.run_id, f, loop_steps(run))
            for ep in build_episodes(run, cfg):
                assign_tier(ep, cfg)
                kind, rules = classify(ep, cfg)
                if kind != "issue":
                    continue
                fp = fingerprint(ep, rules, cfg)
                if hides(labels.get(uid_of(fp)), run.started):
                    continue
                hit = _hit(ep, cfg)
                _keep_first(rp.fps, fp, hit)
                for r in rules:
                    _keep_first(rp.rules, r, hit)
            if rp.fps:
                out.append(rp)
    return out, total


def lookup(key: tuple, rule_ids: set, inv: str, records: list[RunPrint]) -> tuple[list, list]:
    """(same fingerprint, same invariant but other fingerprint); rows are (RunPrint, Hit, invariant id).
    Runs with a screenshot of the failing step come first, then the earliest failures."""
    exact, near = [], []
    for rp in records:
        if key in rp.fps:
            exact.append((rp, rp.fps[key], inv))
        else:
            hits = sorted(r for r in rp.rules if r in rule_ids)
            if hits:
                near.append((rp, rp.rules[hits[0]], hits[0]))
    order = lambda row: (not row[1].shot, row[1].step, row[0].total_steps, row[0].run_id)
    return sorted(exact, key=order), sorted(near, key=order)


def attach_seen(issues: list, batch_ids: set, cfg: dict, primary_of) -> None:
    """Set issue.seen = {"total", "exact", "near"} for every issue;
    rows are (run_id, first step, total steps, invariant, log link, screenshot link)."""
    records, total = scan(cfg, batch_ids)
    for iss in issues:
        exact, near = lookup(iss.key, set(iss.rules), primary_of(iss), records)
        flat = lambda rows: [(rp.run_id, h.step, rp.total_steps, inv, h.log, h.shot) for rp, h, inv in rows]
        iss.seen = {"total": total, "exact": flat(exact), "near": flat(near)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find other runs with the same failure as a problem in the issue report.")
    ap.add_argument("problem", help="problem id from the report (Technical details), e.g. P-1a2b3c")
    ap.add_argument("--fuzzy", action="store_true", help="also list runs failing the same invariant with another fingerprint")
    ap.add_argument("--csv", action="store_true", help="CSV output")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = ap.parse_args(argv)

    records, total = scan(load_config(args.config))
    key = next((fp for rp in records for fp in rp.fps if uid_of(fp) == args.problem), None)
    if key is None:
        print(f"{args.problem} not found in any of the {total} run(s)", file=sys.stderr)
        return 1
    inv = key[0][0]
    exact, near = lookup(key, set(key[0]), inv, records)

    rows = [("exact", *r) for r in exact] + ([("fuzzy", *r) for r in near] if args.fuzzy else [])
    if args.csv:
        w = csv.writer(sys.stdout)
        w.writerow(["section", "run", "first_step", "total_steps", "invariant", "screenshot"])
        for sec, rp, h, i in rows:
            w.writerow([sec, rp.path, h.step, rp.total_steps, i, "yes" if h.shot else "no"])
    else:
        line = lambda rp, h, i: f"{rp.path} | first at step {h.step} | {rp.total_steps} steps | {i}" + (" | screenshot" if h.shot else "")
        for rp, h, i in exact:
            print(line(rp, h, i))
        if args.fuzzy:
            print("\nSame invariant, different fingerprint:")
            for rp, h, i in near:
                print(line(rp, h, i))
    print(f"\nseen in {len(exact)} of {total} runs", file=sys.stderr if args.csv else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())