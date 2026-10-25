"""Invariant inference engine: finds candidate invariants in recorded state snapshots with a small template library
(generate and check, in the style of Daikon) and exports the promoted ones as executable rules.

Every template is one rule function (see RULES): rule(before, after, **params) -> list of (held, detail); an empty
list means the rule does not apply to that step. The same function counts how often a candidate held during
inference and checks the promoted rule in a mutation run (evaluate_inferred_rules), so a rule is checked exactly as
it was measured. Each file in inference/templates/ documents its template.

Guards (inference/guards.py): templates in GUARDABLE are counted in one pass both as they are and under each of
their guards. A guarded candidate is kept only if it is promotable, the unguarded one is not, and the guard adds at
least guard_min_gain support in some dataset (_prune_guarded). A step without the reading a template or guard needs
(traces recorded before a field existed) gives no observation.

Undo/redo round trips are not a template: _round_trip_pairs() feeds (state before an undo/redo step, state after its
immediate counterpart) through the ordinary stability/change_count templates, under the categories
"undo_then_redo@<tab>" / "redo_then_undo@<tab>".

Snapshots are paired only when no failed interaction lies between them: a failed interaction records no snapshot, so
consecutive snapshots of a run must have step numbers one apart (categories._steps_adjacent). This is the pairing the
loop uses, which skips the temporal check after a failed interaction. The same rule applies to the special pairs.

Each dataset is run through the templates independently. A candidate is PROMOTABLE only if every dataset has enough
observations and at least promotion_threshold support for the same direction (inference_config.json); a dataset with
too few observations makes it INSUFFICIENT_EVIDENCE. Agreement across datasets filters out coincidences of one random
walk, but not a real bug in code that all datasets share: such a bug looks like a genuine invariant, so every promoted
rule keeps its per-dataset evidence for review.

Run it with `python -m tests.cli infer`, then `review` (decisions live in config/rule_reviews/, the
newest file is used). Outputs: console summary, invariant_report.md (--report), inferred_rules.json (--rules) and
full rule text (--output-descriptions). --export-only applies the decisions without mining (rewrites
inferred_rules.json from the newest run's candidates.json, or from --candidates PATH). Both modes keep the rules
already in inferred_rules.json that are still accepted in the review, so a smaller trace set never drops a decision.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import sys
import time

# The decisions: the newest file in tests/mutation_testing/config/rule_reviews/ (see review_store.py).
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tests.mutation_testing.inference.inference_config import InferenceConfig, load_config, with_overrides
from tests.mutation_testing.inference.facets import (
    CONTAINMENT_PAIRS,
    CORRELATION_PAIRS,
    FACETS,
    RESTORE_STORES,
    TEXT_VIEWS,
    _facet_has_signal,
)
from tests.mutation_testing.inference.constants import (
    INFERRED_RULES_PATH,
    INFERENCE_DIR,
    INFERENCE_RUNS_DIR,
    SNAPSHOTS_DIR,
    UNTAGGED_DATASET_NAME,
)
from tests.mutation_testing.inference.guards import GUARDABLE, GUARDS
from tests.mutation_testing.inference.templates import RULES
from tests.mutation_testing.inference.categories import (
    _active_tab, _base_category, _is_baseline, _round_trip_pairs, _steps_adjacent, _tabbed_category,
    _undo_pairs,
)
from tests.mutation_testing.inference.common import _enumerate_schema_keyword_paths
from tests.mutation_testing.inference.manual_coverage import evaluate as evaluate_manual_coverage
from tests.mutation_testing.inference.descriptions import _params_label, describe_rule, rule_statement
from tests.mutation_testing.inference.templates.change_count import change_count
from tests.mutation_testing.inference.templates.edit_distance import FACET_FIELDS as EDIT_DISTANCE_FACETS, MODES as EDIT_DISTANCE_MODES, pair_distance
from tests.mutation_testing.inference.templates.gui_control_by_type import observed_rows as observed_control_rows
from tests.mutation_testing.inference.templates.type_stability import TARGET_FIELDS as TYPE_STABILITY_TARGETS
from tests.mutation_testing.inference.memo import memoizing
from tests.mutation_testing.inference.learned_bug import check_wanted, learned_bug_checks, log_learned_bugs
from tests.mutation_testing.inference.review_store import REVIEW_DIR, latest_review_file, load_review


# Numeric parameters live in inference_config.json (see inference_config.py).


# A first-use / later-use candidate is kept only if its support differs from the whole category's by at least this much
# (or, for change_count and edit_distance, if its k differs); see InvariantInferenceEngine._prune_strata
STRATUM_MIN_CONTRAST = 0.3


@dataclass
class Finding:
    """One template's result for one candidate in one dataset."""
    template: str
    category: str
    params: dict  # identifies the candidate with template and category
    held: int  # observations where the positive statement held
    total: int  # applicable observations
    description: str
    expected: int | None = None  # change_count and edit_distance only: k in this dataset
    fail_examples: list[dict] = field(default_factory=list)  # where the positive statement failed
    hold_examples: list[dict] = field(default_factory=list)  # where it held (failures of a negative rule)
    dataset: str = ""

    @property
    def verdict(self) -> str:
        if self.held == self.total:
            return "CONFIRMED"
        if self.held == 0:
            return "NEVER_TRUE"
        return "INCONSISTENT"

    @property
    def confidence(self) -> str:
        return f"{self.held}/{self.total}"


class _Tally:
    """Running count of one candidate's observations while a template is applied to the pairs of a category."""

    def __init__(self) -> None:
        self.held = 0
        self.total = 0
        self.fail_examples: list[dict] = []
        self.hold_examples: list[dict] = []

    def add(self, held: bool, example: dict, max_examples: int) -> None:
        self.total += 1
        if held:
            self.held += 1
            if len(self.hold_examples) < max_examples:
                self.hold_examples.append(example)
        elif len(self.fail_examples) < max_examples:
            self.fail_examples.append(example)


@dataclass
class PromotionCandidate:
    """One candidate merged across datasets."""
    template: str
    category: str
    params: dict
    verdict: str  # PROMOTABLE | BELOW_THRESHOLD | CONTRADICTORY_ACROSS_DATASETS | INSUFFICIENT_EVIDENCE
    per_dataset: dict[str, dict]
    direction: str = "positive"
    expected: int | None = None
    nl_description: str = ""
    rule: dict = field(default_factory=dict)


class InvariantInferenceEngine:

    def __init__(
            self,
            snapshots_dir: str = SNAPSHOTS_DIR,
            min_observations: int | None = None,
            config: InferenceConfig | None = None,
    ):
        # min_observations is an optional per-call override
        self.cfg = with_overrides(config if config is not None else load_config(),
                                  min_observations=min_observations)
        self.dir = Path(snapshots_dir)
        self.snapshots_by_dataset: dict[str, list[dict]] = {}
        self.findings: list[Finding] = []
        self.promotions: list[PromotionCandidate] = []
        self._star_keys: set = set()  # (template, params, direction) of promotable any-action rules
        self._category_sizes: dict[str, dict[str, int]] = defaultdict(dict)  # action category -> {dataset: steps}

    @property
    def min_observations(self) -> int:
        return self.cfg.min_observations

    def discover_datasets(self) -> list[str]:
        """Every {dataset}.jsonl in the snapshots directory except _untagged.jsonl, which cannot take part in a per-dataset check."""
        if not self.dir.exists():
            raise FileNotFoundError(f"Snapshots directory not found: {self.dir}")
        return sorted(
            p.stem for p in self.dir.glob("*.jsonl")
            if p.stem != UNTAGGED_DATASET_NAME
        )

    def load_dataset(self, dataset: str) -> list[dict]:
        """Read one dataset's JSONL file. A bad line (only the last can be cut off by a killed run) is skipped with a warning."""
        path = self.dir / f"{dataset}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"No snapshot file for dataset '{dataset}': {path}")

        snapshots: list[dict] = []
        skipped = 0
        line_num = 0
        with path.open(encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    snapshots.append(json.loads(line))
                except json.JSONDecodeError as e:
                    skipped += 1
                    print(f"[Engine] {path.name}:{line_num} could not be parsed ({e}) "
                          f"- skipping (only ever expected on a killed run's last line)")

        if skipped:
            print(f"[Engine] {dataset}: skipped {skipped} malformed line(s) out of {line_num}")
        print(f"[Engine] Loaded {len(snapshots)} snapshots for dataset '{dataset}' from {path}")
        return snapshots

    def load_all(self, datasets: list[str] | None = None) -> dict[str, int]:
        """Load every discovered dataset (or a subset). Returns {dataset: snapshot count}."""
        datasets = datasets if datasets is not None else self.discover_datasets()
        if not datasets:
            raise FileNotFoundError(
                f"No dataset .jsonl files found in {self.dir} "
                f"(besides possibly {UNTAGGED_DATASET_NAME}.jsonl, which is excluded)"
            )
        counts = {}
        for ds in datasets:
            self.snapshots_by_dataset[ds] = self.load_dataset(ds)
            counts[ds] = len(self.snapshots_by_dataset[ds])
        return counts

    def run(self, on_progress=None) -> list[Finding]:
        """Run every template per dataset, deduplicate, tag findings with their dataset and compute the cross-dataset verdicts.

        on_progress(dataset, dataset_index, dataset_count, category_index, category_count) is called once per action category.
        """
        with memoizing():  # edit distances and GUI control readings are shared by many candidates (inference/memo.py)
            return self._run(on_progress)

    def _scan_control_pairs(self) -> list[tuple[str, str]]:
        """Every (declared schema type, GUI control) pair that occurs in any dataset, so that the same gui_control_by_type
        candidates are tested in every dataset."""
        pairs: set[tuple[str, str]] = set()
        for snapshots in self.snapshots_by_dataset.values():
            for snapshot in snapshots:
                pairs.update((declared, control) for _row, declared, control in observed_control_rows(snapshot))
        return sorted(pairs)

    def _run(self, on_progress=None) -> list[Finding]:
        all_findings: list[Finding] = []
        control_pairs = self._scan_control_pairs()
        dataset_count = len(self.snapshots_by_dataset)

        for dataset_index, (dataset, snapshots) in enumerate(self.snapshots_by_dataset.items(), 1):
            self.findings = []

            # Pair consecutive snapshots of the same run only, one step apart: a failed interaction records no snapshot,
            # so a bigger step jump means one lay between them and the loop skipped its temporal check there.
            clean_pairs = [
                (p, c) for p, c in zip(snapshots[:-1], snapshots[1:])
                if p.get("run_id") is not None
                   and p.get("run_id") == c.get("run_id")
                   and not _is_baseline(c)   # a repair's baseline is no step; the step after it is paired with it as 'before'
                   and _steps_adjacent(p, c)
            ]

            # Every category is also mined for its first and for its later uses in a run ('cat#first', 'cat#later'),
            # so behaviour that only the first use shows (initialisation, defaults) can be told from the steady state.
            # The loop records the use number as snapshot['nth_use']; a trace without it gets no first/later split.
            by_cat: dict[str, list] = defaultdict(list)
            for p, c in clean_pairs:
                cat = _tabbed_category(p, c)
                by_cat[cat].append((p, c))
                n = c.get("nth_use")
                if n:
                    by_cat[f"{cat}#{'first' if n == 1 else 'later'}"].append((p, c))

            for cat, cat_pairs in by_cat.items():
                if "#" not in cat:
                    self._category_sizes[cat][dataset] = len(cat_pairs)

            for cat_index, (cat, cat_pairs) in enumerate(by_cat.items(), 1):
                if on_progress:
                    on_progress(dataset, dataset_index, dataset_count, cat_index, len(by_cat))
                if len(cat_pairs) < self.min_observations:
                    continue
                self._run_stability(cat, cat_pairs)
                self._run_change_count(cat, cat_pairs)
                self._run_edit_distance(cat, cat_pairs)
                for target in TYPE_STABILITY_TARGETS:
                    self._run_rule("type_stability", cat, cat_pairs, {"target": target}, self.min_observations)
                for a, b in CORRELATION_PAIRS:
                    for template in ("correlation", "path_key", "value_to_key"):
                        self._run_rule(template, cat, cat_pairs, {"a": a, "b": b}, self.cfg.min_rule_observations)
                for view in TEXT_VIEWS:
                    self._run_rule("text_store_agreement", cat, cat_pairs, {"view": view}, self.min_observations)
                    self._run_rule("format_agreement", cat, cat_pairs,
                                   {"view": view, "transition": True}, self.min_observations)
                for t in ("gui_validation_agreement", "gui_required_agreement", "gui_value_agreement", "gui_row_set",
                          "gui_renders"):
                    self._run_rule(t, cat, cat_pairs, {"transition": True}, self.min_observations)
                self._run_rule("app_health", cat, cat_pairs, {}, self.min_observations)
            # Undo/redo round trips - a different pairing scheme: (state before the undo/redo
            # pair, state after its counterpart), skipping the intermediate step. Same templates,
            # new pairs, under their own synthetic categories.
            rt_by_cat: dict[str, list] = defaultdict(list)
            for rt_before, rt_after, direction in _round_trip_pairs(snapshots):
                tab = _active_tab(rt_before) or "unknown"
                rt_by_cat[f"{direction}@{tab}"].append((rt_before, rt_after))
            for cat, cat_pairs in rt_by_cat.items():
                if len(cat_pairs) < self.min_observations:
                    continue
                self._run_stability(cat, cat_pairs)
                self._run_change_count(cat, cat_pairs)

            # Undo of an edit: (state before the edit, state after the undo), checked store by store.
            undo_by_cat: dict[str, list] = defaultdict(list)
            for undo_before, undo_after, tag in _undo_pairs(snapshots):
                undo_by_cat[tag].append((undo_before, undo_after))
            for cat, cat_pairs in undo_by_cat.items():
                if len(cat_pairs) < self.min_observations:
                    continue
                for store in RESTORE_STORES:
                    self._run_rule("undo_inverse", cat, cat_pairs, {"store": store}, self.min_observations)

            # State rules do not depend on the action, so they also run once over every pair.
            if len(clean_pairs) >= self.min_observations:
                for a, b in CONTAINMENT_PAIRS:
                    self._run_rule("key_containment", "*", clean_pairs, {"a": a, "b": b},
                                   self.cfg.min_rule_observations)
                for view in TEXT_VIEWS:
                    self._run_rule("text_store_agreement", "*", clean_pairs, {"view": view}, self.min_observations)
                    self._run_rule("format_agreement", "*", clean_pairs,
                                   {"view": view, "transition": False}, self.min_observations)
                for t in ("gui_validation_agreement", "gui_required_agreement", "gui_value_agreement", "gui_row_set",
                          "gui_renders"):
                    self._run_rule(t, "*", clean_pairs, {"transition": False}, self.min_observations)
                self._run_rule("app_health", "*", clean_pairs, {}, self.min_observations)
                for target in TYPE_STABILITY_TARGETS:
                    self._run_rule("type_stability", "*", clean_pairs, {"target": target}, self.min_observations)
                for declared, control in control_pairs:
                    self._run_rule("gui_control_by_type", "*", clean_pairs,
                                   {"declared": declared, "control": control}, self.min_observations)
                for variant in ("app_flags_imply_meta_invalid", "meta_invalid_implies_app_flag"):
                    self._run_rule("validity_agreement", "*", clean_pairs, {"variant": variant}, self.min_observations)

            self._deduplicate()
            for f in self.findings:
                f.dataset = dataset
            all_findings.extend(self.findings)

        self.findings = self._prune_strata(all_findings)
        self._compute_promotions()
        return self.findings

    def _prune_strata(self, findings: list[Finding]) -> list[Finding]:
        """Keep a first-use / later-use candidate only if it tells something the whole category does not: its support
        (pooled over the datasets) differs from the whole category's by at least STRATUM_MIN_CONTRAST, or, for
        change_count and edit_distance, its k differs from the k of the whole category. Everything else repeats the
        plain rule. A candidate is kept or dropped in all datasets together."""
        def identity(f: Finding, category: str) -> tuple:
            return (f.template, category, json.dumps(f.params, sort_keys=True))

        pooled: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
        k_by_dataset: dict[tuple, int | None] = {}   # (identity, dataset) -> the k mined there
        for f in findings:
            key = identity(f, f.category)
            pooled[key][0] += f.held
            pooled[key][1] += f.total
            k_by_dataset[(key, f.dataset)] = f.expected

        keep: set[tuple] = set()
        for f in findings:
            base = _base_category(f.category)
            if base == f.category:
                continue
            key, plain_key = identity(f, f.category), identity(f, base)
            if plain_key not in pooled or not pooled[key][1] or not pooled[plain_key][1]:
                continue
            support = pooled[key][0] / pooled[key][1]
            plain_support = pooled[plain_key][0] / pooled[plain_key][1]
            plain_k = k_by_dataset.get((plain_key, f.dataset))
            other_k = f.expected is not None and plain_k is not None and plain_k != f.expected
            if abs(support - plain_support) >= STRATUM_MIN_CONTRAST or other_k:
                keep.add(key)

        # Show the contrast from both sides: a kept first-use candidate keeps its later-use twin and the other way round
        twins = {(t, _base_category(c) + "#" + ("later" if c.endswith("#first") else "first"), params)
                 for (t, c, params) in keep}
        keep |= twins

        kept = [f for f in findings
                if _base_category(f.category) == f.category or identity(f, f.category) in keep]
        dropped = len(findings) - len(kept)
        if dropped:
            print(f"[Engine] First/later-use split: dropped {dropped} finding(s) that repeat the whole category")
        return kept

    def _run_rule(self, template: str, cat: str, pairs: list, params: dict,
                  min_obs: int, record_params: dict | None = None,
                  expected: int | None = None) -> None:
        """Apply RULES[template] to every pair and record how often the positive statement held.
        record_params is the candidate identity when it differs from params (change_count: the facet).

        A template listed in GUARDABLE is also counted under each of its guards, in the same pass: the rule runs once per
        pair, and an observation also counts for a guard when that guard is True for the pair. Each guarded count is
        recorded as its own finding, with params["guard"] added to the identity; the unguarded rule keeps its identity
        (and so its rule id). Which guarded findings survive is decided in _prune_guarded.
        """
        rule = RULES[template]
        guards = GUARDABLE.get(template, [])
        tallies: dict[str | None, _Tally] = {None: _Tally(), **{g: _Tally() for g in guards}}
        for prev, curr in pairs:
            observations = rule(prev, curr, **params)
            if not observations:
                continue
            open_guards = [g for g in guards if GUARDS[g](prev, curr) is True]
            for h, detail in observations:
                ex = {"run_id": curr.get("run_id"), "step": curr.get("step"),
                      "action": curr.get("action"), **detail}
                for key in (None, *open_guards):
                    tallies[key].add(h, ex, self.cfg.max_examples)

        base = record_params if record_params is not None else params
        for guard, tally in tallies.items():
            if tally.total < min_obs:
                continue
            guarded_params = params if guard is None else {**params, "guard": guard}
            self.findings.append(Finding(
                template=template,
                category=cat,
                params=base if guard is None else {**base, "guard": guard},
                held=tally.held,
                total=tally.total,
                description=rule_statement(template, cat, guarded_params, positive=True),
                expected=expected,
                fail_examples=tally.fail_examples,
                hold_examples=tally.hold_examples,
            ))

    def _run_stability(self, cat: str, pairs: list) -> None:
        for facet, get_facet in FACETS.items():
            if _facet_has_signal(get_facet, pairs):  # skip facets that are empty everywhere
                self._run_rule("stability", cat, pairs, {"facet": facet}, self.min_observations)

        paths: set[tuple[str, ...]] = set()
        for prev, curr in pairs:
            paths |= _enumerate_schema_keyword_paths(prev.get("de_schema"))
            paths |= _enumerate_schema_keyword_paths(curr.get("de_schema"))
        for keys in sorted(paths):
            self._run_rule("stability", cat, pairs, {"path": list(keys)}, self.min_observations)

    def _run_change_count(self, cat: str, pairs: list) -> None:
        """k is the most common change count in the dataset (k == 0 is the stability rule, so it is skipped).
        Reported only if k covers at least cfg.change_count_min_share of the observations.
        """
        for facet, get_facet in FACETS.items():
            if not _facet_has_signal(get_facet, pairs):
                continue
            counts = [d for d in (change_count(get_facet(p), get_facet(c)) for p, c in pairs) if d >= 0]
            if len(counts) < self.min_observations:
                continue
            k, freq = Counter(counts).most_common(1)[0]
            if k == 0 or freq < self.cfg.change_count_min_share * len(counts):
                continue
            self._run_rule("change_count", cat, pairs, {"facet": facet, "expected": k},
                           self.min_observations, record_params={"facet": facet}, expected=k)

    def _run_edit_distance(self, cat: str, pairs: list) -> None:
        """k is the most common non-zero edit distance of the category (0 is the stability rule). It is proposed only if it covers
        at least cfg.change_count_min_share of the steps that changed something. Both "exactly k" and "at most k" are mined:
        a step that does nothing breaks the first and not the second."""
        limit = self.cfg.edit_distance_work_limit
        for facet in EDIT_DISTANCE_FACETS:
            distances = [d for d in (pair_distance(p, c, facet, limit) for p, c in pairs) if d is not None]
            changed = [d for d in distances if d > 0]
            if len(distances) < self.min_observations or not changed:
                continue
            k, freq = Counter(changed).most_common(1)[0]
            if freq < self.cfg.change_count_min_share * len(changed):
                continue
            for mode in EDIT_DISTANCE_MODES:
                self._run_rule("edit_distance", cat, pairs, {"facet": facet, "mode": mode, "k": k, "work_limit": limit},
                               self.min_observations, record_params={"facet": facet, "mode": mode, "work_limit": limit},
                               expected=k)

    def _deduplicate(self) -> None:
        """Drop findings implied by another confirmed finding of the same category and dataset: stability of a schema path
        when the whole schema is stable, and never-true correlation findings whose target facet never changes.
        """
        whole_stable = {
            (f.category, f.params["facet"]) for f in self.findings
            if f.template == "stability" and "facet" in f.params and f.verdict == "CONFIRMED"
        }
        keep = []
        for f in self.findings:
            p = f.params
            if f.template == "stability" and p.get("path") in (["properties"], ["required"]):
                continue
            if f.template == "stability" and (f.category, "schema") in whole_stable:
                if "path" in p or p.get("facet") in ("schema.properties", "schema.required", "schema.defs"):
                    continue
            if (f.template in ("correlation", "path_key", "value_to_key") and f.verdict == "NEVER_TRUE"
                    and (f.category, p["b"].split(".")[0]) in whole_stable):
                continue
            keep.append(f)
        removed = len(self.findings) - len(keep)
        self.findings = keep
        if removed:
            print(f"[Engine] Deduplicated: removed {removed} redundant findings")

    @staticmethod
    def _group_key(f: Finding) -> tuple:
        """Candidate identity, independent of dataset and verdict."""
        return (f.template, f.category, json.dumps(f.params, sort_keys=True))

    @staticmethod
    def _direction_and_support(held: int, total: int) -> tuple[str, float]:
        """Majority direction of one dataset and its support (ties count as positive)."""
        if total == 0:
            return "positive", 0.0
        pct = held / total
        return ("positive", pct) if pct >= 0.5 else ("negative", 1.0 - pct)

    def _compute_promotions(self) -> None:
        """Decide a verdict per candidate:

          PROMOTABLE                     every dataset has enough observations and clears promotion_threshold in the same
                                         direction (for change_count and edit_distance, also the same k)
          BELOW_THRESHOLD                enough data everywhere, but not every dataset clears the threshold
          CONTRADICTORY_ACROSS_DATASETS  datasets that clear the threshold disagree on direction (or on k)
          INSUFFICIENT_EVIDENCE          a dataset is missing or has too few observations

        Only datasets that clear the threshold vote on direction: one near 50% flips on a single observation.
        """
        all_datasets = sorted(self.snapshots_by_dataset.keys())
        groups: dict[tuple, list[Finding]] = defaultdict(list)
        for f in self.findings:
            groups[self._group_key(f)].append(f)

        self.promotions = []
        for key, group in groups.items():
            per_dataset: dict[str, dict] = {}
            for f in group:
                direction, support = self._direction_and_support(f.held, f.total)
                per_dataset[f.dataset] = {
                    "n": f.held, "m": f.total, "direction": direction, "support": support,
                    "verdict": f.verdict, "expected": f.expected,
                    "fail_examples": f.fail_examples, "hold_examples": f.hold_examples,
                }

            missing = [d for d in all_datasets if d not in per_dataset]
            thin = [d for d, v in per_dataset.items() if v["m"] < self.cfg.min_observations_for_promotion]
            constants = {v["expected"] for v in per_dataset.values()}

            direction = "positive"
            direction_fixed = False  # True when datasets that clear the threshold decided it
            if missing or thin:
                verdict = "INSUFFICIENT_EVIDENCE"
            elif len(constants) > 1:
                verdict = "CONTRADICTORY_ACROSS_DATASETS"
            else:
                strong = {v["direction"] for v in per_dataset.values() if v["support"] >= self.cfg.promotion_threshold}
                if len(strong) > 1:
                    verdict = "CONTRADICTORY_ACROSS_DATASETS"
                elif all(v["support"] >= self.cfg.promotion_threshold for v in per_dataset.values()):
                    direction = strong.pop()
                    verdict = "PROMOTABLE"
                else:
                    if strong:
                        direction = strong.pop()
                        direction_fixed = True
                    verdict = "BELOW_THRESHOLD"

            # A candidate that no dataset settles is stated the way most of its observations point, so a rule that is
            # mostly false is shown as its opposite instead of as a positive statement with a support near 0%.
            if verdict in ("INSUFFICIENT_EVIDENCE", "BELOW_THRESHOLD") and not direction_fixed:
                observed = sum(v["m"] for v in per_dataset.values())
                if observed and sum(v["n"] for v in per_dataset.values()) / observed < 0.5:
                    direction = "negative"

            sample = group[0]
            expected = next(iter(constants)) if len(constants) == 1 else None
            eval_params = dict(sample.params)
            if sample.template == "change_count":
                eval_params["expected"] = expected
            elif sample.template == "edit_distance":
                eval_params["k"] = expected
            cand = PromotionCandidate(
                template=sample.template,
                category=sample.category,
                params=eval_params,
                verdict=verdict,
                per_dataset=per_dataset,
                direction=direction,
                expected=expected,
                nl_description=rule_statement(sample.template, sample.category, eval_params,
                                              positive=direction == "positive"),
            )
            cand.rule = self._build_rule(cand)
            self.promotions.append(cand)

        self._prune_guarded()
        self.promotions.sort(key=lambda p: (p.verdict != "PROMOTABLE", p.template, p.category,
                                            _params_label(p.template, p.params)))

        self._star_keys = {self._rule_key(p) for p in self.promotions
                           if p.category == "*" and p.verdict == "PROMOTABLE"}

    @staticmethod
    def _unguarded_key(p: PromotionCandidate) -> tuple:
        """Identity of a candidate without its guard: template, category and the other params."""
        return (p.template, p.category, json.dumps({k: v for k, v in p.params.items() if k != "guard"}, sort_keys=True))

    def _guard_gain(self, guarded: PromotionCandidate, plain: PromotionCandidate) -> float:
        """Largest per-dataset gain in positive support that the guard brings over the rule without it."""
        gains = [guarded.per_dataset[ds]["support"] - plain.per_dataset[ds]["support"]
                 for ds in guarded.per_dataset
                 if ds in plain.per_dataset
                 and guarded.per_dataset[ds]["direction"] == "positive" == plain.per_dataset[ds]["direction"]]
        return max(gains, default=0.0)

    def _prune_guarded(self) -> None:
        """Keep a guarded candidate only if the guard is what makes the rule hold: the unguarded rule is not promotable,
        the guard adds at least cfg.guard_min_gain support in some dataset, and the guarded rule is promotable or at
        least clears the threshold in one dataset (then listed as worth investigating: a rule that still fails under
        the guard is the interesting case, e.g. a blank GUI with a valid schema). The rest would repeat a rule that
        holds anyway or clutter the review. A guarded candidate whose unguarded twin was not recorded is dropped too.
        """
        plain = {self._unguarded_key(p): p for p in self.promotions if "guard" not in p.params}
        kept = []
        for p in self.promotions:
            if "guard" not in p.params:
                kept.append(p)
                continue
            twin = plain.get(self._unguarded_key(p))
            clears_somewhere = any(self._held(p, v)[1] >= self.cfg.promotion_threshold for v in p.per_dataset.values())
            if (twin is not None and p.direction == "positive" and twin.verdict != "PROMOTABLE"
                    and (p.verdict == "PROMOTABLE" or (p.verdict == "BELOW_THRESHOLD" and clears_somewhere))
                    and self._guard_gain(p, twin) >= self.cfg.guard_min_gain):
                kept.append(p)
        dropped = len(self.promotions) - len(kept)
        self.promotions = kept
        if dropped:
            print(f"[Engine] Guards: dropped {dropped} guarded candidate(s) that the guard does not rescue")

    @staticmethod
    def _rule_key(p: PromotionCandidate) -> tuple:
        return (p.template, json.dumps(p.params, sort_keys=True), p.direction)

    @staticmethod
    def _held(p: PromotionCandidate, v: dict) -> tuple[int, float]:
        """How often the candidate's statement held, in its direction, in one dataset."""
        held = v["n"] if p.direction == "positive" else v["m"] - v["n"]
        return held, (held / v["m"] if v["m"] else 0.0)

    def _build_rule(self, p: PromotionCandidate) -> dict:
        """Exportable, executable form of a candidate."""
        core = {"template": p.template, "category": p.category,
                "params": p.params, "direction": p.direction}
        rule_id = "INF-" + hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:8].upper()
        evidence = {}
        for ds, v in sorted(p.per_dataset.items()):
            held, pct = self._held(p, v)
            evidence[self._short_ds(ds)] = {"held": held, "observations": v["m"], "support": round(pct, 4)}
        return {
            "id": rule_id,
            **core,
            "statement": p.nl_description,
            "card": describe_rule(core),
            "min_support": min((e["support"] for e in evidence.values()), default=0.0),
            "evidence": evidence,
        }

    def _manual_coverage(self) -> dict:
        """Which hand-written invariants the candidates recover (see inference/manual_coverage.py)."""
        return evaluate_manual_coverage(self.promotions, self._category_sizes, sorted(self.snapshots_by_dataset),
                                        self.cfg.min_observations)

    def _reviewable(self, p: PromotionCandidate) -> bool:
        """Can this candidate be accepted in the review, whatever its confidence?"""
        # "other" groups unrelated actions, so a rule over it says nothing about any one of them; an unknown tab cannot be matched.
        return (not p.category.startswith("other@")
                and not _base_category(p.category).endswith("@unknown")
                # change_count and edit_distance need one k that every dataset agrees on
                and not (p.template in ("change_count", "edit_distance") and p.expected is None)
                # an any-action ('*') rule already checks it after every step (see _covered)
                and not self._covered(p))

    def _exportable(self, p: PromotionCandidate) -> bool:
        """Reviewable and held in every dataset at the threshold. Others can still be accepted by hand (manual promotion)."""
        return p.verdict == "PROMOTABLE" and self._reviewable(p)

    def _covered(self, p: PromotionCandidate) -> bool:
        """True if a promotable any-action ('*') rule already checks this one after every step: the same template, params and
        direction, or, for a transition rule, its state version (a state that is always right cannot be broken by a step).
        """
        if p.category == "*":
            return False
        if self._rule_key(p) in self._star_keys:
            return True
        if p.direction == "positive" and p.params.get("transition"):
            state_key = (p.template, json.dumps({**p.params, "transition": False}, sort_keys=True), p.direction)
            return state_key in self._star_keys
        return False

    def write_rules(self, path: str = INFERRED_RULES_PATH) -> int:
        """Write inferred_rules.json, which TemporalInvariantChecker evaluates."""
        reviewed = load_review(REVIEW_DIR)
        rules = [{**p.rule, "verdict": p.verdict,
                  "promotion": _promotion_label(p.verdict, p.rule["min_support"], self.cfg.promotion_threshold)}
                 for p in self.promotions
                 if self._reviewable(p) and reviewed.get(p.rule["id"], {}).get("status") == "accepted"]
        checks = learned_bug_checks([p.rule for p in self.promotions if self._reviewable(p)], reviewed)
        rules += checks
        log_learned_bugs(checks, reviewed)
        rules = _keep_accepted(path, rules, reviewed)
        return _write_rules_file(path, rules, {
            "source": str(self.dir),
            "datasets": [self._short_ds(d) for d in sorted(self.snapshots_by_dataset)],
            "threshold": self.cfg.promotion_threshold,
            "config": asdict(self.cfg),
        })

    def generate_nl_descriptions(self) -> list[str]:
        """Full rule text of the exported rules."""
        out = []
        for p in self.promotions:
            if not self._exportable(p):
                continue
            c = p.rule["card"]
            out.append(f"[{p.rule['id']}] {p.nl_description}. Trigger: {c['trigger']}. "
                       f"Applies when: {c['applies_when']}. Asserts: {c['asserts']}. "
                       f"Definitions: {c['definitions']}.")
        return out

    def print_report(self, verbose: bool = False) -> None:
        print(f"\n{'=' * 70}")
        print(f"Invariant Inference Engine - {len(self.snapshots_by_dataset)} dataset(s)")
        print(f"{'=' * 70}")
        counts = Counter(p.verdict for p in self.promotions)
        print(f"  Invariants that hold everywhere : {counts['PROMOTABLE']}")
        print(f"  Worth investigating             : {len(self._investigate_candidates())}")
        print(f"  Contradictory across datasets   : {counts['CONTRADICTORY_ACROSS_DATASETS']}")
        print(f"  Not enough data                 : {counts['INSUFFICIENT_EVIDENCE']}")
        if not verbose:
            print("  (run with --verbose for the raw per-dataset dump)")
            return

        by_dataset: dict[str, list[Finding]] = defaultdict(list)
        for f in self.findings:
            by_dataset[f.dataset].append(f)
        for dataset in sorted(by_dataset):
            fs = by_dataset[dataset]
            print(f"\n### Dataset: {dataset}  ({sum(1 for s in self.snapshots_by_dataset.get(dataset, []) if not _is_baseline(s))} snapshots) ###")
            for f in fs:
                if f.verdict == "INCONSISTENT":
                    print(f"    [{f.template}] {f.category}: {_params_label(f.template, f.params)}  ({f.confidence})")

        print("\n── CROSS-DATASET PROMOTION ──")
        for p in self.promotions:
            if p.verdict in ("PROMOTABLE", "CONTRADICTORY_ACROSS_DATASETS"):
                print(f"  [{p.verdict}] {p.rule['id']} {p.nl_description}")
                for ds in sorted(p.per_dataset):
                    v = p.per_dataset[ds]
                    held, pct = self._held(p, v)
                    print(f"      {self._short_ds(ds)}: {held}/{v['m']} ({pct:.0%})")

    @staticmethod
    def _short_ds(dataset: str) -> str:
        """'strenda_schema__strenda_data_json' becomes 'strenda'."""
        return dataset.split("_schema__")[0]

    def _investigate_candidates(self) -> list[PromotionCandidate]:
        """BELOW_THRESHOLD candidates that clear the threshold in at least one dataset."""
        cands = [
            p for p in self.promotions
            if p.verdict == "BELOW_THRESHOLD"
               and any(self._held(p, v)[1] >= self.cfg.promotion_threshold for v in p.per_dataset.values())
        ]
        cands.sort(key=lambda p: -sum(self._held(p, v)[1] >= self.cfg.promotion_threshold
                                      for v in p.per_dataset.values()))
        return cands

    def _counter_examples(self, p: PromotionCandidate, v: dict) -> list[dict]:
        """Steps where the candidate's statement, in its own direction, failed."""
        return v["fail_examples"] if p.direction == "positive" else v["hold_examples"]

    def _support_row(self, p: PromotionCandidate, mark_below: bool) -> str:
        parts = []
        for ds, v in sorted(p.per_dataset.items()):
            held, pct = self._held(p, v)
            flag = "! " if mark_below and pct < self.cfg.promotion_threshold else ""
            parts.append(f"{flag}{self._short_ds(ds)} {pct:.0%} ({held}/{v['m']})")
        return " · ".join(parts)

    @staticmethod
    def _statement(p: PromotionCandidate) -> str:
        """The candidate's sentence without the 'after <action>: ' prefix."""
        return p.nl_description.removeprefix(f"after {p.category}: ")

    @classmethod
    def _family_key(cls, p: PromotionCandidate) -> str:
        """Candidates with the same template and sentence form a rule family (same statement, different actions).
        The review page groups the same way.
        """
        return f"{p.template}|{cls._statement(p)}"

    def _write_grouped(self, w, candidates: list[PromotionCandidate], show_outliers: bool) -> None:
        """One collapsed <details> block per rule family."""
        if not candidates:
            w("_None._\n")
            return
        T = self.cfg.promotion_threshold
        families: dict[str, list[PromotionCandidate]] = defaultdict(list)
        for p in candidates:
            families[self._family_key(p)].append(p)
        ordered = sorted(families.values(), key=lambda ms: (-len(ms), self._statement(ms[0])))
        for members in ordered:
            members.sort(key=lambda p: p.category)
            n = len(members)
            if n > 1:
                scope = f"{n} actions"
            elif members[0].category == "*":
                scope = "any action"
            else:
                scope = f"<code>{html.escape(members[0].category)}</code>"
            warn = ""
            if show_outliers:
                weak = sorted({self._short_ds(ds) for p in members for ds, v in p.per_dataset.items()
                               if v["m"] >= self.cfg.min_observations_for_promotion
                               and self._held(p, v)[1] < T})
                if weak:
                    warn = f" · below threshold in {', '.join(weak)}"
            w("<details>")
            w(f"<summary><b>{html.escape(self._statement(members[0]))}</b> - {scope}{warn}</summary>\n")
            # Print the definition once if every rule of the family shares it.
            defs = {(p.rule["card"]["applies_when"], p.rule["card"]["asserts"]) for p in members}
            if len(defs) == 1:
                applies_when, asserts = next(iter(defs))
                w(f"_Applies when:_ {applies_when}. _Asserts:_ {asserts}.\n")
            for p in members:
                label = "Any action" if p.category == "*" else f"After `{p.category}`"
                card = p.rule["card"]
                w(f"- **{label}** `{p.rule['id']}`  ")
                w(f"  {self._support_row(p, mark_below=show_outliers)}  ")
                if len(defs) > 1:
                    w(f"  _Applies when:_ {card['applies_when']}. _Asserts:_ {card['asserts']}.")
                if show_outliers:
                    for ds, v in sorted(p.per_dataset.items()):
                        ex = self._counter_examples(p, v)
                        if self._held(p, v)[1] < T and ex:
                            steps = ", ".join(f"run {e.get('run_id')} step {e.get('step')} `{e.get('action')}`"
                                              for e in ex[:self.cfg.max_examples])
                            w(f"  - {self._short_ds(ds)} counter-examples: {steps}")
            w("")
            w("</details>\n")

    def write_readable_report(self, path: str) -> None:
        T = self.cfg.promotion_threshold
        promotable = [p for p in self.promotions if p.verdict == "PROMOTABLE"]
        contradictory = [p for p in self.promotions if p.verdict == "CONTRADICTORY_ACROSS_DATASETS"]
        insufficient = [p for p in self.promotions if p.verdict == "INSUFFICIENT_EVIDENCE"]
        investigate = self._investigate_candidates()

        out: list[str] = []
        w = out.append
        total = sum(1 for s in self.snapshots_by_dataset.values() for x in s if not _is_baseline(x))
        w("# Invariant Inference Report\n")
        w(f"Source: `{self.dir}` - {len(self.snapshots_by_dataset)} datasets, {total} snapshots\n")
        w("| Dataset | Snapshots |")
        w("|---|---|")
        for ds, snaps in sorted(self.snapshots_by_dataset.items()):
            w(f"| {self._short_ds(ds)} | {sum(1 for x in snaps if not _is_baseline(x))} |")
        w("")
        w("## At a glance\n")
        w(f"- **{len(promotable)} candidate rules hold** in every dataset (≥{T:.0%} in each), as "
          f"{len({self._family_key(p) for p in promotable})} distinct statements; "
          f"{sum(self._exportable(p) for p in promotable)} are exported as executable rules")
        w(f"- **{len(investigate)} worth investigating** - hold in some datasets, not in others")
        w(f"- **{len(contradictory)} contradictory** - datasets clearly point opposite ways")
        w(f"- **{len(insufficient)} not judged** - too few observations in at least one dataset")
        w("")
        w(f"**Reading the numbers:** `strenda 92% (11/12)` means the statement held in 11 of 12 "
          f"steps it applied to in the Strenda dataset. Full rule definitions are in the review page "
          f"and in `{INFERRED_RULES_PATH}`.\n")
        w("**Rule families:** every statement below is collapsed. Click it to see the actions it "
          "holds after, with one rule ID and the support per dataset for each. (Needs a viewer that "
          "renders `<details>`, e.g. GitHub or the VS Code preview.)\n")
        w("## 1. Candidates that hold in every dataset\n")
        self._write_grouped(w, promotable, show_outliers=False)
        w("## 2. Worth investigating\n")
        w("Each statement reaches the threshold in the unmarked datasets but not in those marked - "
          "either the behaviour is schema-specific, or those schemas trigger a bug.\n")
        self._write_grouped(w, investigate, show_outliers=True)
        w("## 3. Contradictory across datasets\n")
        self._write_grouped(w, contradictory, show_outliers=True)
        w("## 4. Not enough data\n")
        lacking: dict[str, int] = defaultdict(int)
        by_cat: dict[str, int] = defaultdict(int)
        for p in insufficient:
            by_cat[p.category] += 1
            for ds in self.snapshots_by_dataset:
                v = p.per_dataset.get(ds)
                if v is None or v["m"] < self.cfg.min_observations_for_promotion:
                    lacking[self._short_ds(ds)] += 1
        w("| Dataset | Candidates it has too little data for |")
        w("|---|---|")
        for ds, n in sorted(lacking.items(), key=lambda x: -x[1]):
            w(f"| {ds} | {n} |")
        w("")
        w("| Action category | Candidates not judged |")
        w("|---|---|")
        for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):
            w(f"| {cat} | {n} |")
        w("")
        manual = self._manual_coverage()
        m = manual["summary"]
        w("## 5. Hand-written invariants\n")
        w(f"Of the {m['total']} hand-written invariants, {m['testable']} can be said by a template and tested on these traces. "
          f"The engine found **{m['recovered']}** of them at the threshold (recall {m['recall']:.0%}); "
          f"{m['partial']} partly, {m['seen']} seen but not promotable, {m['missing']} missing. "
          f"{m['no_evidence']} cannot be tested (the traces lack that action) and {m['not_expressible']} cannot be "
          f"expressed by any template yet.\n")
        w("| Invariant | What it says | Found by the engine |")
        w("|---|---|---|")
        for e in manual["entries"]:
            found = {"recovered": "yes", "partial": "partly", "seen": "seen, not promotable", "missing": "no",
                     "no_evidence": "cannot be tested: the traces lack that action",
                     "not_expressible": "no template: " + e.get("reason", "")}[e["status"]]
            w(f"| {e['id']} | {e['name']} | {found} |")
        w("")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("\n".join(out), encoding="utf-8")
        print(f"[Engine] Readable report → {path}")

    def candidates_payload(self) -> dict:
        """All candidate data (written to candidates.json); every number is computed here."""
        from datetime import datetime
        investigate_ids = {id(p) for p in self._investigate_candidates()}
        group_of = {"PROMOTABLE": "holds", "CONTRADICTORY_ACROSS_DATASETS": "contradictory",
                    "INSUFFICIENT_EVIDENCE": "insufficient"}
        manual = self._manual_coverage()
        candidates = []
        for p in self.promotions:
            group = "investigate" if id(p) in investigate_ids else group_of.get(p.verdict, "below")
            datasets = {}
            for ds, v in p.per_dataset.items():
                held, pct = self._held(p, v)
                datasets[self._short_ds(ds)] = {
                    "held": held, "m": v["m"], "pct": round(pct, 4),
                    "examples": self._counter_examples(p, v)[:self.cfg.max_examples] if held < v["m"] else [],
                    # steps where the candidate's statement held (the other list of the finding), for the review page
                    "support": (v["hold_examples"] if p.direction == "positive" else v["fail_examples"])[:self.cfg.max_examples],
                }
            candidates.append({
                "template": p.template,
                "category": p.category,
                "group": group,
                "exported": self._exportable(p),
                "reviewable": self._reviewable(p),
                "manual": manual["tags"].get(p.rule["id"], []),
                "covered": p.verdict == "PROMOTABLE" and self._covered(p),
                "statement": self._statement(p),
                "family": self._family_key(p),
                "datasets": datasets,
                "rule": p.rule,
            })
        return {
            "source": str(self.dir),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "threshold": self.cfg.promotion_threshold,
            "min_obs": self.cfg.min_observations_for_promotion,
            "rules_file": INFERRED_RULES_PATH,
            "datasets": [{"name": self._short_ds(ds), "snapshots": sum(1 for x in s if not _is_baseline(x))}
                         for ds, s in sorted(self.snapshots_by_dataset.items())],
            "manual_coverage": {"entries": manual["entries"], "summary": manual["summary"]},
            "candidates": candidates,
        }

_VERDICT_OF_GROUP = {"holds": "PROMOTABLE", "contradictory": "CONTRADICTORY_ACROSS_DATASETS",
                     "insufficient": "INSUFFICIENT_EVIDENCE", "investigate": "BELOW_THRESHOLD", "below": "BELOW_THRESHOLD"}


def _promotion_label(verdict: str, min_support: float, threshold: float) -> str:
    """How an accepted rule got into inferred_rules.json.
    automatic              PROMOTABLE in this mining.
    insufficient_evidence  not promotable only because some dataset has too few observations (INSUFFICIENT_EVIDENCE) while
                           every dataset that was observed clears the threshold: the rule was promotable on a larger corpus.
    manual                 accepted below the threshold (BELOW_THRESHOLD, CONTRADICTORY_ACROSS_DATASETS, or too few
                           observations and the observed ones are below the threshold too)."""
    if verdict == "PROMOTABLE":
        return "automatic"
    if verdict == "INSUFFICIENT_EVIDENCE" and min_support >= threshold:
        return "insufficient_evidence"
    return "manual"


def _keep_accepted(path: str, rules: list[dict], reviewed: dict) -> list[dict]:
    """Add the rules of the existing rules file that are accepted in the review but were not mined this time,
    so a smaller trace set cannot drop decided rules."""
    try:
        old = json.loads(Path(path).read_text(encoding="utf-8")).get("rules", [])
    except (OSError, ValueError):
        return rules
    have = {r["id"] for r in rules}
    kept = [r for r in old if r.get("id") not in have
            and (reviewed.get(r.get("id"), {}).get("status") == "accepted" or check_wanted(r.get("negates", ""), reviewed))]
    if kept:
        print(f"[Engine] kept {len(kept)} accepted rule(s) that this mining did not reproduce")
    return rules + kept


def _write_rules_file(path: str, rules: list[dict], header: dict) -> int:
    """Write inferred_rules.json. When the set of rules changes, the previous file is kept as a timestamped .bak copy in rules_backups/,
    so a run without decisions cannot wipe them."""
    from datetime import datetime
    target = Path(path)
    if target.exists():
        try:
            old_ids = {r.get("id") for r in json.loads(target.read_text(encoding="utf-8")).get("rules", [])}
        except (OSError, ValueError):
            old_ids = set()
        if old_ids != {r["id"] for r in rules}:
            import shutil
            from tests.mutation_testing.common.paths import RULES_BACKUPS_DIR
            RULES_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            backup = RULES_BACKUPS_DIR / f"{target.stem}.{datetime.now():%Y%m%d_%H%M%S}.bak{target.suffix}"
            shutil.copy2(target, backup)
            print(f"[Engine] rules changed ({len(old_ids)} -> {len(rules)}); previous file saved as {backup}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), **header, "rules": rules,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[Engine] {len(rules)} executable rules → {path}")
    return len(rules)


def export_from_candidates(rules_path: str, candidates_path: str | None, archive_dir: str) -> int:
    """Rewrite inferred_rules.json from a saved candidates.json and the newest review in config/rule_reviews/, without mining.
    Uses the newest run folder when candidates_path is not given. The decisions must belong to that run: the review page
    reads the same candidates.json, so use the run you reviewed."""
    if candidates_path:
        cand_file = Path(candidates_path)
    else:
        runs = sorted(p for p in Path(archive_dir).glob("*/candidates.json"))
        if not runs:
            raise SystemExit(f"[Engine] no candidates.json under {archive_dir}; run the inference once without --export-only")
        cand_file = runs[-1]
    if latest_review_file(REVIEW_DIR) is None:
        raise SystemExit(f"[Engine] no review file in {REVIEW_DIR}: nothing has been decided yet")
    payload = json.loads(cand_file.read_text(encoding="utf-8"))
    reviewed = load_review(REVIEW_DIR)
    by_id = {c["rule"]["id"]: c for c in payload["candidates"]}
    threshold = payload.get("threshold", 1.0)
    rules = [{**c["rule"], "verdict": _VERDICT_OF_GROUP.get(c["group"], "BELOW_THRESHOLD"),
              "promotion": _promotion_label(_VERDICT_OF_GROUP.get(c["group"], "BELOW_THRESHOLD"),
                                            c["rule"]["min_support"], threshold)}
             for c in by_id.values()
             if c.get("reviewable", c.get("exported")) and reviewed.get(c["rule"]["id"], {}).get("status") == "accepted"]
    checks = learned_bug_checks([c["rule"] for c in by_id.values() if c.get("reviewable", c.get("exported"))], reviewed)
    rules += checks
    log_learned_bugs(checks, reviewed)
    rules = _keep_accepted(rules_path, rules, reviewed)
    print(f"[Engine] candidates from {cand_file}")
    return _write_rules_file(rules_path, rules, {
        "source": payload.get("source"), "datasets": [d["name"] for d in payload.get("datasets", [])],
        "threshold": payload.get("threshold"), "exported_from": str(cand_file),
    })


def _format_duration(seconds: float) -> str:
    """'45 s', '1 min 19 s' or '1 h 02 min'."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


class _MiningProgress:
    """Progress line for engine.run(): on a terminal one line is redrawn in place, otherwise one line per dataset.
    Anything else printed meanwhile (the engine's own messages) first clears the line, so the two never mix.
    Use as a context manager; pass .update to engine.run(on_progress=...)."""
    WIDTH = 20

    def __init__(self) -> None:
        self._tty = sys.stdout.isatty()
        self._shown = 0
        self._last_dataset = None
        self._stdout = None

    def __enter__(self):
        if self._tty:
            self._stdout = sys.stdout
            sys.stdout = self
        return self

    def __exit__(self, *exc) -> None:
        if self._stdout is not None:
            self._clear()
            sys.stdout = self._stdout

    # file-like, so it can stand in for sys.stdout while the line is shown
    def write(self, text: str) -> int:
        self._clear()
        return self._stdout.write(text)

    def flush(self) -> None:
        self._stdout.flush()

    def isatty(self) -> bool:
        return True

    def __getattr__(self, name):
        return getattr(self._stdout, name)

    def _clear(self) -> None:
        if self._shown:
            self._stdout.write("\r" + " " * self._shown + "\r")
            self._shown = 0

    def update(self, dataset: str, dataset_index: int, dataset_count: int, category_index: int, category_count: int) -> None:
        if not self._tty:
            if dataset != self._last_dataset:
                print(f"[Engine] mining dataset {dataset_index}/{dataset_count}: {dataset}")
                self._last_dataset = dataset
            return
        fraction = ((dataset_index - 1) + category_index / max(category_count, 1)) / max(dataset_count, 1)
        filled = min(self.WIDTH, int(self.WIDTH * fraction))
        text = (f"[Engine] mining [{'█' * filled}{'░' * (self.WIDTH - filled)}] {fraction:.0%}  "
                f"dataset {dataset_index}/{dataset_count}, category {category_index}/{category_count}")
        text = text[:max(20, shutil.get_terminal_size((100, 20)).columns - 1)]
        self._clear()
        self._stdout.write(text)
        self._stdout.flush()
        self._shown = len(text)


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description="Invariant inference engine for MetaConfigurator")
    parser.add_argument("--snapshots-dir", default=SNAPSHOTS_DIR,
                        help="Directory of per-dataset {dataset}.jsonl files")
    parser.add_argument("--datasets", default=None,
                        help="Comma-separated subset of datasets to load (default: all discovered)")
    parser.add_argument("--min-obs", type=int, default=None,
                        help="Override min_observations from the config")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Override promotion_threshold from the config")
    parser.add_argument("--config", default=None,
                        help="Parameter file (default: inference_config.json next to this module)")
    parser.add_argument("--profile", default=None,
                        help="Profile inside the parameter file (default: 'default')")
    parser.add_argument("--rules", default=INFERRED_RULES_PATH,
                        help=f"Executable rules for TemporalInvariantChecker (default: {INFERRED_RULES_PATH})")
    parser.add_argument("--report", default=f"{INFERENCE_DIR}/invariant_report.md",
                        help=f"Readable Markdown report (default: {INFERENCE_DIR}/invariant_report.md)")
    parser.add_argument("--archive-dir", default=INFERENCE_RUNS_DIR,
                        help="Each engine run also writes a timestamped folder here")
    parser.add_argument("--output-descriptions", default=None,
                        help="Write full rule text for exported rules")
    parser.add_argument("--export-only", action="store_true",
                        help="Do not mine: rewrite the rules file from the newest run's candidates.json and the newest file in rule_reviews/")
    parser.add_argument("--candidates", default=None,
                        help="With --export-only: the candidates.json to use (default: newest run folder)")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print the raw per-dataset dump to the console")
    args = parser.parse_args()

    if args.export_only:
        export_from_candidates(args.rules, args.candidates, args.archive_dir)
        return

    cfg = with_overrides(load_config(args.config, args.profile),
                         min_observations=args.min_obs, promotion_threshold=args.threshold)
    engine = InvariantInferenceEngine(args.snapshots_dir, config=cfg)
    requested = [d.strip() for d in args.datasets.split(",")] if args.datasets else None
    engine.load_all(requested)
    mining_started = time.perf_counter()
    with _MiningProgress() as progress:
        engine.run(on_progress=progress.update)
    mining_seconds = time.perf_counter() - mining_started
    engine.print_report(verbose=args.verbose)
    engine.write_rules(args.rules)
    engine.write_readable_report(args.report)

    from datetime import datetime
    run_dir = Path(args.archive_dir) / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    # A run folder keeps what the other tools need: the candidates (review), the rules the loop uses, and the
    # decisions in force. The report is the same data as candidates.json and is only written to
    # --report (the latest run).
    engine.write_rules(str(run_dir / "inferred_rules.json"))
    (run_dir / "candidates.json").write_text(
        json.dumps(engine.candidates_payload(), ensure_ascii=False, indent=1), encoding="utf-8")
    latest_review = latest_review_file(REVIEW_DIR)
    if latest_review is not None:
        shutil.copy(latest_review, run_dir / "rule_review.json")   # the decisions in force for this run

    if args.output_descriptions:
        descs = engine.generate_nl_descriptions()
        Path(args.output_descriptions).write_text("\n".join(descs), encoding="utf-8")
        print(f"[Engine] {len(descs)} rule descriptions → {args.output_descriptions}")

    print(f"[Engine] Inference took {_format_duration(time.perf_counter() - started)} "
          f"(mining {_format_duration(mining_seconds)})")


if __name__ == "__main__":
    main()