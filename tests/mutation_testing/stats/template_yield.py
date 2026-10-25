"""What each template contributes through the whole funnel: candidates -> promotable -> exported -> reviewed -> accepted.
    python -m tests.cli stats yield [--candidates FILE] [--review-dir DIR] [--csv FILE] [--report FILE]

Does not mine. Reads the newest runs/<time>/candidates.json and the newest file in rule_reviews/, as
`infer --export-only` does. One row per template (all templates in RULES, also those without candidates), templates
with no accepted rule first. Precision = accepted / reviewed, trivial rate = trivial / reviewed ("-" if nothing was
reviewed). Learned-bug decisions count as reviewed only. Outputs (like the threshold sweep): console table, CSV and
Markdown (for the thesis) and an HTML page like the other stats reports, all in tests/output/reports/stats/.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter
from pathlib import Path

from tests.mutation_testing.common.paths import STATS_DIR
from tests.mutation_testing.inference.constants import INFERENCE_DIR, INFERENCE_RUNS_DIR
from tests.mutation_testing.inference.review_store import REVIEW_DIR, latest_review_file
from tests.mutation_testing.inference.templates import RULES
from tests.mutation_testing.stats.common import Report, Table, show_page, text_of, write_page

DECISIONS = ("accepted", "trivial", "rejected", "learned_bug")
HEADER = ["template", "candidates", "promotable", "exported", "reviewed", "accepted", "trivial", "rejected",
          "learned_bug", "precision", "trivial_rate"]
TITLES = ["Template", "Candidates", "Promotable", "Exported", "Reviewed", "Accepted", "Trivial", "Rejected",
          "Learned bug", "Precision", "Trivial rate"]
COUNTS = HEADER[1:9]


def newest_candidates(archive_dir: str) -> Path:
    runs = sorted(Path(archive_dir).glob("*/candidates.json"))
    if not runs:
        raise SystemExit(f"[Yield] no candidates.json under {archive_dir}; run the inference once (python -m tests.cli infer)")
    return runs[-1]


def _ratio(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "-"


def _decision(review: dict, rule_id) -> str | None:
    entry = review.get(rule_id)
    status = entry.get("status") if isinstance(entry, dict) else None
    return status if status in DECISIONS else None


def build_rows(payload: dict, review: dict) -> tuple[list[dict], Counter, Counter]:
    """(one dict per template, sorted; orphan decisions by status; review-file decisions by status)."""
    per: dict[str, Counter] = {t: Counter() for t in RULES}
    ids = set()
    for c in payload["candidates"]:
        row = per.setdefault(c["template"], Counter())
        rule_id = (c.get("rule") or {}).get("id")
        ids.add(rule_id)
        row["candidates"] += 1
        row["promotable"] += c.get("group") == "holds"
        row["exported"] += bool(c.get("exported"))
        status = _decision(review, rule_id)
        if status:
            row["reviewed"] += 1
            row[status] += 1
    orphans = Counter(_decision(review, rid) for rid in review if rid not in ids and _decision(review, rid))
    in_file = Counter(_decision(review, rid) for rid in review if _decision(review, rid))
    rows = [{"template": t, **{k: row[k] for k in COUNTS}} for t, row in per.items()]
    # No accepted rule first (most candidates first: the noisiest), then the least productive
    rows.sort(key=lambda r: (r["accepted"] > 0, r["accepted"], -r["candidates"], r["template"]))
    return rows, orphans, in_file


def _total(rows: list[dict]) -> dict:
    return {"template": "Total", **{k: sum(r[k] for r in rows) for k in COUNTS}}


def _cells(r: dict) -> list:
    return [r["template"], *(r[k] for k in COUNTS), _ratio(r["accepted"], r["reviewed"]),
            _ratio(r["trivial"], r["reviewed"])]


def _fmt(counter: Counter) -> str:
    return ", ".join(f"{k.replace('_', ' ')} {counter[k]}" for k in DECISIONS if counter[k]) or "none"


def checks(payload: dict, total: dict, orphans: Counter, in_file: Counter, engine_report: Path) -> list[str]:
    """Do the totals match what the review tool and the engine report print? One line each."""
    out = []
    n_file, n_table = sum(in_file.values()), total["reviewed"]
    if n_file == n_table:
        out.append(f"OK: review file {n_file} decisions ({_fmt(in_file)}) = table reviewed {n_table}.")
    else:
        out.append(f"DOES NOT MATCH: review file has {n_file} decisions ({_fmt(in_file)}), the table has {n_table} reviewed; "
                   f"the {sum(orphans.values())} other decision(s) are for rules that are not in this candidates.json.")
    try:
        text = engine_report.read_text(encoding="utf-8")
    except OSError:
        return out + [f"Not checked against the engine report ({engine_report} not found)."]
    found = {}
    for key, pattern in (("promotable", r"\*\*(\d+) candidate rules hold\*\*"), ("exported", r"(\d+) are exported"),
                         ("investigate", r"\*\*(\d+) worth investigating"), ("contradictory", r"\*\*(\d+) contradictory"),
                         ("insufficient", r"\*\*(\d+) not judged")):
        m = re.search(pattern, text)
        if m:
            found[key] = int(m.group(1))
    groups = Counter(c.get("group") for c in payload["candidates"])
    mine = {"promotable": total["promotable"], "exported": total["exported"], "investigate": groups["investigate"],
            "contradictory": groups["contradictory"], "insufficient": groups["insufficient"]}
    bad = [f"{k} {mine[k]} (report: {v})" for k, v in found.items() if mine[k] != v]
    if not found:
        out.append("Not checked against the engine report (its summary lines were not found).")
    elif bad:
        out.append("DOES NOT MATCH the engine report: " + "; ".join(bad) + ". The report may come from another mining run.")
    else:
        out.append(f"OK: promotable {total['promotable']} and exported {total['exported']} match the engine report"
                   + (" (also investigate, contradictory, not judged)." if len(found) == 5 else "."))
    return out


def write_csv(rows: list[dict], total: dict, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows([text_of(x) for x in _cells(r)] for r in rows + [total])
    print(f"[Yield] CSV -> {path}")


def write_markdown(rows: list[dict], total: dict, notes: list[str], orphan_line: str, sources: str, path: str) -> None:
    out = ["# Yield per template\n", sources + "\n",
           "| " + " | ".join(TITLES) + " |", "|" + "---|" * len(TITLES)]
    out += ["| " + " | ".join(text_of(x) for x in _cells(r)) + " |" for r in rows]
    out.append("| " + " | ".join(f"**{text_of(x)}**" for x in _cells(total)) + " |")
    out += ["",
            "Candidates: every candidate of the template in the mining. Promotable: holds in every dataset at the threshold. "
            "Exported: promotable and reviewable. Reviewed: has a decision. Precision = accepted / reviewed; trivial rate = "
            "trivial / reviewed; learned-bug decisions count as reviewed only. Templates without an accepted rule come first.\n",
            orphan_line + "\n", "Checks: " + " ".join(notes) + "\n"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(out), encoding="utf-8")
    print(f"[Yield] Markdown report -> {path}")


def main() -> None:
    stamp = time.strftime("%Y%m%d_%H%M%S")   # every run keeps its own files, as in the threshold sweep
    parser = argparse.ArgumentParser(description="Yield per template through the inference funnel")
    parser.add_argument("--archive-dir", default=INFERENCE_RUNS_DIR, help="Where the inference runs are (newest candidates.json is used)")
    parser.add_argument("--candidates", default=None, help="A specific candidates.json (default: the newest run)")
    parser.add_argument("--review-dir", default=REVIEW_DIR, help="Folder with the review files (newest is used)")
    parser.add_argument("--engine-report", default=f"{INFERENCE_DIR}/invariant_report.md",
                        help="Engine report to check the totals against (default: %(default)s)")
    parser.add_argument("--csv", default=f"{STATS_DIR.as_posix()}/template_yield_{stamp}.csv", help="Output CSV (default: %(default)s)")
    parser.add_argument("--report", default=f"{STATS_DIR.as_posix()}/template_yield_{stamp}.md",
                        help="Output Markdown table (default: %(default)s)")
    parser.add_argument("--html", default=f"{STATS_DIR.as_posix()}/template_yield_{stamp}.html",
                        help="Output report page (default: %(default)s)")
    parser.add_argument("--no-open", action="store_true", help="Do not open the report page in the browser")
    args = parser.parse_args()

    cand_file = Path(args.candidates) if args.candidates else newest_candidates(args.archive_dir)
    payload = json.loads(cand_file.read_text(encoding="utf-8"))
    review_file = latest_review_file(args.review_dir)
    review = json.loads(review_file.read_text(encoding="utf-8")) if review_file else {}
    sources = f"Candidates: `{cand_file}` (mined {payload.get('generated', '?')}). Review: `{review_file or 'none'}`."

    rows, orphans, in_file = build_rows(payload, review)
    total = _total(rows)
    notes = checks(payload, total, orphans, in_file, Path(args.engine_report))
    orphan_line = (f"Decisions for rules that are no longer in this candidates.json: {sum(orphans.values())}"
                   f" ({_fmt(orphans)}); they are not in the table.")

    table = Table("Yield per template", TITLES, [_cells(r) for r in rows], total=_cells(total),
                  intro="Templates without an accepted rule come first (orange); click a heading to sort. Precision = accepted / "
                        "reviewed, trivial rate = trivial / reviewed; learned-bug decisions count as reviewed only.",
                  flag=lambda row: row[5] == 0)
    page = Report(name="template_yield", title="Yield per template", subtitle=sources.replace("`", ""),
                  intro="What each template contributes through the funnel: candidates, promotable, exported, reviewed, accepted.",
                  tables=[table], notes=[orphan_line] + notes)
    write_csv(rows, total, args.csv)
    write_markdown(rows, total, notes, orphan_line, sources, args.report)
    show_page(write_page(page, args.html), args.no_open)


if __name__ == "__main__":
    main()