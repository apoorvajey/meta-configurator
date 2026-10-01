"""Delta-debugging (ddmin) reduction of a mutation-loop history.json to the smallest subsequence that
still reproduces a named invariant failure. Based on Zeller and Hildebrandt's ddmin, with three
outcomes per candidate and unit-level instead of character-level granularity.

Reduction works on atomic units from build_atomic_units(), built from each repair step's recorded
target_index and role. A repair block is kept or removed whole, because splitting it changes the
undo stack its clicks act on.

Each candidate is classified:
- FAIL: the target invariant reproduced, no missing-target errors
- PASS: it did not reproduce, no missing-target errors
- INVALID: a missing-target error occurred, so the replay diverged and neither result can be trusted

The schema is resolved once per reduce_history() call, using the same rule as replay_runner.
--invariant is matched by prefix against each failure's full name ("INV-D6" matches
"INV-D6 GUI validation icons match violations").

Limitations:
- Undo/redo clicks can silently act differently if a removed unit changed the undo stack. No
  exception is raised, so this gives a false PASS or FAIL instead of INVALID. Replay any reduced
  result that contains an undo/redo unit (e.g. with --interactive) before relying on it.
- Some Text View executor methods do nothing instead of raising when their target is gone, which
  the INVALID check also cannot see.
- Missing-target detection is a substring heuristic on Playwright error messages.

    python -m tests.mutation_testing.test_case_reducer --history history.json --invariant INV-D6 --out reduced_history.json
    python -m tests.mutation_testing.test_case_reducer --history history.json --invariant INV-D6 --schema "Strenda Schema" --out reduced_history.json

The second form is for histories without schema/data meta.
"""

from __future__ import annotations

import json
import argparse
from dataclasses import dataclass
from enum import Enum

from playwright.sync_api import sync_playwright

from tests.shared.python.utils import setup_test_panel_via_localstorage, dismiss_dialog_if_present
from tests.mutation_testing.replay_runner import (
    ReplayRunner,
    ReplayOutcome,
    setup_app_for_schema,
    resolve_schema_for_replay,
    SCHEMA_CHOICES,
)


# Outcome

class Outcome(Enum):
    FAIL = "fail"
    PASS = "pass"
    INVALID = "invalid"


_UNDO_REDO_TESTIDS = ("undo", "redo", "schema_undo", "schema_redo")


# Atomic units

@dataclass
class AtomicUnit:
    start: int  # inclusive index into the exported "steps" list
    end: int  # inclusive
    contains_repair: bool
    contains_undo_redo: bool

    @property
    def indices(self) -> list[int]:
        return list(range(self.start, self.end + 1))

    def __repr__(self) -> str:
        span = f"{self.start}" if self.start == self.end else f"{self.start}-{self.end}"
        flags = []
        if self.contains_repair:
            flags.append("repair")
        if self.contains_undo_redo:
            flags.append("undo/redo")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        return f"<unit {span}{flag_str}>"


def build_atomic_units(steps: list[dict]) -> list[AtomicUnit]:
    """Group exported step records into units that ddmin treats as indivisible.

    Each repair step (action_id starting with "repair:") anchors a span back to:
    - its recorded target_index, for a walk-back repair that can cover many earlier actions, or
    - the nearest preceding non-repair step, when target_index is None (single-undo-or-revert, tied to
      one action, or a file exported before target_index existed).

    Overlapping or adjacent spans are merged, and every index outside a span becomes its own unit.
    The undo/redo risk in the module docstring still applies.
    """
    n = len(steps)
    spans: list[tuple[int, int]] = []

    for j, step in enumerate(steps):
        action_id = step.get("action_id", "")
        if not action_id.startswith("repair:"):
            continue
        target_index = step.get("target_index")
        if target_index is not None:
            anchor_start = target_index
        else:
            anchor_start = j
            k = j - 1
            while k >= 0 and steps[k].get("action_id", "").startswith("repair:"):
                k -= 1
            if k >= 0:
                anchor_start = k
        spans.append((anchor_start, j))

    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    units: list[AtomicUnit] = []
    covered = set()
    for start, end in merged:
        contains_repair = any(steps[i].get("action_id", "").startswith("repair:")
                               for i in range(start, end + 1))
        contains_undo_redo = any(steps[i].get("testid") in _UNDO_REDO_TESTIDS
                                  for i in range(start, end + 1))
        units.append(AtomicUnit(start, end, contains_repair, contains_undo_redo))
        covered.update(range(start, end + 1))

    for i in range(n):
        if i not in covered:
            units.append(AtomicUnit(i, i, False, steps[i].get("testid") in _UNDO_REDO_TESTIDS))

    units.sort(key=lambda u: u.start)
    return units


# Candidate execution

def _run_candidate(full_steps: list[dict], schema_option_name: str, data_file_name: str,
                    keep_indices: list[int], target_invariant_name: str,
                    headless: bool = True, verbose: bool = False) -> tuple[Outcome, ReplayOutcome]:
    """Launch a fresh browser, load the already-resolved schema/data pair, replay `keep_indices`
    (ascending) and classify the result.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page(no_viewport=True)

        setup_test_panel_via_localstorage(page)
        page.wait_for_timeout(1500)
        dismiss_dialog_if_present(page)
        setup_app_for_schema(page, schema_option_name, data_file_name)
        page.evaluate("document.body.style.zoom = '0.75'")

        runner = ReplayRunner(page, full_steps, interactive=False)
        outcome = runner.run(
            include=sorted(keep_indices),
            pause_on_failure=False,
            pause_at_end=False,
            export_log=False,
            verbose=verbose,
            target_invariant_name=target_invariant_name,
        )
        browser.close()

    if outcome.invalid:
        return Outcome.INVALID, outcome
    if outcome.reproduced:
        return Outcome.FAIL, outcome
    return Outcome.PASS, outcome


# ddmin

def ddmin(units: list[AtomicUnit], test_fn, max_iterations: int = 500) -> list[AtomicUnit]:
    """ddmin with a three-outcome test. INVALID is treated like PASS when deciding whether to accept a
    reduction, but stays visible through test_fn's logging.

    At each granularity n, try removing each of n chunks (does the complement still FAIL?), then try
    keeping only each chunk. Either success restarts the loop at the new candidate. Otherwise n doubles
    up to len(candidate). When single-unit removal no longer helps, `candidate` is 1-minimal at unit
    level.
    """
    candidate = list(units)
    n = 2
    iterations = 0

    while len(candidate) >= 2 and iterations < max_iterations:
        chunk_size = max(1, len(candidate) // n)
        chunks = [candidate[i:i + chunk_size] for i in range(0, len(candidate), chunk_size)]
        reduced = False

        # remove each chunk (does the complement still fail?)
        for chunk in chunks:
            iterations += 1
            complement = [u for u in candidate if u not in chunk]
            if not complement:
                continue
            if test_fn(complement) == Outcome.FAIL:
                candidate = complement
                n = max(n - 1, 2)
                reduced = True
                break

        if reduced:
            continue

        # keep only each chunk
        for chunk in chunks:
            if len(chunk) >= len(candidate):
                continue
            iterations += 1
            if test_fn(chunk) == Outcome.FAIL:
                candidate = chunk
                n = 2
                reduced = True
                break

        if reduced:
            continue

        if n >= len(candidate):
            break
        n = min(n * 2, len(candidate))

    if iterations >= max_iterations:
        print(f"ddmin hit its {max_iterations}-iteration safety cap - "
              f"result below may not be fully minimal")

    return candidate


# Driver

def reduce_history(history_path: str, target_invariant_name: str, out_path: str,
                    cli_schema: str | None = None, headless: bool = True) -> None:
    with open(history_path, encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        full_steps, meta = raw, {}
    else:
        full_steps = raw.get("steps", [])
        meta = raw.get("meta", {}) or {}

    try:
        schema_option_name, data_file_name = resolve_schema_for_replay(meta, cli_schema)
    except ValueError as e:
        print(f"Error: {e}")
        return

    print(f"Loaded {len(full_steps)} steps from {history_path}")
    print(f"   Schema/data pair: {schema_option_name} / {data_file_name}")
    print(f"   Target invariant: {target_invariant_name}")

    all_indices = list(range(len(full_steps)))

    print("\n=== Sanity check: does the FULL sequence reproduce the target failure? ===")
    outcome, replay_outcome = _run_candidate(full_steps, schema_option_name, data_file_name,
                                              all_indices, target_invariant_name, headless=headless)
    if outcome != Outcome.FAIL:
        extra = f", invalid_reason: {replay_outcome.invalid_reason}" if replay_outcome.invalid else ""
        print(f"Error: Full sequence did not reproduce '{target_invariant_name}' "
              f"(outcome: {outcome.value}{extra}). Aborting - nothing to reduce.")
        return
    print(f"✓ Confirmed - reproduces at replay step {replay_outcome.reproduced_at_step}")

    units = build_atomic_units(full_steps)
    print(f"\nBuilt {len(units)} atomic unit(s) from {len(full_steps)} steps")
    for u in units:
        if u.end > u.start or u.contains_repair or u.contains_undo_redo:
            print(f"   {u!r}")

    invalid_count = 0

    def test_fn(kept_units: list[AtomicUnit]) -> Outcome:
        nonlocal invalid_count
        keep_indices = [i for u in kept_units for i in u.indices]
        outcome, replay_outcome = _run_candidate(full_steps, schema_option_name, data_file_name,
                                                  keep_indices, target_invariant_name, headless=headless)
        if outcome == Outcome.INVALID:
            invalid_count += 1
        note = f" - {replay_outcome.invalid_reason}" if outcome == Outcome.INVALID else ""
        print(f"   [{outcome.value}] {len(keep_indices)} step(s) kept{note}")
        return outcome

    print("\n=== Running ddmin ===")
    minimized = ddmin(units, test_fn)

    kept_indices = sorted(i for u in minimized for i in u.indices)
    print(f"\n✓ Reduced from {len(full_steps)} to {len(kept_indices)} step(s) "
          f"({len(minimized)} atomic unit(s))")
    if invalid_count:
        print(f"   ({invalid_count} candidate(s) during search were INVALID - "
              f"discarded, not counted as either FAIL or PASS)")

    flagged = [u for u in minimized if u.contains_undo_redo]
    if flagged:
        print(f"\n{len(flagged)} unit(s) in the final result touch undo/redo: "
              f"{[repr(u) for u in flagged]}")
        print("   These can't be fully verified by this tool alone (see module "
              "docstring) - recommend replaying the reduced result with "
              "--interactive and confirming by eye before treating it as "
              "the authoritative minimal repro.")

    reduced_steps = [full_steps[i] for i in kept_indices]
    reduced_payload = {
        "meta": {"schema_option_name": schema_option_name, "data_file": data_file_name},
        "steps": reduced_steps,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(reduced_payload, f, indent=2)
    print(f"\nReduced history → {out_path}")
    print(f"   Replay with: python -m tests.mutation_testing.replay_runner --history {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Delta-debug a mutation loop history to a minimal repro")
    parser.add_argument("--history", required=True, help="Path to the full history.json to reduce")
    parser.add_argument("--invariant", required=True,
                         help="Invariant name/prefix to preserve, e.g. INV-D6 (matched as a "
                              "prefix against each failure's full name)")
    parser.add_argument("--out", default="reduced_history.json", help="Output path for the reduced history")
    parser.add_argument("--schema", choices=SCHEMA_CHOICES, default=None,
                         help="Schema/data pair to use. Required if the history file predates "
                              "schema/data metadata. Overrides recorded metadata if both present.")
    parser.add_argument("--headed", action="store_true",
                         help="Show the browser during reduction (default: headless, faster)")
    args = parser.parse_args()
    reduce_history(args.history, args.invariant, args.out,
                    cli_schema=args.schema, headless=not args.headed)


if __name__ == "__main__":
    main()