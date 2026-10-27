"""HTML report of the regression replays: how well they work on the hosted site and on the local build.

    python -m tests.cli regression-report [--runs-dir DIR] [--manifest FILE] [--out FILE]

Input: the regression_<time>.json files that `python -m tests.regression.run_regression` writes to
tests/output/regression_runs/ (one per invocation, with the result of every repeat). For every manifest entry and
environment the report shows the latest result found in those files, so running entries one by one still gives a
complete picture. The history table at the end lists every invocation.
Output: tests/output/reports/regression/regression_report_<time>.html (earlier reports are kept).
"""
from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.common.paths import REGRESSION_REPORT_DIR, REGRESSION_RUNS_DIR
from tests.mutation_testing.common.report_theme import DARK_VARS

MANIFEST = Path(__file__).resolve().parents[2] / "regression" / "manifest.json"
ENVIRONMENTS = ("hosted", "local")
HISTORY_LIMIT = 30

CSS = ":root{" + DARK_VARS + "}" + """
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 60px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 10px}
.sub{color:var(--mute);margin:0 0 22px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.card b{display:block;font-size:24px}.card span{color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:14px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:var(--chip);font-weight:600;font-size:12.5px;text-transform:uppercase;letter-spacing:.03em;color:var(--mute)}
tr:last-child td{border-bottom:0}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.tag{display:inline-block;background:var(--chip);border-radius:20px;padding:1px 9px;font-size:12px;margin:1px 2px 1px 0}
.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mute{color:var(--mute)}.scroll{overflow-x:auto}
tr.differ td:first-child{border-left:3px solid var(--warn)}
details{margin:6px 0}summary{cursor:pointer;font-weight:600}
@media print{.scroll{overflow:visible}}
"""

e = html.escape


_FILE_TIME = re.compile(r"_(\d{8}_\d{6})\.json$")
_FILE_REPEAT = re.compile(r"_r(\d+)_\d{8}_\d{6}\.json$")


def _from_old_format(rows: list, path: Path) -> dict:
    """A summary written before run_regression saved records: a bare list of rows, no per-repeat results. Time and
    repeat count come from the file name (regression_<apps>_r<N>_<YYYYmmdd_HHMMSS>.json)."""
    stamp = _FILE_TIME.search(path.name)
    when = datetime.strptime(stamp.group(1), "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S") if stamp else ""
    repeat = _FILE_REPEAT.search(path.name)
    return {"time": when, "git": {}, "repeat": int(repeat.group(1)) if repeat else None, "only": None,
            "rows": [r for r in rows if isinstance(r, dict) and "name" in r], "old_format": True}


def load_records(runs_dir: Path) -> list[dict]:
    """Every regression_*.json of the folder, oldest first. Files that cannot be read are skipped. Both the record
    format ({"time", "git", "repeat", "only", "rows"}) and the older bare list of rows are read."""
    records = []
    for path in runs_dir.glob("regression_*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, list):
            record = _from_old_format(record, path)
        if isinstance(record, dict) and isinstance(record.get("rows"), list):
            record["file"] = path.name
            records.append(record)
    records.sort(key=lambda r: (r.get("time") or "", r["file"]))   # oldest first, by run time rather than file name
    return records


def latest_rows(records: list[dict]) -> dict[tuple[str, str], dict]:
    """(entry name, environment) -> the newest row, with the time of the invocation it came from."""
    latest: dict[tuple[str, str], dict] = {}
    for record in records:   # oldest first, so later records overwrite earlier ones
        for row in record["rows"]:
            label = row.get("label") or row.get("app", "?")
            latest[(row["name"], label)] = {**row, "when": record.get("time", ""), "git": record.get("git", {})}
    return latest


def signature(rep: dict) -> tuple:
    return (rep.get("status"), tuple(rep.get("bugs", [])), tuple(rep.get("failed_invariants", [])))


def agreement(row: dict) -> tuple[int, int]:
    """(repeats that gave the most common result, number of repeats)."""
    reps = row.get("repeats") or []
    if not reps:
        return 0, 0
    return Counter(signature(r) for r in reps).most_common(1)[0][1], len(reps)


def describe(row: dict) -> str:
    """What the replays ended as, e.g. 'known-bugs BUG-04, BUG-10'."""
    reps = row.get("repeats") or []
    if not reps:
        return row.get("reason") or "no result"
    top = Counter(signature(r) for r in reps).most_common(1)[0][0]
    status, bugs, _ = top
    return status + (" " + ", ".join(bugs) if bugs else "")


def env_stats(rows: list[dict]) -> dict:
    flaky = [r for r in rows if agreement(r)[0] < agreement(r)[1]]
    diverged = [r for r in rows if any("diverged" in str(x.get("status")) for x in r.get("repeats") or [])]
    seconds = [x["seconds"] for r in rows for x in r.get("repeats") or [] if isinstance(x.get("seconds"), (int, float))]
    return {"entries": len(rows), "ok": sum(1 for r in rows if r.get("ok")),
            "failing": sum(1 for r in rows if not r.get("ok")), "flaky": len(flaky), "diverged": len(diverged),
            "replays": len(seconds), "avg": (sum(seconds) / len(seconds)) if seconds else 0.0, "total": sum(seconds)}


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min" if seconds >= 90 else f"{seconds:.0f} s"


def _cell(row: dict | None) -> str:
    if row is None:
        return "<td class=mute>not run</td>"
    good, total = agreement(row)
    css = "good" if row.get("ok") else "bad"
    verdict = "as expected" if row.get("ok") else e(row.get("reason") or "failed")
    secs = [x["seconds"] for x in row.get("repeats") or [] if isinstance(x.get("seconds"), (int, float))]
    timing = f" · {sum(secs) / len(secs):.0f} s" if secs else ""
    agree_css = "" if good == total else " class=warn"
    agree = (f"<span{agree_css}>{good}/{total} repeats agree</span>" if total else
             f"<span class=mute>{e(str(row.get('runs', '?')))} run(s), repeats not recorded (older summary)</span>")
    return (f"<td><span class={css}>{'✓' if row.get('ok') else '✗'} {e(describe(row))}</span><br>"
            f"<span class=mute>{verdict}</span><br>"
            f"{agree}<span class=mute>{timing} · expect {e(str(row.get('expect')))}</span></td>")


def _known_bug_ids() -> list[str]:
    try:
        from tests.mutation_testing.runs.known_bugs import load_known_bugs
        return [b["id"] for b in load_known_bugs()]
    except Exception:   # the report must still render when the triage config cannot be read
        return []


def render(records: list[dict], manifest_path: Path = MANIFEST) -> str:
    latest = latest_rows(records)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest.get("entries", [])
    except (OSError, ValueError):
        entries = []
    names = [en["name"] for en in entries] or sorted({n for n, _ in latest})
    envs = [env for env in ENVIRONMENTS if any(label == env for _, label in latest)]
    envs += sorted({label for _, label in latest} - set(ENVIRONMENTS))

    out = ["<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Regression Replay Report</title><style>{CSS}</style></head><body><main>",
           "<h1>Regression replays</h1>",
           f"<p class=sub>{len(records)} recorded invocation(s) · generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
           "each cell is the newest result of that entry on that app</p>"]
    if not records:
        out.append("<p>No regression runs recorded yet. Run <code>python -m tests.regression.run_regression --app both "
                   "--repeat 5</code> first.</p></main></body></html>")
        return "".join(out)

    # 1. Numbers per environment
    out.append("<h2>How well the replays work</h2><div class=scroll><table><tr><th>App</th><th class=n>Entries</th>"
               "<th class=n>As expected</th><th class=n>Failing</th><th class=n>Flaky</th><th class=n>Diverged</th>"
               "<th class=n>Replays</th><th class=n>Avg per replay</th><th>Last run</th></tr>")
    for env in envs:
        rows = [r for (n, label), r in latest.items() if label == env and n in names]
        s = env_stats(rows)
        when = max((r["when"] for r in rows), default="")
        git = next((r["git"] for r in rows if r["when"] == when and r.get("git")), {})
        commit = (git.get("commit", "") + ("+dirty" if git.get("dirty") else "")) if git else ""
        out.append(f"<tr><td><b>{e(env)}</b></td><td class=n>{s['entries']} of {len(names)}</td>"
                   f"<td class='n good'>{s['ok']}</td><td class='n {'bad' if s['failing'] else ''}'>{s['failing']}</td>"
                   f"<td class='n {'warn' if s['flaky'] else ''}'>{s['flaky']}</td>"
                   f"<td class='n {'warn' if s['diverged'] else ''}'>{s['diverged']}</td>"
                   f"<td class=n>{s['replays']}</td><td class=n>{_minutes(s['avg'])}</td>"
                   f"<td>{e(when)} <span class=mute>{e(commit)}</span></td></tr>")
    out.append("</table></div><p class=sub style='margin-top:8px'>Flaky: the repeats of an entry did not all end the same. "
               "Diverged: the replay left the recorded run, so the app behaves differently from when the sequence was recorded.</p>")

    # 2. Entry x environment
    out.append("<h2>Entry by app</h2><div class=scroll><table><tr><th>Entry</th><th>Guards</th>"
               + "".join(f"<th>{e(env)}</th>" for env in envs) + "<th>Same?</th></tr>")
    for name in names:
        rows = [latest.get((name, env)) for env in envs]
        present = [r for r in rows if r is not None]
        guards = next((r.get("guards") for r in present if r.get("guards")), None) or next(
            (en.get("bugs", []) for en in entries if en.get("name") == name), [])
        sigs = {(describe(r), bool(r.get("ok")), agreement(r)[0] == agreement(r)[1]) for r in present}
        differ = len(present) > 1 and len(sigs) > 1
        same = "<td class=mute>-</td>" if len(present) < 2 else (
            "<td class=warn>different</td>" if differ else "<td class=good>same</td>")
        out.append(f"<tr{' class=differ' if differ else ''}><td><b>{e(name)}</b></td>"
                   f"<td>{''.join(f'<span class=tag>{e(g)}</span>' for g in guards)}</td>"
                   + "".join(_cell(r) for r in rows) + same + "</tr>")
    out.append("</table></div>")

    # 3. Failures that no known bug explains
    unexplained: dict[str, set[str]] = {}
    for (name, env), row in latest.items():
        if name not in names:
            continue
        for rep in row.get("repeats") or []:
            for inv in rep.get("failed_invariants", []):
                unexplained.setdefault(inv, set()).add(f"{name} ({env})")
    out.append("<h2>Failures that no known bug explains</h2>")
    if unexplained:
        out.append("<p class=sub>An invariant that fails in a replay without belonging to a known bug fails the entry. "
                   "Attribute it to a bug in triage_config.json, or fix the invariant.</p><div class=scroll><table>"
                   "<tr><th>Invariant</th><th class=n>Entries</th><th>Where</th></tr>")
        for inv, where in sorted(unexplained.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            out.append(f"<tr><td><b>{e(inv)}</b></td><td class=n>{len(where)}</td>"
                       f"<td>{''.join(f'<span class=tag>{e(w)}</span>' for w in sorted(where))}</td></tr>")
        out.append("</table></div>")
    else:
        out.append("<p class=good>None.</p>")

    # 4. Which known bugs have a regression entry
    guarded = {b for en in entries for b in en.get("bugs", [])}
    known = _known_bug_ids()
    missing = [b for b in known if b not in guarded]
    out.append("<h2>Bug coverage</h2>")
    out.append(f"<p class=sub>{len([b for b in known if b in guarded])} of {len(known)} known bugs have a regression entry.</p>"
               if known else "<p class=sub>The known bugs could not be read.</p>")
    if missing:
        out.append("<p>Without a regression entry: " + "".join(f"<span class=tag>{e(b)}</span>" for b in missing) + "</p>")

    # 5. History
    out.append(f"<h2>History</h2><div class=scroll><table><tr><th>Time</th><th>Commit</th><th>Apps</th><th class=n>Repeats</th>"
               "<th>Entry filter</th><th>Result</th></tr>")
    for record in reversed(records[-HISTORY_LIMIT:]):
        git = record.get("git") or {}
        commit = (git.get("commit", "") + ("+dirty" if git.get("dirty") else "")) if git else ""
        per_env = {}
        for row in record["rows"]:
            label = row.get("label") or row.get("app", "?")
            ok, total = per_env.get(label, (0, 0))
            per_env[label] = (ok + (1 if row.get("ok") else 0), total + 1)
        result = " · ".join(f"<span class={'good' if ok == total else 'bad'}>{e(label)} {ok}/{total}</span>"
                            for label, (ok, total) in per_env.items())
        out.append(f"<tr><td>{e(record.get('time', ''))}</td><td>{e(commit)}</td><td>{e(', '.join(per_env))}</td>"
                   f"<td class=n>{record.get('repeat', '')}</td><td>{e(record.get('only') or 'all')}</td><td>{result}</td></tr>")
    out.append("</table></div></main></body></html>")
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="HTML report of the regression replays on the hosted site and the local build")
    ap.add_argument("--runs-dir", default=str(REGRESSION_RUNS_DIR),
                    help="Folder with the regression_<time>.json files (default: %(default)s)")
    ap.add_argument("--manifest", default=str(MANIFEST), help="The regression manifest (default: %(default)s)")
    ap.add_argument("--out", default=None, help="Output file (default: tests/output/reports/regression/"
                                                "regression_report_<timestamp>.html; earlier reports are kept)")
    args = ap.parse_args()
    page = render(load_records(Path(args.runs_dir)), Path(args.manifest))
    out = Path(args.out) if args.out else REGRESSION_REPORT_DIR / f"regression_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"[Regression] report saved: {out}")
    print(f"[Regression] open in browser: {out.resolve().as_uri()}")


if __name__ == "__main__":
    main()