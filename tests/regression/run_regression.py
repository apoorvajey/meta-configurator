"""Regression replays: replay every minimal sequence listed in manifest.json and check how each one ends.

    python -m tests.regression.run_regression                 # all entries, once each
    python -m tests.regression.run_regression --repeat 5      # every entry 5 times: all 5 must agree
    python -m tests.regression.run_regression --only bug03-format-label-desync
    python -m tests.regression.run_regression --app both      # the hosted site and the local build, side by side
    python -m tests.regression.run_regression --no-logs       # do not save the replay logs
Unless --no-logs is given, the first replay of every entry is saved as a self-contained log with its screenshots:
tests/regression/logs/<name>_<app>/ (report.html, screenshots/, console.log) and an overview tests/regression/logs/index.html.
(The local build must be running first: cd meta_configurator, npm run dev.)

Each entry is replayed with the normal replay tool (headless, in its own process) and judged by the status file the
replay writes next to its history (RUN_CLEAN.json, RUN_KNOWN_BUGS.json, ...):
  expect "known-bugs"  the bug is still open: the replay must still show every listed bug;
  expect "clean"       the bug is fixed: none of the listed bugs may show up (with no 'bugs' list: no failure at all).
Other discovered bugs seen in the same replay are only reported. Anything else (a failure that is not a discovered
bug, a crash, a timeout, results that differ between repeats) is a failure and
the exit code is 1, which turns a CI job red. The app under test is LIVE_BASE_URL (tests/shared/python/utils.py);
set "app" in mutation_testing/config/run_config.json (hosted | local | a URL), or the environment variable
METACONFIGURATOR_URL, which wins, to choose the app.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from tests.mutation_testing.common.paths import OUTPUT_DIR
from tests.shared.python.utils import HOSTED_URL, LIVE_BASE_URL, configured_local_url

REGRESSION_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = REGRESSION_DIR / "manifest.json"
SUMMARY_FILE = OUTPUT_DIR / "regression_summary.json"
LOGS_DIR = REGRESSION_DIR / "logs"
REPLAY_TIMEOUT_SECONDS = 15 * 60
EXPECTATIONS = ("clean", "known-bugs")
_HISTORY_LINE = re.compile(r"^Replay history\s+\S+\s+(.+)$", re.MULTILINE)   # "Replay history → <path>"


def replay_once(history: Path, url: str | None = None) -> dict:
    """Replay one history in a fresh process and read its status file. {"error": ...} if that did not work."""
    command = [sys.executable, "-m", "tests.mutation_testing.replay.replay_runner", "--history", str(history),
               "--headless", "--clean-runs", "include", "--known-bug-runs", "include",
               "--no-update-traces", "--no-state-snapshots"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if url:
        env["METACONFIGURATOR_URL"] = url   # wins over run_config.json in tests/shared/python/utils.py
    started = time.time()
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=REPLAY_TIMEOUT_SECONDS, env=env)
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {REPLAY_TIMEOUT_SECONDS // 60} minutes"}
    output = (done.stdout or "") + (done.stderr or "")
    found = _HISTORY_LINE.search(output)
    if done.returncode != 0 or not found:
        return {"error": f"replay did not finish (exit code {done.returncode})", "output_tail": output[-1500:]}
    folder = Path(found.group(1).strip()).parent
    markers = sorted(folder.glob("RUN_*.json"))
    if not markers:
        return {"error": f"no RUN_*.json status file in {folder}", "output_tail": output[-1500:]}
    summary = json.loads(markers[0].read_text(encoding="utf-8"))
    return {"status": summary["status"],
            "bugs": sorted(summary.get("known_bugs", {})),
            "failed_invariants": summary.get("invariants_failed", []),
            "steps_diverged": summary.get("steps_diverged", 0),
            "folder": str(folder),
            "seconds": round(time.time() - started, 1)}


def expected_for(entry: dict, kind: str) -> str:
    """The entry's expectation for an environment ("hosted" or "local"). "expect" is one value for both, or an object
    such as {"hosted": "known-bugs", "local": "clean"} when the bug is already fixed in the version under development."""
    expect = entry["expect"]
    if isinstance(expect, dict):
        return expect.get(kind) or expect.get("default") or "known-bugs"
    return expect


def env_kind(url: str) -> str:
    return "hosted" if url.rstrip("/") == HOSTED_URL.rstrip("/") else "local"


def judge(entry: dict, result: dict, expect: str | None = None) -> str | None:
    """None if the result is what the entry expects, otherwise the reason it is not.
    The entry guards the bugs in its 'bugs' list. Other discovered bugs that show up in the same replay are not its
    business (they are shown in the output), but a failure that is not a discovered bug always fails it."""
    if "error" in result:
        return result["error"]
    wanted, listed, seen = expect or expected_for(entry, "default"), sorted(entry.get("bugs", [])), result["bugs"]
    if result["status"] in ("failures", "diverged-failures", "incomplete") or result["failed_invariants"]:
        detail = f" (failed: {', '.join(result['failed_invariants'])})" if result["failed_invariants"] else ""
        return f"ended as {result['status']!r}{detail}"
    if wanted == "known-bugs":
        missing = [b for b in listed if b not in seen]
        if missing:
            return f"expected {missing} to still occur, replay ended as {result['status']!r} with bugs {seen}"
    elif listed:   # "clean": the listed bugs are fixed, so they must be gone
        back = [b for b in listed if b in seen]
        if back:
            return f"{back} occurred again"
    elif result["status"] != "clean":
        return f"ended as {result['status']!r}, expected 'clean'"
    return None


def _check_manifest(manifest: dict, base: Path) -> list[str]:
    problems = []
    names = set()
    for i, entry in enumerate(manifest.get("entries", [])):
        label = entry.get("name", f"entry {i + 1}")
        if label in names:
            problems.append(f"{label}: name used twice")
        names.add(label)
        expect = entry.get("expect")
        values = list(expect.values()) if isinstance(expect, dict) else [expect]
        if not values or any(v not in EXPECTATIONS for v in values):
            problems.append(f"{label}: 'expect' must be one of {', '.join(EXPECTATIONS)}, or an object such as "
                            f'{{"hosted": "known-bugs", "local": "clean"}}')
        if "known-bugs" in values and not entry.get("bugs"):
            problems.append(f"{label}: expect 'known-bugs' needs a non-empty 'bugs' list")
        if not (base / entry.get("history", "")).is_file():
            problems.append(f"{label}: history file not found: {base / entry.get('history', '')}")
    if not manifest.get("entries"):
        problems.append("the manifest has no entries")
    return problems


def _git_info() -> dict:
    """{"commit": short hash, "branch": name, "dirty": tracked files changed} of the repository, or {} without git.
    Untracked files do not count as dirty (test output and new sequences are untracked most of the time)."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=10,
                              cwd=REGRESSION_DIR).stdout.strip()
    try:
        commit = git("rev-parse", "--short", "HEAD")
        if not commit:
            return {}
        return {"commit": commit, "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}
    except (OSError, subprocess.SubprocessError):
        return {}


def _write_summary(rows: list[dict], repeat: int = 1, only: str | None = None) -> None:
    """One record per invocation: {"time", "git", "repeat", "only", "rows": [...]}. Every row carries the result of each
    repeat ("repeats"), which the regression report uses for flaky, diverged and timing figures."""
    SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "git": _git_info(), "repeat": repeat, "only": only,
              "rows": rows}
    text = json.dumps(record, indent=2)
    SUMMARY_FILE.write_text(text, encoding="utf-8")   # always the latest run
    apps = "_".join(dict.fromkeys(re.sub(r"[^A-Za-z0-9.-]+", "-", str(r["label"])).strip("-") for r in rows)) or "run"
    saved = SUMMARY_FILE.parent / "regression_runs" / f"regression_{apps}_r{repeat}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(text, encoding="utf-8")   # one file per run, never overwritten
    print(f"Summary saved: {saved.as_posix()}")
    target = os.environ.get("GITHUB_STEP_SUMMARY")   # the table on the workflow run's summary page
    if target:
        lines = ["| Replay | App | Expected | Result | Runs |", "|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['name']} | {r['label']} | {r['expect']} | "
                         f"{'✅ ok' if r['ok'] else '❌ ' + r['reason']} | {r['runs']} |")
        with open(target, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def _slug(text: str) -> str:
    """bug-05 -> bug_05; anything that is not a letter, digit or underscore becomes an underscore."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")


def save_log(entry: dict, label: str, expect: str, result: dict, reason: str | None, seen: str,
             logs_dir: Path = LOGS_DIR) -> dict:
    """Save the replay log of one entry with its screenshots: <logs_dir>/<name>[_<app>]/ with report.html,
    screenshots/ and console.log inside. The report refers to its screenshots by relative path, so the folder can be
    opened, moved or zipped on its own. Returns {"report": path relative to logs_dir, "screenshots": n}."""
    stem = _slug(entry["name"]) + ("" if label == "current" else "_" + _slug(label))
    logs_dir.mkdir(parents=True, exist_ok=True)
    files_dir = logs_dir / stem
    if files_dir.exists():
        shutil.rmtree(files_dir)
    verdict = "as expected" if reason is None else "NOT as expected: " + reason
    banner = (f'<div style="font:14px system-ui,sans-serif;padding:10px 14px;margin:0 0 12px;'
              f'background:{"#e8f5e9" if reason is None else "#fdecea"};border:1px solid #ccc">'
              f'<b>Regression replay: {html.escape(entry["name"])}</b> &middot; app: {html.escape(label)} &middot; '
              f'bugs: {html.escape(", ".join(entry.get("bugs", [])) or "-")} &middot; expected: {html.escape(expect)} &middot; '
              f'seen: {html.escape(seen)} &middot; <b>{html.escape(verdict)}</b></div>')
    folder = Path(result["folder"]) if result.get("folder") else None
    log_file = folder / "replay_log.html" if folder else None
    files_dir.mkdir(parents=True, exist_ok=True)
    shots = 0
    if log_file and log_file.is_file():
        text = log_file.read_text(encoding="utf-8", errors="replace")
        shots_dir = folder / "screenshots"
        if shots_dir.is_dir():
            files_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(shots_dir, files_dir / "screenshots")
            shots = sum(1 for _ in (files_dir / "screenshots").glob("*.png"))
        console = folder / "console.log"
        if console.is_file():
            files_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(console, files_dir / "console.log")
        text = text.replace("<body>", "<body>" + banner, 1) if "<body>" in text else banner + text
    else:   # the replay did not finish: keep what we know
        tail = html.escape(result.get("output_tail", "") or "")
        text = ("<!doctype html><meta charset=utf-8><title>" + html.escape(entry["name"]) + "</title><body>" + banner
                + "<p>No replay log was written.</p><pre>" + tail + "</pre></body>")
    (files_dir / "report.html").write_text(text, encoding="utf-8")
    return {"report": f"{stem}/report.html", "screenshots": shots}


def write_log_index(rows: list[dict], logs_dir: Path = LOGS_DIR) -> None:
    """logs/index.html: one line per saved log with its verdict."""
    items = []
    for r in rows:
        if not r.get("log"):
            continue
        mark = "&#9989;" if r["ok"] else "&#10060;"
        why = "" if r["ok"] else " &ndash; " + html.escape(r["reason"])
        items.append(f'<tr><td>{mark}</td><td><a href="{html.escape(r["log"])}">{html.escape(r["name"])}</a></td>'
                     f'<td>{html.escape(str(r["label"]))}</td><td>{html.escape(str(r["expect"]))}</td>'
                     f'<td>{r.get("screenshots", 0)}</td><td>{html.escape(r["reason"]) if not r["ok"] else "as expected"}</td></tr>')
    if not items:
        return
    page = ("<!doctype html><meta charset=utf-8><title>Regression replay logs</title>"
            "<style>body{font:14px system-ui,sans-serif;margin:24px}td,th{padding:4px 10px;border-bottom:1px solid #ddd;text-align:left}</style>"
            "<h1>Regression replay logs</h1><table><tr><th></th><th>Entry</th><th>App</th><th>Expected</th>"
            "<th>Screenshots</th><th>Result</th></tr>" + "".join(items) + "</table>")
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "index.html").write_text(page, encoding="utf-8")


def _targets(app: str | None) -> list[tuple[str, str | None]]:
    """(label, url) to test. None: the app that run_config.json / METACONFIGURATOR_URL selects."""
    if app is None:
        return [("current", None)]
    if app == "hosted":
        return [("hosted", HOSTED_URL)]
    if app == "local":
        return [("local", configured_local_url())]
    if app == "both":
        return [("hosted", HOSTED_URL), ("local", configured_local_url())]
    return [("custom", app)]


def _reachable(url: str) -> bool:
    """True if something answers at url. Any HTTP answer counts, even an error page: only a refused or timed-out
    connection means the app is not there."""
    local = url.startswith(("http://localhost", "http://127."))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if local else urllib.request.build_opener()
    try:   # a local address never goes through a proxy
        opener.open(urllib.request.Request(url, headers={"Accept": "text/html"}), timeout=8).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the regression sequences of manifest.json")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--only", help="run only the entry with this name")
    parser.add_argument("--repeat", type=int, help="replays per entry (default: 'repeat' in the manifest, else 1)")
    parser.add_argument("--app", help="hosted | local | both | a full URL (default: run_config.json / METACONFIGURATOR_URL). "
                                      "both runs every entry on the hosted site and the local build and compares them. "
                                      "local and both need the dev server running: cd meta_configurator, npm run dev")
    parser.add_argument("--no-logs", action="store_true",
                        help="do not save the replay logs with screenshots to tests/regression/logs/")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    problems = _check_manifest(manifest, base)
    if problems:
        print("Manifest problems:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    entries = [e for e in manifest["entries"] if not args.only or e["name"] == args.only]
    if not entries:
        print(f"No entry named {args.only!r}.", file=sys.stderr)
        return 2
    repeat = max(1, args.repeat or int(manifest.get("repeat", 1)))
    targets = _targets(args.app)
    for label, url in targets:   # fail fast with a hint instead of one failed replay per entry
        shown = url or LIVE_BASE_URL
        if not _reachable(shown):
            hint = ("\n  Start the local app first:  cd meta_configurator  then  npm run dev  (leave it running)"
                    if shown.startswith("http://localhost") or shown.startswith("http://127.") else "")
            print(f"Cannot reach {shown}{' (' + label + ')' if label != 'current' else ''}.{hint}", file=sys.stderr)
            return 2

    rows = []
    seen_by_app: dict[str, dict[str, str]] = {}   # entry name -> app label -> what was seen (for the comparison)
    for label, url in targets:
        print(f"App under test: {url or LIVE_BASE_URL}" + (f"  ({label})" if label != "current" else ""))
        for entry in entries:
            outcomes = []
            repeats: list[dict] = []   # one result per replay, saved for the regression report
            log_info: dict = {}
            expect = expected_for(entry, env_kind(url or LIVE_BASE_URL))
            for n in range(1, repeat + 1):
                result = replay_once(base / entry["history"], url)
                reason = judge(entry, result, expect)
                seen = result.get("status", "error")
                if result.get("bugs"):
                    seen += " " + ",".join(result["bugs"])
                    extra = [b for b in result["bugs"] if b not in entry.get("bugs", [])]
                    if extra:
                        seen += f" (other discovered bugs seen: {', '.join(extra)})"
                print(f"  {entry['name']}  run {n}/{repeat}: {seen}"
                      + (f"  [{result.get('seconds')}s]" if "seconds" in result else "")
                      + ("  ok" if reason is None else f"  FAILED - {reason}"))
                if reason and result.get("output_tail"):
                    print("    last output:\n      " + result["output_tail"].replace("\n", "\n      "))
                outcomes.append((reason, seen))
                repeats.append({"status": result.get("status", "error"), "bugs": result.get("bugs", []),
                                "failed_invariants": result.get("failed_invariants", []),
                                "steps_diverged": result.get("steps_diverged", 0),
                                "seconds": result.get("seconds"), "ok": reason is None,
                                **({"reason": reason} if reason else {}),
                                **({"error": result["error"]} if "error" in result else {})})
                if n == 1 and not args.no_logs:   # the first replay of the entry is the one that is saved
                    try:
                        log_info = save_log(entry, label, expect, result, reason, seen)
                        print(f"    log: tests/regression/logs/{log_info['report']}  ({log_info['screenshots']} screenshots)")
                    except Exception as e:   # a problem with the log must never change the regression verdict
                        print(f"    (log not saved: {e})")
            failures = [o for o in outcomes if o[0]]
            different = len({o[1] for o in outcomes}) > 1
            if failures:
                reason = failures[0][0]
            elif different:
                reason = "results differ between runs: " + "; ".join(sorted({o[1] for o in outcomes}))
            else:
                reason = None
            shown_url = url or LIVE_BASE_URL
            rows.append({"name": entry["name"], "label": label if label != "current" else env_kind(shown_url),
                         "app": shown_url, "guards": entry.get("bugs", []),
                         "expect": expect, "ok": reason is None, "reason": reason or "", "runs": repeat,
                         "repeats": repeats,
                         **({"log": log_info["report"], "screenshots": log_info["screenshots"]} if log_info else {})})
            seen_by_app.setdefault(entry["name"], {})[label] = outcomes[0][1] if not reason else f"FAILED: {reason}"

    _write_summary(rows, repeat, args.only)
    if not args.no_logs:
        write_log_index(rows)
    bad = [r for r in rows if not r["ok"]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} regression replay(s) as expected.")
    for r in bad:
        print(f"  FAILED {r['name']} ({r['label']}): {r['reason']}")
    if len(targets) > 1:
        print("\nHosted vs local:")
        for name, by_app in seen_by_app.items():
            same = len(set(by_app.values())) == 1
            print(f"  {name}: {'same on both' if same else 'DIFFERENT'}")
            if not same:
                for label, seen in by_app.items():
                    print(f"      {label}: {seen}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())