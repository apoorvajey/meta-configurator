"""
test_case_reducer.py
=====================
Located at: tests/mutation_testing/replay/test_case_reducer.py

Delta-debugging (ddmin) reduction of a mutation-loop history.json to
the smallest subsequence that still reproduces a named invariant
failure. Adapted from Zeller & Hildebrandt's ddmin — see "Simplifying
and Isolating Failure-Inducing Input" for the original algorithm; the
version here is close but not a literal transcription (three-outcome
handling, unit-level rather than character-level granularity).

Reduction operates on ATOMIC UNITS, not raw steps, built by
build_atomic_units() from each repair step's recorded target_index /
role (see replay_runner.export_history's docstring). A repair block
can only be removed or kept whole — never split — because splitting it
would change how many things are on the undo stack when its undo/redo
clicks replay, silently changing what they actually do.

Each ddmin candidate is classified FAIL / PASS / INVALID:
  - FAIL:    target invariant reproduced, no missing-target errors
  - PASS:    target invariant did not reproduce, no missing-target errors
  - INVALID: a missing-target-looking error occurred — the replay
             diverged from what the candidate was meant to test, so the
             result (reproduced or not) isn't trustworthy either way

SCHEMA RESOLUTION: same rule as replay_runner.py — a history file's
recorded meta wins, else --schema is required, else this refuses to
guess (see resolve_schema_for_replay's docstring in replay_runner.py).
Resolved ONCE per reduce_history() call, not re-derived per candidate.

INVARIANT MATCHING: --invariant is the invariant id as shown in the report (e.g. "INV-S1", "TEMP-COLLAPSE-02"); it
must equal the part of a failure's name before the first space.

SPEED: the history is first cut at the first step where the target fails (later steps cannot cause it), short tails
are tried before ddmin, results are cached, one browser serves all candidates, screenshots/snapshots are off, and the
state checks evaluate only the target invariant (see CandidateRunner). The final result is re-checked with every
invariant on; --check-all disables the target-only shortcut for the whole search.

KNOWN LIMITATIONS (see build_atomic_units / _looks_like_missing_target
docstrings for detail — flagged here rather than solved):
  - Undo/redo clicks can silently do something different than intended
    if an earlier removed unit changed the live undo stack, with NO
    exception raised — this can produce a false PASS/FAIL rather than
    a detected INVALID. Any reduced result containing an undo/redo
    unit should be manually spot-checked (e.g. replay it with
    --interactive) before being treated as authoritative.
  - Several Text View executor methods no-op instead of raising when
    their target no longer resolves — also invisible to the INVALID
    classifier.
  - Missing-target detection is a substring heuristic on Playwright
    error messages, not a guaranteed classification.

BY BUG: after a bug is logged in config/triage_config.json, --bug BUG-03 finds the run for you: every recorded run
(tests/output/runs/*/history.json) is read with the same matcher the loop uses, the runs where the bug occurs are
listed, and the one where it first appears earliest is reduced (--history <run> limits the search to one run). The
invariant that is reduced is the one that triggered the bug in that run.

ADMISSION (with --bug): the reduced run is saved to tests/regression/<bug id>/history.json (e.g. bug02), an entry for it
is added to tests/regression/manifest.json (an existing entry for the bug is updated), and the entry is replayed
--repeat times (default 5) with the regression runner. It stays in the manifest only if all of them pass; otherwise
it is removed again and the reduced file is kept for inspection. --no-admit only reduces; --no-check skips the replays.

ALL KNOWN BUGS: --list shows where each logged bug stands (already reduced, ready, or why it cannot be reduced yet:
too few recorded runs, runs that cannot be replayed exactly). --all reduces and admits every bug that is ready and not
reduced yet, one after the other (--only BUG-07,BUG-09 limits it to those; --redo does the reduced ones again;
--min-runs N asks for more logs; bugs shown only by a mined INF- rule are skipped unless --include-inferred). One
failing bug does not stop the rest; the table at the end shows what happened to each.

Usage:
    python -m tests.mutation_testing.replay.test_case_reducer --bug BUG-03
    python -m tests.mutation_testing.replay.test_case_reducer --list
    python -m tests.mutation_testing.replay.test_case_reducer --all
    python -m tests.mutation_testing.replay.test_case_reducer --only BUG-07,BUG-09

    python -m tests.mutation_testing.replay.test_case_reducer \
        --history history.json --invariant INV-D6 --out reduced_history.json

    # History file predates schema/data meta — must say which pair:
    python -m tests.mutation_testing.replay.test_case_reducer \
        --history history.json --invariant INV-D6 --schema "Strenda Schema" \
        --out reduced_history.json
"""

from __future__ import annotations

import contextlib
import io
import json
import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from playwright.sync_api import sync_playwright

from tests.shared.python.utils import LIVE_BASE_URL, SessionMode
from tests.invariants.invariants_core import INVARIANT_REGISTRY, InvariantChecker, set_invariant_filter
from tests.mutation_testing.discovery.values import set_valid_probability
from tests.mutation_testing.loop.app_setup import VIEWPORT, _dataset_slug, prepare_app_page
from tests.mutation_testing.replay.replay_runner import ReplayRunner
from tests.mutation_testing.runs.reproducibility import ReplayOutcome
from tests.mutation_testing.runs.history_io import load_history, resolve_schema_for_replay, SCHEMA_CHOICES
from tests.mutation_testing.runs.run_layout import effective_history, resolve_history_path, iter_run_dirs
from tests.mutation_testing.runs.known_bugs import KnownBugMatcher, load_known_bugs, observe_history_step


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------

class Outcome(Enum):
    FAIL = "fail"
    PASS = "pass"
    INVALID = "invalid"


_UNDO_REDO_TESTIDS = ("undo", "redo", "schema_undo", "schema_redo")


# ---------------------------------------------------------------------------
# Atomic units
# ---------------------------------------------------------------------------

@dataclass
class AtomicUnit:
    start: int  # inclusive index into the exported "steps" list
    end: int    # inclusive
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
    """
    Groups exported step records into atomic units ddmin must treat as
    indivisible.

    Every repair step (action_id starting with "repair:") anchors a
    span back to either:
      - its recorded target_index — a walk-back repair (see
        mutation_loop.py's _repair_policy_walk_back_to_known_good),
        which can cover many earlier actions, or
      - the nearest preceding NON-repair step, when target_index is
        None — the single-undo-or-revert policy, which is always tied
        to just the one action that triggered it (see
        _repair_policy_single_undo_or_revert), OR a file exported
        before target_index existed at all (old files fall back to
        this same approximation — see this module's top-level
        docstring on undo/redo residual risk).

    Overlapping/adjacent spans are merged (interval merge) so a run of
    back-to-back repair events collapses into one unit. Every index not
    covered by any span becomes its own singleton unit.

    See this module's top-level docstring for the undo/redo residual
    risk this does NOT close.
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


# ---------------------------------------------------------------------------
# Candidate execution
# ---------------------------------------------------------------------------

class CandidateRunner:
    """Replays candidate step subsets in fresh browser contexts that share ONE browser.

    Speed-ups over launching a browser per candidate and checking everything at every step:
      - one Chromium for the whole reduction (a new context per candidate keeps candidates isolated);
      - no screenshots, state snapshots or HTML logs, and console output is swallowed;
      - only the target invariant is evaluated by the state checks (set_invariant_filter). Temporal and inferred
        rules are cheap (they read snapshots) and stay on. Disable with fast=False (or full_checks=True per call);
      - the replay stops at the first step where the target fails.
    The baseline check before step 1 always runs unfiltered, as in replay_one, because some checks hover over
    elements and so change the page.
    """

    def __init__(self, pw, full_steps: list[dict], meta: dict, schema_option_name: str, data_file_name: str,
                 target: str, headless: bool = True, fast: bool = True, bug_id: str | None = None):
        self.browser = pw.chromium.launch(headless=headless)
        self.full_steps = full_steps
        self.schema_option_name = schema_option_name
        self.data_file_name = data_file_name
        self.target = target
        self.bug_id = bug_id   # when set, the target only counts if the matcher assigns this bug to it
        self.fast = fast
        resolved = meta.get("resolved_settings") or {}
        self.run_id = resolved.get("run_id")
        self.mirror_scroll = resolved.get("screenshots_enabled", True)
        self.dataset = _dataset_slug(schema_option_name, data_file_name)
        # A state invariant is filtered by id; anything else (TEMP-*, INF-*) is evaluated from snapshots
        state_ids = {p.id for p in INVARIANT_REGISTRY.values()}
        self.state_filter = {target} if target in state_ids else set()

    def close(self) -> None:
        self.browser.close()

    def run(self, keep_indices: list[int], full_checks: bool = False) -> tuple[Outcome, ReplayOutcome | None]:
        context = self.browser.new_context(viewport=VIEWPORT)
        try:
            page = context.new_page()
            with contextlib.redirect_stdout(io.StringIO()):
                prepare_app_page(page, self.schema_option_name, self.data_file_name)
                set_invariant_filter(None)
                InvariantChecker(page, SessionMode.DataEditor).run_all()          # baseline, as in replay_one
                if self.fast and not full_checks:
                    set_invariant_filter(self.state_filter)
                runner = ReplayRunner(page, self.full_steps, interactive=False, dataset=self.dataset,
                                      run_id=self.run_id, mirror_screenshot_scroll=self.mirror_scroll)
                outcome = runner.run(
                    include=sorted(keep_indices), pause_on_failure=False, pause_at_end=False,
                    export_log=False, verbose=False, analyze_failures=False, capture_screenshots=False,
                    save_state_snapshots=False, progress_every=0,
                    target_invariant_name=self.target, stop_on_reproduction=True,
                    target_bug_id=self.bug_id)
        except Exception as e:   # the app could not even be set up: nothing can be concluded
            print(f"   candidate could not run: {type(e).__name__}: {e}")
            return Outcome.INVALID, None
        finally:
            set_invariant_filter(None)
            context.close()

        if outcome.invalid:
            return Outcome.INVALID, outcome
        if outcome.reproduced:
            return Outcome.FAIL, outcome
        return Outcome.PASS, outcome


# ---------------------------------------------------------------------------
# ddmin
# ---------------------------------------------------------------------------

def ddmin(units: list[AtomicUnit], test_fn, max_iterations: int = 500) -> list[AtomicUnit]:
    """
    ddmin adapted for a three-outcome test. INVALID is treated exactly
    like PASS for deciding whether to accept a reduction — an
    inconclusive result never counts as "safe to keep" — but stays
    visible to the caller via test_fn's own logging rather than
    silently collapsing into "reduction didn't help".

    At each granularity n: try removing each of n complementary chunks
    (does the complement still FAIL?), then try keeping only each chunk
    in isolation (does that chunk alone still FAIL?). Either success
    restarts the loop at the new candidate. If neither succeeds, n
    doubles (finer chunks) up to n == len(candidate); once even
    single-unit removal doesn't reduce further, `candidate` is
    1-minimal with respect to unit-level removal.
    """
    candidate = list(units)
    n = 2
    iterations = 0

    while len(candidate) >= 2 and iterations < max_iterations:
        chunk_size = max(1, len(candidate) // n)
        chunks = [candidate[i:i + chunk_size] for i in range(0, len(candidate), chunk_size)]
        reduced = False

        # Try removing each chunk (does the complement still fail?)
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

        # Try keeping only each chunk in isolation
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
        print(f"⚠️  ddmin hit its {max_iterations}-iteration safety cap — "
              f"result below may not be fully minimal")

    return candidate


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def reduce_history(history_path: str, target_invariant_name: str, out_path: str,
                    cli_schema: str | None = None, headless: bool = True,
                    fast: bool = True, tail_first: bool = True, bug_id: str | None = None) -> bool:
    """Reduce a history; True if a reduced history was written."""
    resolved_path = resolve_history_path(Path(history_path))   # a run folder or a history.json, as in replay
    if resolved_path is None:
        print(f"❌ History file not found: {history_path}")
        return False
    history_path = str(resolved_path)
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
        print(f"❌ {e}")
        return False

    resolved = meta.get("resolved_settings") or {}
    set_valid_probability(gui=resolved.get("valid_value_probability", 0.5),
                          text_view=resolved.get("text_view_valid_value_probability", 0.8))

    print(f"📂 Loaded {len(full_steps)} steps from {history_path}")
    print(f"   Schema/data pair: {schema_option_name} / {data_file_name}")
    print(f"   Target invariant: {target_invariant_name}"
          f"   (fast mode: {'on' if fast else 'off'}, tail-first: {'on' if tail_first else 'off'})")

    with sync_playwright() as pw:
        runner = CandidateRunner(pw, full_steps, meta, schema_option_name, data_file_name,
                                 target_invariant_name, headless=headless, fast=fast, bug_id=bug_id)
        try:
            return _reduce(runner, full_steps, meta, schema_option_name, data_file_name,
                           target_invariant_name, out_path, tail_first, bug_id)
        finally:
            runner.close()


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def _bar(best: int, total: int, width: int = 20) -> str:
    """How far the search has shrunk the sequence so far (units of the smallest failing candidate vs the start)."""
    done = round(width * (1 - best / total)) if total else 0
    return "[" + "#" * done + "-" * (width - done) + "]"


def _reduce(runner: CandidateRunner, full_steps: list[dict], meta: dict, schema_option_name: str,
            data_file_name: str, target: str, out_path: str, tail_first: bool,
            bug_id: str | None = None) -> bool:
    cache: dict[tuple[int, ...], Outcome] = {}
    runs = 0
    t0 = time.time()

    def run_indices(indices: list[int], full_checks: bool = False) -> tuple[Outcome, ReplayOutcome | None]:
        nonlocal runs
        key = tuple(sorted(indices))
        if not full_checks and key in cache:
            return cache[key], None
        runs += 1
        outcome, replay_outcome = runner.run(list(key), full_checks=full_checks)
        if not full_checks:
            cache[key] = outcome
        return outcome, replay_outcome

    print(f"App under test: {LIVE_BASE_URL}")   # the page loads are silenced during the search, so say it once here
    print("\n=== Sanity check: does the FULL sequence reproduce the target failure? ===")
    outcome, replay_outcome = run_indices(list(range(len(full_steps))))
    if outcome != Outcome.FAIL:
        extra = (f", invalid_reason: {replay_outcome.invalid_reason}"
                 if replay_outcome is not None and replay_outcome.invalid else "")
        print(f"❌ Full sequence did not reproduce '{target}' (outcome: {outcome.value}{extra}). "
              f"Aborting — nothing to reduce.")
        return False
    first_fail = replay_outcome.reproduced_at_step   # 1-based
    print(f"✓ Confirmed — first reproduced at step {first_fail} of {len(full_steps)}")

    # Steps after the first failure cannot have caused it: cut them off before reducing anything
    original_total = len(full_steps)
    del full_steps[first_fail:]            # in place: the runner holds the same list
    cache[tuple(range(len(full_steps)))] = Outcome.FAIL
    print(f"✂️  Cut to the first {len(full_steps)} step(s) (was {original_total})")

    units = build_atomic_units(full_steps)
    print(f"\n📦 Built {len(units)} atomic unit(s) from {len(full_steps)} steps")
    for u in units:
        if u.end > u.start or u.contains_repair or u.contains_undo_redo:
            print(f"   {u!r}")

    invalid_count = 0
    best_units = len(units)   # size of the smallest candidate that still failed so far

    def test_fn(kept_units: list[AtomicUnit]) -> Outcome:
        nonlocal invalid_count, best_units
        keep_indices = [i for u in kept_units for i in u.indices]
        outcome, replay_outcome = run_indices(keep_indices)
        if outcome == Outcome.INVALID:
            invalid_count += 1
        note = f" — {replay_outcome.invalid_reason}" if outcome == Outcome.INVALID and replay_outcome else ""
        if outcome == Outcome.FAIL:
            best_units = min(best_units, len(kept_units))
        print(f"   {_bar(best_units, len(units))} {best_units}/{len(units)} unit(s) | replay {runs} | "
              f"{_clock(time.time() - t0)} | [{outcome.value}] {len(keep_indices)} step(s) kept{note}")
        return outcome

    candidate = units
    if tail_first and len(units) > 2:
        # Most bugs need only the last few actions before the failure: try short tails (1, 2, 4, ... units)
        print("\n=== Tail-first: shortest tail that still reproduces ===")
        w = 1
        while w < len(units):
            if test_fn(units[-w:]) == Outcome.FAIL:
                candidate = units[-w:]
                break
            w *= 2

    print(f"\n=== Running ddmin on {len(candidate)} unit(s) ===")
    minimized = ddmin(candidate, test_fn)

    kept_indices = sorted(i for u in minimized for i in u.indices)
    print(f"\n✓ Reduced from {original_total} to {len(kept_indices)} step(s) "
          f"({len(minimized)} atomic unit(s)), {runs} replay(s) run")
    print(f"⏱️  Search took {_clock(time.time() - t0)} (mm:ss)")
    if invalid_count:
        print(f"   ({invalid_count} candidate(s) during search were INVALID — "
              f"discarded, not counted as either FAIL or PASS)")

    # The search checked only the target invariant: confirm with every invariant on
    confirmed = True
    if runner.fast:
        verdict, _ = run_indices(kept_indices, full_checks=True)
        confirmed = verdict == Outcome.FAIL
        print("✓ Confirmed with ALL invariants on" if confirmed else
              f"⚠️  With all invariants on, the reduced sequence did NOT reproduce '{target}' "
              f"({verdict.value}) — a skipped check probably changes the page. Rerun with --check-all.")

    flagged = [u for u in minimized if u.contains_undo_redo]
    if flagged:
        print(f"\n⚠️  {len(flagged)} unit(s) in the final result touch undo/redo: "
              f"{[repr(u) for u in flagged]}")
        print("   These can't be fully verified by this tool alone (see module docstring) — "
              "replay the reduced result several times before treating it as the authoritative minimal repro.")

    reduced_steps = []
    for i in kept_indices:
        step = dict(full_steps[i])
        step.pop("state_hash", None)    # no longer the state of a full run: replay would report divergence at step 1
        step.pop("target_index", None)  # indexes of the old, longer history
        reduced_steps.append(step)

    reduced_meta = {k: v for k, v in meta.items() if k != "replay"}
    reduced_meta.update(schema_option_name=schema_option_name, data_file=data_file_name)
    reduced_meta["reduced"] = {"target_invariant": target, "source_steps": original_total,
                               "kept_steps": len(kept_indices), "confirmed_with_all_invariants": confirmed,
                               "replays": runs, "seconds": round(time.time() - t0),
                               "app_url": LIVE_BASE_URL}
    if bug_id:
        reduced_meta["reduced"]["bug"] = bug_id
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"meta": reduced_meta, "steps": reduced_steps}, f, indent=2)
    print(f"\n💾 Reduced history → {out_path}")
    print(f"   Replay with: python -m tests.mutation_testing.replay.replay_runner --history {out_path}")
    return True


# ---------------------------------------------------------------------------
# Finding the run of a logged bug
# ---------------------------------------------------------------------------

def bugs_in_steps(steps: list[dict], bugs: list[dict] | None = None) -> dict[str, tuple[str, int]]:
    """{bug id: (invariant id to reduce, 1-based step of the bug's first appearance)} for the logged bugs that occur in
    a recorded run. Uses the matcher of the loop on the recorded failures, so action kinds in the bug's definition are
    respected. A bug can fail several invariants at once; the one reduced is a state invariant if there is one (those
    are checked by every replay), else the first by id."""
    matcher = KnownBugMatcher(bugs)
    state_ids = {p.id for p in INVARIANT_REGISTRY.values()}
    found: dict[str, tuple[str, int]] = {}
    for n, step in enumerate(steps, start=1):
        known, _ = observe_history_step(matcher, step)
        for bug in set(known.values()) - set(found):
            invs = sorted((i for i, b in known.items() if b == bug), key=lambda i: (i not in state_ids, i))
            found[bug] = (invs[0], n)
    return found


def run_name(history: Path) -> str:
    """Folder name of the run a history belongs to (the history of a replay lies in <run>/replays/<replay>/)."""
    return history.parent.parent.parent.name if history.parent.parent.name == "replays" else history.parent.name


def find_runs_with_bug(bug_id: str) -> list[tuple[Path, str, int, int]]:
    """Recorded runs that contain the bug: (history.json, invariant id, first step, total steps), best candidate first
    (earliest first appearance, then fewest steps). Reads the effective history of a run, like the known bugs page:
    the newest complete replay if there is one, else the original."""
    bugs = load_known_bugs()
    hits = []
    for run_dir in iter_run_dirs():
        path = effective_history(run_dir)
        if path is None:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        steps = raw if isinstance(raw, list) else raw.get("steps", [])
        found = bugs_in_steps(steps, bugs).get(bug_id)
        if found:
            hits.append((path, found[0], found[1], len(steps)))
    return sorted(hits, key=lambda h: (h[2], h[3], run_name(h[0])))


def _pick_run(bug_id: str, history: str | None) -> tuple[str, str] | None:
    """(history path, invariant id) to reduce for a bug, or None (the reason is printed)."""
    if not any(b.get("id") == bug_id for b in load_known_bugs()):
        print(f"❌ '{bug_id}' is not in config/triage_config.json. Log the bug there first.")
        return None
    if history:
        resolved = resolve_history_path(Path(history))
        if resolved is None:
            print(f"❌ History file not found: {history}")
            return None
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        found = bugs_in_steps(raw if isinstance(raw, list) else raw.get("steps", [])).get(bug_id)
        if not found:
            print(f"❌ {bug_id} does not occur in {resolved}.")
            return None
        print(f"🔎 {bug_id} first appears in {resolved} at step {found[1]} (invariant {found[0]})")
        return str(resolved), found[0]
    hits = find_runs_with_bug(bug_id)
    if not hits:
        print(f"❌ No recorded run under the runs folder contains {bug_id}.")
        return None
    print(f"🔎 {bug_id} occurs in {len(hits)} recorded run(s) (earliest first appearance first):")
    for path, inv, step, total in hits[:10]:
        print(f"   {run_name(path)}: step {step} of {total} ({inv})")
    best = hits[0]
    print(f"   → using {run_name(best[0])}{' (replay)' if best[0].parent.parent.name == 'replays' else ''} (pass --history <run> to choose another)")
    return str(best[0]), best[1]


REGRESSION_DIR = Path(__file__).resolve().parents[2] / "regression"   # tests/regression


def default_output(bug_id: str) -> str:
    """Where the reduced run of a bug is kept: tests/regression/bug02/history.json for BUG-02."""
    return str(REGRESSION_DIR / bug_id.lower().replace("-", "").replace(" ", "_") / "history.json")


def admit(bug_id: str, out_path: str, repeat: int, check: bool) -> bool:
    """Put the reduced run into the manifest of the regression replays and, if check, replay it `repeat` times.
    The entry stays only if every replay gives the expected result. True if the bug is in the manifest afterwards."""
    manifest_path = REGRESSION_DIR / "manifest.json"
    try:
        history_rel = Path(out_path).resolve().relative_to(REGRESSION_DIR.resolve()).as_posix()
    except ValueError:
        print(f"ℹ️  {out_path} is outside {REGRESSION_DIR}: not added to the manifest.")
        return False
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file()
                else {"repeat": 1, "entries": []})
    entries = manifest.setdefault("entries", [])
    existing = next((e for e in entries if e.get("history") == history_rel or e.get("bugs") == [bug_id]), None)
    previous = dict(existing) if existing else None
    entry = existing or {"name": bug_id.lower().replace(" ", "-"), "history": history_rel,
                         "expect": "known-bugs", "bugs": [bug_id]}
    entry["history"] = history_rel
    if existing is None:
        entries.append(entry)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\n📋 {'Updated' if existing else 'Added'} manifest entry '{entry['name']}' ({manifest_path})")
    if not check:
        return True
    try:
        from tests.regression.run_regression import main as run_regression
    except ImportError:
        print("ℹ️  tests/regression/run_regression.py not found: entry added without the replays.")
        return True
    print(f"\n=== Admission check: {repeat} replay(s) of '{entry['name']}' ===")
    if run_regression(["--only", entry["name"], "--repeat", str(repeat)]) == 0:
        print(f"✅ '{entry['name']}' admitted: {repeat} of {repeat} replays as expected.")
        return True
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"] = [e for e in manifest["entries"] if e.get("name") != entry["name"]]
    if previous is not None:   # it was there before: put it back as it was
        manifest["entries"].append(previous)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"❌ '{entry['name']}' NOT admitted: the replays did not all give the expected result. "
          f"The reduced history is kept at {out_path} for inspection.")
    return False


# ---------------------------------------------------------------------------------------------------------------------
# All known bugs at once: --list shows where each one stands, --all (or --only) reduces the ones that are ready.
# ---------------------------------------------------------------------------------------------------------------------

def _reduced_bugs() -> dict[str, dict]:
    """{bug id: manifest entry} for the bugs that already have a reduced run in the regression manifest (the history
    file must exist too)."""
    manifest_path = REGRESSION_DIR / "manifest.json"
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8")).get("entries", [])
    except (OSError, ValueError):
        return {}
    done: dict[str, dict] = {}
    for entry in entries:
        if not (REGRESSION_DIR / entry.get("history", "")).is_file():
            continue
        for bug_id in entry.get("bugs", []):
            done[bug_id] = entry
    return done


def plan_bugs(only: list[str] | None = None, min_runs: int = 1, include_inferred: bool = False,
              cli_schema: str | None = None) -> list[dict]:
    """Where each known bug stands: [{bug, name, state, detail, history, invariant, runs}].

    state: 'reduced'   already in the regression manifest
           'unadmitted' a reduced history file exists in tests/regression/ but the manifest has no entry for it
           'ready'     a recorded run contains it, is exactly replayable (every action step has a recorded value and
                       seed) and names its schema/data pair; history/invariant say what would be reduced
           'no_logs'   fewer than min_runs recorded runs contain it
           'not_replayable' / 'no_schema'   runs contain it, but none can be replayed faithfully
           'inferred'  the bug is shown by a mined (INF-) rule; skipped unless include_inferred, because mined rules
                       are not guaranteed to reproduce in the reducer
    The run chosen is the first of find_runs_with_bug() (earliest appearance, then fewest steps) that can be replayed."""
    from tests.mutation_testing.runs.reproducibility import check_reproducibility
    bugs = [b for b in load_known_bugs() if b.get("id")]
    if only:
        wanted = {w.strip().upper() for w in only if w.strip()}
        bugs = [b for b in bugs if b["id"].upper() in wanted]
        missing = wanted - {b["id"].upper() for b in bugs}
        if missing:
            print(f"ℹ️  Not in the known bugs of config/triage_config.json: {', '.join(sorted(missing))}")
    reduced = _reduced_bugs()
    plan = []
    for bug in bugs:
        bug_id = bug["id"]
        row = {"bug": bug_id, "name": bug.get("name") or bug.get("summary", "")[:60], "state": "", "detail": "",
               "history": None, "invariant": None, "runs": 0}
        plan.append(row)
        if bug_id in reduced:
            row.update(state="reduced", detail=f"manifest entry '{reduced[bug_id].get('name')}'")
            continue
        leftover = Path(default_output(bug_id))
        if leftover.is_file():
            row.update(state="unadmitted", detail=f"{leftover.parent.name}/history.json exists but the manifest has no "
                                                  f"entry for it (admission failed, or --reduce-only): "
                                                  f"reduce {bug_id} --admit-only")
            continue
        hits = find_runs_with_bug(bug_id)
        row["runs"] = len(hits)
        if len(hits) < max(1, min_runs):
            row.update(state="no_logs", detail=f"{len(hits)} recorded run(s) contain it, {max(1, min_runs)} needed")
            continue
        reason = "no run could be replayed"
        state = "not_replayable"
        for path, invariant, first_step, total in hits:
            try:
                steps, meta = load_history(path)
            except (OSError, ValueError):
                continue
            repro = check_reproducibility(steps)
            if not repro.ok:
                reason, state = f"{path.parent.name}: {repro.reason}", "not_replayable"
                continue
            try:
                resolve_schema_for_replay(meta, cli_schema)
            except ValueError:
                reason, state = f"{path.parent.name}: no schema/data pair recorded (use --schema)", "no_schema"
                continue
            row.update(state="ready", history=str(path), invariant=invariant,
                       detail=f"{path.parent.name}, first at step {first_step} of {total} ({invariant})")
            break
        else:
            row.update(state=state, detail=reason)
        if row["state"] == "ready" and row["invariant"].startswith("INF-") and not include_inferred:
            row.update(state="inferred", detail=f"shown by the mined rule {row['invariant']}; use --include-inferred "
                                                f"to reduce it anyway ({row['detail']})")
    return plan


_STATE_MARK = {"reduced": "✅", "unadmitted": "⚠️ ", "ready": "▶ ", "no_logs": "⏸ ", "not_replayable": "⚠️ ", "no_schema": "⚠️ ",
               "inferred": "ℹ️ ", "admitted": "✅", "reduced_only": "⚠️ ", "failed": "❌", "skipped": "⏸ "}


def print_plan(plan: list[dict]) -> None:
    width = max([len(r["bug"]) for r in plan] + [3]) + 2
    print(f"\n   {'bug':<{width}}{'state':<16}details")
    for row in plan:
        print(f"{_STATE_MARK.get(row['state'], '  ')} {row['bug']:<{width}}{row['state']:<16}{row['name'][:38]}"
              f"{'  - ' + row['detail'] if row['detail'] else ''}")
    ready = sum(1 for r in plan if r["state"] == "ready")
    unadmitted = sum(1 for r in plan if r["state"] == "unadmitted")
    print(f"\n{len(plan)} known bug(s): {sum(1 for r in plan if r['state'] == 'reduced')} reduced, {ready} ready to reduce"
          + (f", {unadmitted} reduced but not in the manifest" if unadmitted else "") + ".")


def reduce_all(plan: list[dict], repeat: int, check: bool, admit_runs: bool, headless: bool, fast: bool,
               tail_first: bool, cli_schema: str | None, redo: bool) -> list[dict]:
    """Reduce (and, unless admit_runs is off, admit) every bug of the plan that is 'ready' (or already 'reduced' with
    redo). One bug failing does not stop the others; Ctrl+C ends the loop and still prints the table. Returns the plan
    rows with the outcome in 'state': admitted, reduced_only (reduced but not admitted), failed or skipped."""
    todo = [r for r in plan if r["state"] == "ready" or (redo and r["state"] == "reduced")]
    print(f"\n▶ Reducing {len(todo)} bug(s): {', '.join(r['bug'] for r in todo) or 'none'}")
    started = time.time()
    try:
        for n, row in enumerate(todo, start=1):
            bug_id = row["bug"]
            print(f"\n{'=' * 78}\n[{n}/{len(todo)}] {bug_id}  {row['name']}\n{'=' * 78}")
            if row["state"] == "reduced":   # --redo: pick the run again
                picked = _pick_run(bug_id, None)
                if picked is None:
                    row.update(state="failed", detail="no run to reduce")
                    continue
                row["history"], row["invariant"] = picked
            out = default_output(bug_id)
            try:
                written = reduce_history(row["history"], row["invariant"], out, cli_schema=cli_schema,
                                         headless=headless, fast=fast, tail_first=tail_first, bug_id=bug_id)
                if not written:
                    row.update(state="failed", detail="the reducer wrote no reduced run")
                elif not admit_runs:
                    row.update(state="reduced_only", detail=f"written to {out}, not admitted (--no-admit)")
                elif admit(bug_id, out, repeat, check=check):
                    row.update(state="admitted", detail=f"{out}")
                else:
                    row.update(state="reduced_only", detail=f"{out} kept, replays disagreed (not admitted)")
            except KeyboardInterrupt:
                raise
            except Exception as e:   # one bug must not stop the others
                print(f"❌ {bug_id}: {type(e).__name__}: {e}")
                row.update(state="failed", detail=f"{type(e).__name__}: {e}")
    except KeyboardInterrupt:
        print("\n⏹ Stopped by the user; the bugs after this one were not reduced.")
        for row in todo:
            if row["state"] in ("ready", "reduced"):
                row.update(state="skipped", detail="stopped before it was reduced")
    print(f"\n=== Finished in {_clock(time.time() - started)} ===")
    print_plan(plan)
    return plan


EPILOG = """\
Workflow:  the loop finds a failure -> log it as a bug (python -m tests.cli bugs) -> reduce it (this tool: cuts the
recorded run down to the fewest steps that still show the bug) -> admit it (add the reduced run to
tests/regression/manifest.json, but only if it replays 5 times with the expected result) -> the regression replays
(python -m tests.cli regression) run it on the hosted site and the local build.

Examples:
  reduce                          show where every known bug stands
  reduce BUG-12 --app local       reduce BUG-12 on the local build and admit it
  reduce BUG-12 BUG-13            several bugs, one after the other
  reduce BUG-12 --reduce-only     only write the reduced run, leave the manifest alone
  reduce BUG-13 --admit-only      the reduced run exists already: add it to the manifest and check it
  reduce --all --app local        every bug that is ready to reduce
  reduce BUG-12 --run tests/output/runs/20261025_205951    reduce from one particular run

Rarely needed: --invariant ID (with --run: reduce by invariant instead of by bug), --out FILE, --schema NAME,
--check-all, --no-tail-first, --no-check, --min-runs N, --include-inferred.
"""


def _app_url(app: str) -> str:
    from tests.shared.python.utils import HOSTED_URL, configured_local_url
    return {"hosted": HOSTED_URL, "local": configured_local_url()}.get(app, app)


def _switch_app(app: str) -> None:
    """Run this command again with METACONFIGURATOR_URL set, then exit with its result. The app a tool tests is read once,
    when tests/shared/python/utils.py is imported, so the choice must be in the environment before Python starts."""
    url = _app_url(app)
    if not url.startswith(("http://", "https://")):
        sys.exit(f"--app must be hosted, local or a URL starting with http:// or https://, not '{app}'")
    if os.environ.get("METACONFIGURATOR_URL") == url:
        return
    command = [sys.executable, "-m", __spec__.name] if __spec__ else [sys.executable, sys.argv[0]]
    sys.exit(subprocess.call([*command, *sys.argv[1:]], env={**os.environ, "METACONFIGURATOR_URL": url}))


def _reduce_one(bug_id: str, args, single: bool) -> str:
    """Reduce (and unless --reduce-only, admit) one bug. Returns 'admitted', 'reduced_only' or 'failed'."""
    if args.admit_only:
        out = default_output(bug_id)
        if not Path(out).is_file():
            print(f"❌ {bug_id}: no reduced run at {out}. Reduce it first (leave out --admit-only).")
            return "failed"
        return "admitted" if admit(bug_id, out, max(1, args.repeat), check=not args.no_check) else "reduced_only"
    picked = _pick_run(bug_id, args.history if single else None)
    if picked is None:
        return "failed"
    history, invariant = picked
    out = args.out if (args.out and single) else default_output(bug_id)
    written = reduce_history(history, invariant, out, cli_schema=args.schema, headless=not args.headed,
                             fast=not args.check_all, tail_first=not args.no_tail_first, bug_id=bug_id)
    if not written:
        return "failed"
    if args.reduce_only:
        return "reduced_only"
    return "admitted" if admit(bug_id, out, max(1, args.repeat), check=not args.no_check) else "reduced_only"


def main():
    parser = argparse.ArgumentParser(
        prog="reduce", description="Cut the recorded run of a known bug down to the fewest steps that still show it, "
                                   "and add it to the regression replays.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bugs", nargs="*", metavar="BUG",
                        help="bug ids to reduce, e.g. BUG-12 BUG-13. No ids: show where every known bug stands")
    parser.add_argument("--app", metavar="APP",
                        help="where to replay while reducing: hosted | local | a URL (default: run_config.json). "
                             "local needs the dev server running (cd meta_configurator, npm run dev)")
    parser.add_argument("--all", action="store_true", help="reduce every known bug that is ready (see the list)")
    parser.add_argument("--list", action="store_true", help="show where every known bug stands; reduces nothing")
    parser.add_argument("--run", "--history", dest="history", metavar="RUN",
                        help="reduce this run (a run folder or history.json) instead of searching all runs; one bug only")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reduce-only", dest="reduce_only", action="store_true",
                      help="only write the reduced run to tests/regression/<bug>/history.json; do not touch the manifest")
    mode.add_argument("--admit-only", dest="admit_only", action="store_true",
                      help="do not reduce: add the existing reduced run to the manifest and replay it to check it")
    parser.add_argument("--repeat", type=int, default=5, metavar="N",
                        help="replays that must all give the expected result before the entry is kept (default 5)")
    parser.add_argument("--redo", action="store_true", help="with --all: also reduce bugs that are already in the manifest")
    parser.add_argument("--headed", action="store_true", help="show the browser window (default: hidden, faster)")
    # Rarely needed or kept from earlier versions; described in the epilog, hidden from the option list
    hide = argparse.SUPPRESS
    parser.add_argument("--no-admit", dest="reduce_only", action="store_true", help=hide)    # old name of --reduce-only
    parser.add_argument("--bug", dest="bug_opts", action="append", default=[], help=hide)    # old: reduce --bug BUG-03
    parser.add_argument("--only", default=None, help=hide)                                    # old: --only BUG-07,BUG-09
    parser.add_argument("--invariant", default=None, help=hide)
    parser.add_argument("--out", default=None, help=hide)
    parser.add_argument("--no-check", action="store_true", help=hide)
    parser.add_argument("--schema", choices=SCHEMA_CHOICES, default=None, help=hide)
    parser.add_argument("--check-all", action="store_true", help=hide)
    parser.add_argument("--no-tail-first", action="store_true", help=hide)
    parser.add_argument("--min-runs", type=int, default=1, help=hide)
    parser.add_argument("--include-inferred", action="store_true", help=hide)
    args = parser.parse_args()

    bugs = [b.strip().upper() for b in [*args.bugs, *args.bug_opts, *(args.only or "").split(",")] if b.strip()]
    if args.history and len(bugs) > 1:
        parser.error("--run works for one bug at a time")
    if args.all and args.history:
        parser.error("--run cannot be combined with --all")

    if args.history and args.invariant and not bugs:   # by invariant instead of by bug: reduce only
        if args.app:
            _switch_app(args.app)
        reduce_history(args.history, args.invariant, args.out or "reduced_history.json", cli_schema=args.schema,
                       headless=not args.headed, fast=not args.check_all, tail_first=not args.no_tail_first)
        return

    if args.list or not (bugs or args.all):   # nothing to do but look
        print_plan(plan_bugs(only=bugs or None, min_runs=args.min_runs, include_inferred=args.include_inferred,
                             cli_schema=args.schema))
        if not args.list:
            print("\nNext: reduce <BUG> [--app local]   or   reduce --all   (reduce --help explains the rest)")
        return

    if args.app:
        _switch_app(args.app)

    if args.all:
        plan = plan_bugs(only=bugs or None, min_runs=args.min_runs, include_inferred=args.include_inferred,
                         cli_schema=args.schema)
        if args.admit_only:
            for row in [r for r in plan if r["state"] == "unadmitted"]:
                row["state"] = _reduce_one(row["bug"], args, single=False)
            print_plan(plan)
            return
        reduce_all(plan, repeat=max(1, args.repeat), check=not args.no_check, admit_runs=not args.reduce_only,
                   headless=not args.headed, fast=not args.check_all, tail_first=not args.no_tail_first,
                   cli_schema=args.schema, redo=args.redo)
        return

    results = {}
    try:
        for bug_id in bugs:
            if len(bugs) > 1:
                print(f"\n{'=' * 78}\n{bug_id}\n{'=' * 78}")
            results[bug_id] = _reduce_one(bug_id, args, single=len(bugs) == 1)
    except KeyboardInterrupt:
        print("\n⏹ Stopped by the user.")
    if len(bugs) > 1:
        print("\nResult:")
        for bug_id, state in results.items():
            print(f"  {_STATE_MARK.get(state, '  ')} {bug_id}: {state}")


if __name__ == "__main__":
    main()