"""Threshold sensitivity sweep for the inference engine's promotion_threshold (inference_config.json).

The templates run once, since only _compute_promotions() depends on the threshold. The sweep then
calls the engine's own _compute_promotions() once per threshold with engine.cfg.promotion_threshold
swapped.

For each threshold it reports:
- how many candidates are PROMOTABLE and how many of those are exported as executable rules
  ("other@" and "@unknown" categories are not exported, see InvariantInferenceEngine._exportable)
- the exported rules per template
- BELOW_THRESHOLD, CONTRADICTORY_ACROSS_DATASETS and INSUFFICIENT_EVIDENCE counts
- which rule ids enter or leave the exported set compared with the previous (lower) threshold

Outputs: a console table always; reports/threshold_sweep.csv (--csv), reports/threshold_sweep.md
(--report) and reports/threshold_sweep.png (--figure, needs matplotlib).

The sweep range (sweep_start, sweep_end, sweep_step) comes from inference_config.json; the flags
override it.

    python -m tests.mutation_testing.threshold_sweep
    python -m tests.mutation_testing.threshold_sweep --profile strict
    python -m tests.mutation_testing.threshold_sweep --snapshots-dir DIR --datasets a,b
    python -m tests.mutation_testing.threshold_sweep --start 0.70 --end 1.00 --step 0.05
    python -m tests.mutation_testing.threshold_sweep --thresholds 0.7,0.8,0.85,0.9,0.95,1.0
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

from tests.mutation_testing.inference.constants import REPORTS_DIR, SNAPSHOTS_DIR
from tests.mutation_testing.inference_config import load_config, with_overrides
from tests.mutation_testing.invariant_inference_engine import InvariantInferenceEngine, PromotionCandidate


# Plot and formatting constants; they do not affect results.
FIGURE_SIZE = (7, 4.5)
FIGURE_DPI = 150
LEGEND_FONT_SIZE = 8
THRESHOLD_DECIMALS = 6  # rounding for generated thresholds (float drift)
COLUMN_WIDTH = 14  # console table
BAR_WIDTH = 24  # progress bar


class _Progress:
    """Progress display for the slow part. On a terminal it redraws one line with a bar; otherwise it
    prints one line per dataset. Writes to the real stdout because the engine's own output is muted.
    """

    def __init__(self, out) -> None:
        self.out = out
        self.tty = out.isatty()
        self.width = 0
        self.last_dataset: str | None = None

    def __call__(self, dataset: str, ds_i: int, ds_n: int, cat_i: int, cat_n: int) -> None:
        name = InvariantInferenceEngine._short_ds(dataset)
        if self.tty:
            filled = BAR_WIDTH * cat_i // cat_n
            line = (f"[Sweep] Running templates: dataset {ds_i}/{ds_n} {name}  "
                    f"[{'#' * filled}{'-' * (BAR_WIDTH - filled)}] {cat_i}/{cat_n} action categories")
            self.width = max(self.width, len(line))
            self.out.write("\r" + line.ljust(self.width))
            self.out.flush()
        elif dataset != self.last_dataset:
            self.out.write(f"[Sweep] Running templates: dataset {ds_i}/{ds_n} {name}\n")
            self.out.flush()
        self.last_dataset = dataset

    def finish(self) -> None:
        if self.tty:
            self.out.write("\r" + " " * self.width + "\r")
            self.out.flush()


def _rule_label(engine: InvariantInferenceEngine, p: PromotionCandidate) -> str:
    """id [template] statement (per-dataset support)"""
    support = " · ".join(f"{engine._short_ds(ds)} {engine._held(p, v)[1]:.0%}"
                         for ds, v in sorted(p.per_dataset.items()))
    return f"{p.rule['id']} [{p.template}] {p.nl_description}  ({support})"


def sweep_thresholds(engine: InvariantInferenceEngine, thresholds: list[float]) -> list[dict]:
    """Re-classify engine.findings at each threshold with the engine's _compute_promotions().
    Restores engine.cfg and engine.promotions afterwards.
    """
    original = engine.cfg
    rows: list[dict] = []
    previous_ids: set[str] = set()
    labels: dict[str, str] = {}
    try:
        for t in sorted(thresholds):
            engine.cfg = replace(original, promotion_threshold=t)
            engine._compute_promotions()

            verdicts = Counter(p.verdict for p in engine.promotions)
            exported = [p for p in engine.promotions if engine._exportable(p)]
            ids = {p.rule["id"] for p in exported}
            for p in exported:
                labels[p.rule["id"]] = _rule_label(engine, p)

            rows.append({
                "threshold": t,
                "promotable": verdicts["PROMOTABLE"],
                "exported": len(exported),
                "by_template": Counter(p.template for p in exported),
                "below": verdicts["BELOW_THRESHOLD"],
                "contradictory": verdicts["CONTRADICTORY_ACROSS_DATASETS"],
                "insufficient": verdicts["INSUFFICIENT_EVIDENCE"],
                "total_candidates": len(engine.promotions),
                "lost": sorted(labels[i] for i in previous_ids - ids),
                "gained": sorted(labels[i] for i in ids - previous_ids) if rows else [],
            })
            previous_ids = ids
    finally:
        engine.cfg = original
        engine._compute_promotions()  # restore the engine state
    return rows


def print_sweep(rows: list[dict]) -> None:
    templates = sorted({t for r in rows for t in r["by_template"]})
    header = ["threshold", "promotable", "exported", *templates, "below", "contradictory", "insufficient"]
    print("  ".join(f"{h:>{COLUMN_WIDTH}}" for h in header))
    for r in rows:
        cells = [f"{r['threshold']:.2f}", r["promotable"], r["exported"],
                 *(r["by_template"].get(t, 0) for t in templates),
                 r["below"], r["contradictory"], r["insufficient"]]
        print("  ".join(f"{c:>{COLUMN_WIDTH}}" for c in cells))

    print(f"\n(insufficient={rows[0]['insufficient']} should stay constant across every row -- "
          f"it's decided by observation counts, not by the support threshold)")

    print("\nRules that enter/leave the exported set as the threshold rises:")
    for r in rows[1:]:
        if r["lost"]:
            print(f"\n  {r['threshold']:.2f}  lost ({len(r['lost'])})")
            for label in r["lost"]:
                print(f"    - {label}")
        if r["gained"]:
            # can happen when a candidate that was contradictory at a lower threshold becomes promotable
            print(f"\n  {r['threshold']:.2f}  gained ({len(r['gained'])})")
            for label in r["gained"]:
                print(f"    + {label}")


def write_csv(rows: list[dict], path: str) -> None:
    templates = sorted({t for r in rows for t in r["by_template"]})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["threshold", "promotable", "exported", *templates,
                    "below", "contradictory", "insufficient", "total_candidates",
                    "lost_vs_previous", "gained_vs_previous"])
        for r in rows:
            w.writerow([r["threshold"], r["promotable"], r["exported"],
                        *(r["by_template"].get(t, 0) for t in templates),
                        r["below"], r["contradictory"], r["insufficient"], r["total_candidates"],
                        len(r["lost"]), len(r["gained"])])
    print(f"[Sweep] CSV -> {path}")


def write_markdown(rows: list[dict], path: str, engine: InvariantInferenceEngine) -> None:
    templates = sorted({t for r in rows for t in r["by_template"]})
    out: list[str] = []
    w = out.append
    w("# Threshold Sensitivity Sweep\n")
    total = sum(len(s) for s in engine.snapshots_by_dataset.values())
    w(f"Source: `{engine.dir}` -- {len(engine.snapshots_by_dataset)} datasets, {total} snapshots, "
      f"{rows[0]['total_candidates']} candidates.\n")
    w("Each row re-classifies the SAME candidates (same held/total counts per dataset) "
      "against a different per-dataset support cutoff, using the engine's own "
      "`_compute_promotions()`. Only the classification changes -- the underlying "
      "observations do not.\n")

    header = ["Threshold", "Promotable", "Exported", *templates, "Below", "Contradictory", "Insufficient"]
    w("| " + " | ".join(header) + " |")
    w("|" + "---|" * len(header))
    for r in rows:
        cells = [f"{r['threshold']:.0%}", str(r["promotable"]), str(r["exported"]),
                 *(str(r["by_template"].get(t, 0)) for t in templates),
                 str(r["below"]), str(r["contradictory"]), str(r["insufficient"])]
        w("| " + " | ".join(cells) + " |")
    w("")
    w(f"`insufficient` should be constant across every row ({rows[0]['insufficient']} here) -- it is "
      "decided by observation counts (missing/thin datasets), not by the support threshold. If it "
      "isn't constant, something is wrong with the sweep, not the engine.\n")

    if any(r["lost"] or r["gained"] for r in rows):
        w("## Rules that enter/leave the exported set\n")
        for r in rows[1:]:
            if r["lost"]:
                w(f"**Lost at {r['threshold']:.0%}** ({len(r['lost'])}):\n")
                for label in r["lost"]:
                    w(f"- {label}")
                w("")
            if r["gained"]:
                w(f"**Gained at {r['threshold']:.0%}** ({len(r['gained'])}):\n")
                for label in r["gained"]:
                    w(f"- {label}")
                w("")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(out), encoding="utf-8")
    print(f"[Sweep] Markdown report -> {path}")


def write_figure(rows: list[dict], path: str, chosen: float) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[Sweep] matplotlib not installed -- skipping figure (pip install matplotlib to enable)")
        return
    thresholds = [r["threshold"] for r in rows]
    fig, ax = plt.subplots(figsize=FIGURE_SIZE)
    ax.plot(thresholds, [r["promotable"] for r in rows], marker="o", label="Promotable")
    ax.plot(thresholds, [r["exported"] for r in rows], marker="s", label="Exported (executable rules)")
    ax.plot(thresholds, [r["below"] for r in rows], marker="^", label="Below threshold")
    ax.plot(thresholds, [r["contradictory"] for r in rows], marker="x", label="Contradictory")
    ax.axvline(chosen, color="gray", linestyle="--", linewidth=1,
               label=f"Chosen threshold ({chosen:.0%})")
    ax.set_xlabel("Per-dataset support threshold")
    ax.set_ylabel("Number of candidates")
    ax.set_title("Threshold sensitivity: candidate classification vs. support cutoff")
    ax.legend(fontsize=LEGEND_FONT_SIZE)
    fig.tight_layout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[Sweep] Figure -> {path}")


def _parse_thresholds(args, cfg) -> list[float]:
    if args.thresholds:
        return [float(t.strip()) for t in args.thresholds.split(",")]
    start = args.start if args.start is not None else cfg.sweep_start
    end = args.end if args.end is not None else cfg.sweep_end
    step = args.step if args.step is not None else cfg.sweep_step
    n_steps = round((end - start) / step)  # index-based to avoid float drift and keep the endpoint
    return [round(start + i * step, THRESHOLD_DECIMALS) for i in range(n_steps + 1)]


def main():
    parser = argparse.ArgumentParser(description="Threshold sensitivity sweep for the invariant inference engine")
    parser.add_argument("--snapshots-dir", default=SNAPSHOTS_DIR,
                        help="Directory of per-dataset {dataset}.jsonl files")
    parser.add_argument("--datasets", default=None,
                        help="Comma-separated subset of datasets to load (default: all discovered)")
    parser.add_argument("--min-obs", type=int, default=None,
                        help="Override min_observations from the config")
    parser.add_argument("--config", default=None,
                        help="Parameter file (default: inference_config.json next to the engine)")
    parser.add_argument("--profile", default=None,
                        help="Profile inside the parameter file (default: 'default')")
    parser.add_argument("--start", type=float, default=None, help="Override sweep_start")
    parser.add_argument("--end", type=float, default=None, help="Override sweep_end")
    parser.add_argument("--step", type=float, default=None, help="Override sweep_step")
    parser.add_argument("--thresholds", default=None,
                        help="Explicit comma-separated thresholds, overrides --start/--end/--step, "
                             "e.g. 0.7,0.8,0.85,0.9")
    parser.add_argument("--csv", default=f"{REPORTS_DIR}/threshold_sweep.csv")
    parser.add_argument("--report", default=f"{REPORTS_DIR}/threshold_sweep.md")
    parser.add_argument("--figure", default=f"{REPORTS_DIR}/threshold_sweep.png")
    args = parser.parse_args()

    cfg = with_overrides(load_config(args.config, args.profile), min_observations=args.min_obs)
    engine = InvariantInferenceEngine(args.snapshots_dir, config=cfg)
    requested = [d.strip() for d in args.datasets.split(",")] if args.datasets else None
    thresholds = _parse_thresholds(args, cfg)

    out = sys.stdout  # real stdout; the engine's own log lines stay muted
    progress = _Progress(out)
    started = time.perf_counter()
    print("[Sweep] Loading traces ...", flush=True)
    with contextlib.redirect_stdout(io.StringIO()):
        loaded = engine.load_all(requested)
    print(f"[Sweep] Loaded {len(loaded)} dataset(s), {sum(loaded.values())} snapshots", flush=True)
    with contextlib.redirect_stdout(io.StringIO()):
        engine.run(on_progress=progress)  # computes engine.findings once
    progress.finish()
    print(f"[Sweep] Templates done in {time.perf_counter() - started:.1f}s; "
          f"re-classifying at {len(thresholds)} thresholds ...", flush=True)
    rows = sweep_thresholds(engine, thresholds)
    print()

    print_sweep(rows)
    write_csv(rows, args.csv)
    write_markdown(rows, args.report, engine)
    write_figure(rows, args.figure, cfg.promotion_threshold)


if __name__ == "__main__":
    main()