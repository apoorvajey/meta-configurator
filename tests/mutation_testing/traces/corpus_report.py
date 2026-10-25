"""HTML report of the snapshot corpus: which runs are in tests/output/traces/ and what was done to them.

Shows the time of the first and last run, totals per dataset, one row per run (when it ran, snapshots, steps, action
profile, failing steps, whether a replay re-recorded it) and the archived corpora. Written to
reports/corpus/corpus_report_<timestamp>.html; earlier reports are kept. For a run that a complete replay re-recorded,
"Failing steps" reads "original -> replay" (from replays/<replay_id>/comparison.json) and the run counts as clean or
failing by the replay result, because replay does not rewrite history.json.
"""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.common.report_theme import DARK_VARS
from tests.mutation_testing.runs.run_layout import RUNS_DIR as _RUNS, find_run_dir, iter_run_dirs
from tests.mutation_testing.runs.run_layout import CLEAN_STATUSES, RUNS_DIR as _RUNS, find_run_dir, iter_run_dirs
from tests.mutation_testing.runs.run_status import MARKERS

from tests.mutation_testing.common.paths import CORPUS_DIR as _CORPUS, TRACES_DIR as _TRACES

TRACES_DIR = _TRACES.as_posix()
RUNS_DIR = str(_RUNS)
CORPUS_DIR = _CORPUS.as_posix()
RUN_ID_FORMAT = "%Y%m%d_%H%M%S"


def _when(run_id: str) -> datetime | None:
    try:
        return datetime.strptime(run_id[:15], RUN_ID_FORMAT)
    except ValueError:
        return None


def _category(action: str) -> str:
    try:
        from tests.mutation_testing.inference.categories import _action_category
        return _action_category(action or "")
    except Exception:
        return "other"


def _history_info(runs_dir: Path, run_id: str) -> dict:
    path = (find_run_dir(run_id, runs_dir) or runs_dir / run_id) / "history.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    status = next((k for k, m in MARKERS.items() if (path.parent / m).is_file()), "")
    steps = raw.get("steps", []) if isinstance(raw, dict) else raw
    meta = (raw.get("meta", {}) or {}) if isinstance(raw, dict) else {}
    settings = meta.get("resolved_settings", {}) or {}
    failing = sum(1 for s in steps if s.get("action_succeeded") and s.get("passed") is False)
    errors = sum(1 for s in steps if not s.get("action_succeeded"))
    return {"status": status, "steps": len(steps), "failing_steps": failing, "action_errors": errors,
            "profile": settings.get("weights_profile_file") or "", "n_steps": settings.get("n_steps")}


def _replay_results(runs_dir: str) -> dict[str, dict]:
    """Newest complete replay per run id that replaced the run's trace: {run_id: {"orig": n, "now": n, "at": time}}.

    Read from tests/output/runs/<run_id>/replays/<replay_id>/comparison.json (written by every complete replay); a later replay wins.
    """
    found: dict[str, dict] = {}
    files = [p for d in iter_run_dirs(Path(runs_dir)) for p in sorted(d.glob("replays/*/comparison.json"))]
    for p in files:
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            if not c.get("complete") or "removed" not in (c.get("trace") or {}):
                continue
            found[str(c["run_id"])] = {"orig": c["original"]["failing_steps"], "now": c["replay"]["failing_steps"],
                                       "at": c.get("compared_at", "")}
        except Exception:
            continue
    return found


def _state(r: dict) -> str:
    """unfinished (no history.json), clean, known-bugs or failures - judged by the replay result when there is one."""
    if r.get("failing_now") is None:
        return "unfinished"
    if r.get("replay_result"):
        return "clean" if r["failing_now"] == 0 else "failures"
    if r.get("status") == "known-bugs":
        return "known-bugs"
    return "clean" if r.get("status") in CLEAN_STATUSES else "failures"


def collect(traces_dir: str, runs_dir: str = RUNS_DIR) -> dict:
    root, hist = Path(traces_dir), Path(runs_dir)
    replays = _replay_results(runs_dir)
    datasets: dict[str, dict] = {}
    for p in sorted(root.glob("*.jsonl")) if root.exists() else []:
        runs: dict[str, dict] = {}
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    snap = json.loads(line)
                except Exception:
                    continue
                if snap.get("baseline"):   # the state a repair left behind: no step of its own
                    continue
                rid = str(snap.get("run_id"))
                r = runs.setdefault(rid, {"snapshots": 0, "last_step": 0, "replayed": False, "cats": Counter()})
                r["snapshots"] += 1
                r["last_step"] = max(r["last_step"], snap.get("step") or 0)
                r["replayed"] = r["replayed"] or bool(snap.get("replayed_at"))
                r["cats"][_category(snap.get("action", ""))] += 1
        for rid, r in runs.items():
            r.update(_history_info(hist, rid))
            r["when"] = _when(rid)
            r["replay_result"] = replays.get(rid)
            r["failing_now"] = r["replay_result"]["now"] if r["replay_result"] else r.get("failing_steps")
        datasets[p.stem] = {"file": p.name, "runs": runs, "snapshots": sum(r["snapshots"] for r in runs.values())}

    log = []
    log_path = root / "corpus_log.log"
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                log.append(json.loads(line))
            except Exception:
                pass
    archives = []
    for m in sorted((root / "archive").glob("*/manifest.json")) if (root / "archive").exists() else []:
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            continue
        archives.append({"folder": m.parent.name, "label": data.get("label"), "at": data.get("archived_at"),
                         "files": data.get("files", {})})
    return {"datasets": datasets, "log": log, "archives": archives}


CSS = ":root{" + DARK_VARS + "}" + """
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 60px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 10px}
.sub{color:var(--mute);margin:0 0 22px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.card b{display:block;font-size:24px}.card span{color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:14px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:var(--chip);font-weight:600;font-size:12.5px;text-transform:uppercase;letter-spacing:.03em;color:var(--mute)}
tr:last-child td{border-bottom:0}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.tag{display:inline-block;background:var(--chip);border-radius:20px;padding:1px 9px;font-size:12px;margin:1px 2px 1px 0}
.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mute{color:var(--mute)}.scroll{overflow-x:auto}
details{margin:6px 0}summary{cursor:pointer;font-weight:600}
@media print{.scroll{overflow:visible}}
"""


def rp_for(r: dict):
    return r.get("replay_result")


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


COPY_JS = """
function fb(t,d){var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();
try{document.execCommand('copy');d()}catch(e){}a.remove()}
document.addEventListener('click',function(e){var b=e.target.closest('button[data-text]');if(!b)return;
var t=b.dataset.text,o=b.textContent,d=function(){b.textContent='Copied \\u2713';setTimeout(function(){b.textContent=o},1300)};
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(d,function(){fb(t,d)})}else{fb(t,d)}});
"""
SORT_JS = """
document.querySelectorAll('table.sortable').forEach(function(t){var hs=t.querySelectorAll('th');
hs.forEach(function(h,i){h.style.cursor='pointer';h.title='Click to sort';h.addEventListener('click',function(){
var asc=h.dataset.dir!=='asc';hs.forEach(function(x){x.dataset.dir='';x.classList.remove('asc','desc')});
h.dataset.dir=asc?'asc':'desc';h.classList.add(asc?'asc':'desc');
var rows=[].slice.call(t.querySelectorAll('tr')).slice(1);
var v=function(r){var c=r.children[i];return c?(c.dataset.v!==undefined?c.dataset.v:c.textContent.trim()):''};
rows.sort(function(a,b){var x=v(a),y=v(b),nx=parseFloat(x),ny=parseFloat(y);
var c=(!isNaN(nx)&&!isNaN(ny)&&/^-?[0-9.]+$/.test(x)&&/^-?[0-9.]+$/.test(y))?nx-ny:x.localeCompare(y);return asc?c:-c});
rows.forEach(function(r){t.appendChild(r)})})})});
"""
BUTTON_CSS = "th.asc:after{content:' \\25B2'}th.desc:after{content:' \\25BC'}th{user-select:none}a.lg{color:var(--accent)}.cmd{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0 0}"
BUTTON_CSS += "button[data-text]{font:inherit;font-size:12px;padding:2px 8px;border:1px solid var(--line);border-radius:6px;background:var(--chip);color:var(--ink);cursor:pointer}button[data-text]:hover{border-color:var(--accent)}"


def render(data: dict, traces_dir: str, runs_dir: str = RUNS_DIR) -> str:
    e = html.escape
    datasets = data["datasets"]
    all_runs = [(ds, rid, r) for ds, d in datasets.items() for rid, r in d["runs"].items()]
    times = [r["when"] for _, _, r in all_runs if r["when"]]
    first, last = (min(times), max(times)) if times else (None, None)
    total_snaps = sum(d["snapshots"] for d in datasets.values())
    replayed = sum(1 for _, _, r in all_runs if r["replayed"])

    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Snapshot Corpus Report</title><style>{CSS}{BUTTON_CSS}</style></head><body><main>",
           "<h1>Snapshot corpus</h1>",
           f"<p class=sub>Folder <code>{e(traces_dir)}</code> · generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>",
           "<div class=cards>"]
    for value, label in ((_fmt(first), "first run"), (_fmt(last), "last run"),
                         (str(len(all_runs)), "runs"), (f"{total_snaps:,}", "snapshots"),
                         (str(len(datasets)), "datasets"), (str(replayed), "runs re-recorded by replay")):
        out.append(f"<div class=card><b>{e(value)}</b><span>{label}</span></div>")
    out.append("</div>")
    if first and last:
        out.append(f"<p class=sub style='margin-top:10px'>The corpus spans {last - first}.</p>")

    states = Counter(_state(r) for _, _, r in all_runs)
    n_unfinished, n_clean, n_other = states["unfinished"], states["clean"], states["failures"]
    n_known = states["known-bugs"]
    out.append(f"<p class=sub style='margin-top:6px'><b>{len(all_runs)} runs:</b> {n_other} with failures or incomplete, "
               f"{n_clean} clean, {n_known} with only known bugs, {n_unfinished} unfinished (no history.json). "
               "A replayed run counts by its replay result.</p>")
    if n_unfinished:
        drop = "python -m tests.cli archive --drop-incomplete"
        out.append(f"<div class=cmd><b>Unfinished runs</b><code>{e(drop)}</code>"
                   f"<button data-text=\"{e(drop, quote=True)}\">Copy drop command</button></div>"
                   "<p class=sub style='margin:4px 0 0'>These runs crashed or were stopped before finishing. "
                   "Add <code>--dry-run</code> to preview. Removed snapshots are moved to <code>archive/_dropped</code>, not deleted.</p>")
    if first and last:
        rep = (f"python -m tests.cli report --since {min(rid for _, rid, _ in all_runs)} "
               f"--until {max(rid for _, rid, _ in all_runs)}")
        out.append(f"<div class=cmd><b>Report on these runs</b><code>{e(rep)}</code>"
                   f"<button data-text=\"{e(rep, quote=True)}\">Copy report command</button></div>"
                   "<p class=sub style='margin:4px 0 0'>Covers every recorded run between the first and last run id, "
                   "including runs that are no longer in the traces.</p>")

    out.append("<h2>Datasets</h2><div class=scroll><table class=sortable><tr><th>Dataset<th class=n>Runs<th class=n>Snapshots"
               "<th>First run<th>Last run<th class=n>Replayed</tr>")
    for ds, d in datasets.items():
        ts = [r["when"] for r in d["runs"].values() if r["when"]]
        out.append(f"<tr><td>{e(ds)}<td class=n>{len(d['runs'])}<td class=n>{d['snapshots']:,}"
                   f"<td>{_fmt(min(ts) if ts else None)}<td>{_fmt(max(ts) if ts else None)}"
                   f"<td class=n>{sum(1 for r in d['runs'].values() if r['replayed'])}</tr>")
    out.append("</table></div>")

    out.append("<h2>Runs</h2><p class=sub>Click a column heading to sort.</p><div class=scroll><table class=sortable><tr>"
               "<th>Started<th>Run id<th>Dataset<th>Profile<th class=n>Snapshots<th class=n>Last step"
               "<th class=n>Failing steps<th>Origin<th>History folder<th>Mutation log<th>Replay<th>Actions</tr>")
    for ds, rid, r in sorted(all_runs, key=lambda t: (t[2]["when"] or datetime.min, t[1])):
        origin = ("<span class=good>re-recorded by replay</span>" if r["replayed"] and rp_for(r) else
                  "<span class=warn>re-recorded (no comparison saved)</span>" if r["replayed"] else
                  "<span class=mute>original</span>")
        fails, rp = r.get("failing_now"), r.get("replay_result")
        if fails is None:
            fail_cell = "<td class='n mute' data-v=-1>no history"
        elif rp:
            fail_cell = (f"<td class='n {'bad' if fails else 'good'}' data-v={fails} "
                         f"title='original run: {rp['orig']}, replay: {fails}'>{rp['orig']} &rarr; {fails}")
        else:
            fail_cell = f"<td class='n {'bad' if fails else 'good'}'>{fails}"
        if fails is None:
            hist_cell, replay_cell = "<td class=mute>-", "<td class=mute>no history"
        else:
            folder = find_run_dir(rid, Path(runs_dir)) or Path(runs_dir) / rid
            cmd = f"python -m tests.cli replay --history {Path(runs_dir).as_posix()}/{rid}"   # by run id only
            hist_cell = f"<td><code>{e(folder.as_posix())}</code>"
            replay_cell = f"<td><button data-text=\"{e(cmd, quote=True)}\">Copy replay command</button>"
        log_file = (find_run_dir(rid, Path(runs_dir)) or Path(runs_dir) / rid) / "mutation_log.html"
        log_cell = (f"<td data-v=a><a class=lg href=\"{e(log_file.resolve().as_uri(), quote=True)}\" target=_blank>Open log</a>"
                    if log_file.is_file() else "<td class=mute data-v=z>no log")
        cats = " ".join(f"<span class=tag>{e(c)} {n}</span>" for c, n in r["cats"].most_common(6))
        out.append(f"<tr><td data-v=\"{e(rid)}\">{_fmt(r['when'])}<td><code>{e(rid)}</code><td>{e(ds)}"
                   f"<td>{e(str(r.get('profile') or ''))}<td class=n>{r['snapshots']}<td class=n>{r['last_step']}"
                   f"{fail_cell}<td>{origin}{hist_cell}{log_cell}{replay_cell}<td>{cats}</tr>")
    out.append("</table></div>")

    out.append("</table></div>")

    out.append("<h2>Archived corpora</h2>")

    if data["archives"]:
        out.append("<div class=scroll><table><tr><th>Archived<th>Label<th>Folder<th class=n>Runs<th class=n>Snapshots</tr>")
        for a in reversed(data["archives"]):
            runs = sum(v.get("runs", 0) for v in a["files"].values())
            snaps = sum(v.get("snapshots", 0) for v in a["files"].values())
            out.append(f"<tr><td>{e(str(a['at'] or ''))}<td>{e(str(a['label'] or ''))}<td><code>{e(a['folder'])}</code>"
                       f"<td class=n>{runs}<td class=n>{snaps:,}</tr>")
        out.append("</table></div>")
    else:
        out.append("<p class=mute>No archive yet. Create one with <code>python -m tests.cli archive</code>.</p>")
    out.append(f"</main><script>{COPY_JS}{SORT_JS}</script></body></html>")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="HTML report of the snapshot corpus")
    ap.add_argument("--traces-dir", default=TRACES_DIR, help="Folder with the recorded snapshot traces (default: %(default)s)")
    ap.add_argument("--runs-dir", default=RUNS_DIR,
                    help="Folder with the runs: histories, logs and replays (tests/output/runs/<run_id>/)")
    ap.add_argument("--out", default=None,
                    help="Output file (default: tests/output/reports/corpus/corpus_report_<timestamp>.html; earlier reports are kept)")
    args = ap.parse_args()
    data = collect(args.traces_dir, args.runs_dir)
    page = render(data, args.traces_dir, args.runs_dir)
    out = Path(args.out) if args.out else Path(CORPUS_DIR) / f"corpus_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"[Corpus] report saved: {out}")
    print(f"[Corpus] open in browser: {out.resolve().as_uri()}")


if __name__ == "__main__":
    main()