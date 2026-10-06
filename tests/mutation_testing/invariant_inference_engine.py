"""Invariant inference engine. Finds candidate invariants in recorded state snapshots with a small template
library (generate and check, in the style of Daikon) and exports the promoted ones as executable rules.

Every template is one rule function (see RULES): rule(before, after, **params) -> list of (held, detail).
An empty list means the rule does not apply to that step. The same function counts how often a candidate held
during inference and checks the promoted rule during a mutation run (evaluate_inferred_rules), so a rule is
checked exactly as it was measured.

Templates:
    stability             a facet (or schema keyword path) is identical before and after
    change_count          DeepDiff reports exactly k changes for a facet
    correlation            if the top-level keys of A change, the keys of B change (or do not)
    path_key               a key added or removed in A is added or removed in B as well (or not)
    value_to_key           each key newly added to A is a top-level key of B afterwards (or not)
    key_containment        after the step, keys(A) are a subset of keys(B) (or not); any action
    format_agreement       the Text View content is in the format the selector shows (or not);
                          the transition variant only blames the action that introduced a mismatch
    text_store_agreement  the parsed Text View equals the editor's store (or not)
    order_stability        the raw order of a list-valued facet is unchanged (only meaningful when
                          its set of elements hasn't changed - see ORDERED_FACETS)
    gui_validation_agreement  a root-level property's GUI validation-error icon agrees with whether
                          its value actually violates its declared schema; any action, state rule

Undo/redo round trips are not a new template: _round_trip_pairs() feeds a different kind of pair
(state before an undo/redo step, state after its immediate matching counterpart, skipping the
undone/redone state in between) through the ordinary stability/change_count templates, under
synthetic categories "undo_then_redo@<tab>" / "redo_then_undo@<tab>".

Each dataset is run through the templates independently. A candidate is PROMOTABLE only if every dataset has
enough observations and at least promotion_threshold support for the same direction (inference_config.json).
A dataset with too few observations makes it INSUFFICIENT_EVIDENCE.

Limit: requiring agreement across datasets filters out coincidences of one random walk, but not a real bug in
code that all datasets share. Such a bug looks like a genuine invariant in the traces, so every promoted rule
keeps its per-dataset evidence for review.

Outputs: console summary, reports/invariant_report.md (--report), reports/invariant_dashboard.html
(--dashboard), inferred_rules.json (--rules) and full rule text (--output-descriptions).

    python -m tests.mutation_testing.invariant_inference_engine
    python -m tests.mutation_testing.invariant_inference_engine --snapshots-dir DIR --datasets a,b --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tests.mutation_testing.inference_config import InferenceConfig, load_config, with_overrides
from tests.mutation_testing.inference.facets import (
    CONTAINMENT_PAIRS,
    CORRELATION_PAIRS,
    FACETS,
    ORDERED_FACETS,
    TEXT_VIEWS,
    _facet_has_signal,
)
from tests.mutation_testing.inference.constants import (
    INFERRED_RULES_PATH,
    REPORTS_DIR,
    SNAPSHOTS_DIR,
    UNTAGGED_DATASET_NAME,
)
from tests.mutation_testing.inference.templates import RULES
from tests.mutation_testing.inference.categories import _active_tab, _round_trip_pairs, _tabbed_category
from tests.mutation_testing.inference.common import _enumerate_schema_keyword_paths
from tests.mutation_testing.inference.descriptions import _params_label, describe_rule, rule_statement
from tests.mutation_testing.inference.templates.change_count import change_count


# Numeric parameters live in inference_config.json (see inference_config.py).


@dataclass
class Finding:
    """One template's result for one candidate in one dataset."""
    template: str
    category: str
    params: dict  # identifies the candidate with template and category
    held: int  # observations where the positive statement held
    total: int  # applicable observations
    description: str
    expected: int | None = None  # change_count only: k in this dataset
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
        all_findings: list[Finding] = []
        dataset_count = len(self.snapshots_by_dataset)

        for dataset_index, (dataset, snapshots) in enumerate(self.snapshots_by_dataset.items(), 1):
            self.findings = []

            # Pair consecutive snapshots of the same run only.
            clean_pairs = [
                (p, c) for p, c in zip(snapshots[:-1], snapshots[1:])
                if p.get("run_id") is not None
                   and p.get("run_id") == c.get("run_id")
                   and c.get("step", 0) > p.get("step", -1)
            ]

            by_cat: dict[str, list] = defaultdict(list)
            for p, c in clean_pairs:
                by_cat[_tabbed_category(p, c)].append((p, c))

            for cat_index, (cat, cat_pairs) in enumerate(by_cat.items(), 1):
                if on_progress:
                    on_progress(dataset, dataset_index, dataset_count, cat_index, len(by_cat))
                if len(cat_pairs) < self.min_observations:
                    continue
                self._run_stability(cat, cat_pairs)
                self._run_change_count(cat, cat_pairs)
                self._run_order_stability(cat, cat_pairs)
                for a, b in CORRELATION_PAIRS:
                    for template in ("correlation", "path_key", "value_to_key"):
                        self._run_rule(template, cat, cat_pairs, {"a": a, "b": b}, self.cfg.min_rule_observations)
                for view in TEXT_VIEWS:
                    self._run_rule("text_store_agreement", cat, cat_pairs, {"view": view}, self.min_observations)
                    self._run_rule("format_agreement", cat, cat_pairs,
                                   {"view": view, "transition": True}, self.min_observations)
                self._run_rule("gui_validation_agreement", cat, cat_pairs,
                               {"transition": True}, self.min_observations)

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

            # State rules do not depend on the action, so they also run once over every pair.
            if len(clean_pairs) >= self.min_observations:
                for a, b in CONTAINMENT_PAIRS:
                    self._run_rule("key_containment", "*", clean_pairs, {"a": a, "b": b}, self.cfg.min_rule_observations)
                for view in TEXT_VIEWS:
                    self._run_rule("text_store_agreement", "*", clean_pairs, {"view": view}, self.min_observations)
                    self._run_rule("format_agreement", "*", clean_pairs,
                                   {"view": view, "transition": False}, self.min_observations)
                self._run_rule("gui_validation_agreement", "*", clean_pairs,
                               {"transition": False}, self.min_observations)

            self._deduplicate()
            for f in self.findings:
                f.dataset = dataset
            all_findings.extend(self.findings)

        self.findings = all_findings
        self._compute_promotions()
        return self.findings

    def _run_rule(self, template: str, cat: str, pairs: list, params: dict,
                  min_obs: int, record_params: dict | None = None,
                  expected: int | None = None) -> None:
        """Apply RULES[template] to every pair and record how often the positive statement held.
        record_params is the candidate identity when it differs from params (change_count: the facet).
        """
        rule = RULES[template]
        held = total = 0
        fail_ex: list[dict] = []
        hold_ex: list[dict] = []
        for prev, curr in pairs:
            for h, detail in rule(prev, curr, **params):
                total += 1
                ex = {"run_id": curr.get("run_id"), "step": curr.get("step"),
                      "action": curr.get("action"), **detail}
                if h:
                    held += 1
                    if len(hold_ex) < self.cfg.max_examples:
                        hold_ex.append(ex)
                elif len(fail_ex) < self.cfg.max_examples:
                    fail_ex.append(ex)

        if total < min_obs:
            return
        identity = record_params if record_params is not None else params
        self.findings.append(Finding(
            template=template,
            category=cat,
            params=identity,
            held=held,
            total=total,
            description=rule_statement(template, cat, params, positive=True),
            expected=expected,
            fail_examples=fail_ex,
            hold_examples=hold_ex,
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

    def _run_order_stability(self, cat: str, pairs: list) -> None:
        for facet, get_facet in ORDERED_FACETS.items():
            if _facet_has_signal(get_facet, pairs):
                self._run_rule("order_stability", cat, pairs, {"facet": facet}, self.min_observations)

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
                if "path" in p or p.get("facet") in ("schema.properties", "schema.required"):
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
                                         direction (for change_count, also the same k)
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
                    verdict = "BELOW_THRESHOLD"

            sample = group[0]
            expected = next(iter(constants)) if len(constants) == 1 else None
            eval_params = dict(sample.params)
            if sample.template == "change_count":
                eval_params["expected"] = expected
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

        self.promotions.sort(key=lambda p: (p.verdict != "PROMOTABLE", p.template, p.category,
                                            _params_label(p.template, p.params)))

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

    @staticmethod
    def _exportable(p: PromotionCandidate) -> bool:
        # "other" groups unrelated actions, so a rule over it says nothing about any one of them; an unknown tab cannot be matched.
        return (p.verdict == "PROMOTABLE" and not p.category.startswith("other@")
                and not p.category.endswith("@unknown"))

    def write_rules(self, path: str = INFERRED_RULES_PATH) -> int:
        """Write inferred_rules.json, which TemporalInvariantChecker evaluates."""
        from datetime import datetime
        rules = [p.rule for p in self.promotions if self._exportable(p)]
        Path(path).write_text(json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": str(self.dir),
            "datasets": [self._short_ds(d) for d in sorted(self.snapshots_by_dataset)],
            "threshold": self.cfg.promotion_threshold,
            "config": asdict(self.cfg),
            "rules": rules,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[Engine] {len(rules)} executable rules → {path}")
        return len(rules)

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
        print(f"\n{'='*70}")
        print(f"Invariant Inference Engine - {len(self.snapshots_by_dataset)} dataset(s)")
        print(f"{'='*70}")
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
            print(f"\n### Dataset: {dataset}  ({len(self.snapshots_by_dataset.get(dataset, []))} snapshots) ###")
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
        The dashboard groups the same way.
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
        total = sum(len(s) for s in self.snapshots_by_dataset.values())
        w("# Invariant Inference Report\n")
        w(f"Source: `{self.dir}` - {len(self.snapshots_by_dataset)} datasets, {total} snapshots\n")
        w("| Dataset | Snapshots |")
        w("|---|---|")
        for ds, snaps in sorted(self.snapshots_by_dataset.items()):
            w(f"| {self._short_ds(ds)} | {len(snaps)} |")
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
          f"steps it applied to in the Strenda dataset. Full rule definitions are in the dashboard "
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
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("\n".join(out), encoding="utf-8")
        print(f"[Engine] Readable report → {path}")

    def dashboard_payload(self) -> dict:
        """All data the dashboard shows; every number is computed here."""
        from datetime import datetime
        investigate_ids = {id(p) for p in self._investigate_candidates()}
        group_of = {"PROMOTABLE": "holds", "CONTRADICTORY_ACROSS_DATASETS": "contradictory",
                    "INSUFFICIENT_EVIDENCE": "insufficient"}
        candidates = []
        for p in self.promotions:
            group = "investigate" if id(p) in investigate_ids else group_of.get(p.verdict, "below")
            datasets = {}
            for ds, v in p.per_dataset.items():
                held, pct = self._held(p, v)
                datasets[self._short_ds(ds)] = {
                    "held": held, "m": v["m"], "pct": round(pct, 4),
                    "examples": self._counter_examples(p, v)[:self.cfg.max_examples] if held < v["m"] else [],
                }
            candidates.append({
                "template": p.template,
                "category": p.category,
                "group": group,
                "exported": self._exportable(p),
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
            "near_margin": self.cfg.dashboard_near_margin,
            "rules_file": INFERRED_RULES_PATH,
            "datasets": [{"name": self._short_ds(ds), "snapshots": len(s)}
                         for ds, s in sorted(self.snapshots_by_dataset.items())],
            "candidates": candidates,
        }

    def write_html_dashboard(self, path: str) -> None:
        from tests.mutation_testing.inference_dashboard import render_dashboard
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(render_dashboard(self.dashboard_payload()), encoding="utf-8")
        print(f"[Engine] HTML dashboard → {path}")


def main():
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
    parser.add_argument("--report", default=f"{REPORTS_DIR}/invariant_report.md",
                        help=f"Readable Markdown report (default: {REPORTS_DIR}/invariant_report.md)")
    parser.add_argument("--dashboard", default=f"{REPORTS_DIR}/invariant_dashboard.html",
                        help=f"Self-contained HTML dashboard (default: {REPORTS_DIR}/invariant_dashboard.html)")
    parser.add_argument("--output-descriptions", default=None,
                        help="Write full rule text for exported rules")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print the raw per-dataset dump to the console")
    args = parser.parse_args()

    cfg = with_overrides(load_config(args.config, args.profile),
                         min_observations=args.min_obs, promotion_threshold=args.threshold)
    engine = InvariantInferenceEngine(args.snapshots_dir, config=cfg)
    requested = [d.strip() for d in args.datasets.split(",")] if args.datasets else None
    engine.load_all(requested)
    engine.run()
    engine.print_report(verbose=args.verbose)
    engine.write_rules(args.rules)
    engine.write_readable_report(args.report)
    engine.write_html_dashboard(args.dashboard)

    if args.output_descriptions:
        descs = engine.generate_nl_descriptions()
        Path(args.output_descriptions).write_text("\n".join(descs), encoding="utf-8")
        print(f"[Engine] {len(descs)} rule descriptions → {args.output_descriptions}")


if __name__ == "__main__":
    main()