"""Known bugs: one page to see every bug and the runs it occurred in, add a new one, edit or remove one.

Optional fields of a bug (set when adding or editing): "name" (a title shown in the list; a new bug can also be given its
own ID instead of the generated one), "group" (bugs with the same group name are one root cause, e.g. the
same defect seen in the Data Editor and in the Schema Editor), "where" (data | schema | both; required for a new bug), "comment" (free text);
"found_by" (how the bug was found: loop = a failure in a mutation-loop run, inferred = learned from an inferred rule),
"git_issue" (link to the Git issue, http:// or https://); "added_at" is set automatically when a bug is added.

    python -m tests.cli bugs                  open the page; click Update there, press Enter here to apply
    python -m tests.cli bugs import           apply the newest new update file by hand: from tests/output/inbox, else Downloads
                                              (--json FILE, --folder DIR); applied files are moved to tests/output/bug_updates
    python -m tests.cli bugs import --all     apply EVERY update file, oldest first: the new ones in tests/output/inbox and Downloads,
                                              and the bugs of the files already in tests/output/bug_updates that are missing from the
                                              list (--folder DIR: only that folder). A bug already in the list (same summary or ID) is
                                              skipped, so running it again changes nothing.

The bugs live in config/triage_config.json:
    known_bugs   active bugs; their failures are labelled and not counted as failures (runs/known_bugs.py).
                 "do_not_repair": true makes the loop skip undo-repair and the stuck-run check for the bug.
    fixed_bugs   removed as fixed (+ fixed_at, fix_note); they are no longer known bugs, so a failure of the same
                 invariants is an ordinary failure again. The page checks the runs after fixed_at and marks the bug
                 RETURNED if one fails the same invariants.
    not_a_bug    removed as "not a bug" (+ closed_at, note); kept for reference only, never checked.

Changes made on the page wait there as "pending" until Update saves known_bugs_update_<time>.json (a normal browser
download: Downloads, or tests/output/inbox if you save it there). The terminal looks in the inbox first, then in Downloads,
and moves the applied file to the update folder ("known_bugs_update_folder" in triage_config.json, default
tests/output/bug_updates). Applying it
(add / edit / remove / repair settings) copies the config to tests/output/config_backups/triage_config.<time>.bak.json first and rewrites only the three
lists above (the rest of the file stays as it is, byte for byte).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.common.paths import BUG_UPDATES_DIR, CONFIG_BACKUPS_DIR, INBOX_DIR, KNOWN_BUGS_DIR
from tests.mutation_testing.common.report_theme import DARK_VARS
from tests.mutation_testing.reports.triage.run_loading import DEFAULT_CONFIG_PATH, find_run_logs, load_config, load_run
from tests.mutation_testing.runs.known_bugs import KnownBugMatcher, observe_history_step
from tests.mutation_testing.runs.run_layout import RUNS_DIR, effective_history, iter_run_dirs

KNOWN, FIXED, NOT_A_BUG = "known_bugs", "fixed_bugs", "not_a_bug"
LISTS = (KNOWN, FIXED, NOT_A_BUG)
TIME_FORMAT = "%Y-%m-%d %H:%M"
REPORT_NAME = "known_bugs.html"
UPDATE_GLOB = "known_bugs_update_*.json"
FOLDER_SETTING = "known_bugs_update_folder"   # top-level key of triage_config.json; default: BUG_UPDATES_DIR
EXIT_OK, EXIT_USAGE = 0, 2
FOUND_BY_VALUES = ("", "loop", "inferred")   # optional "found_by" label of a bug: Mutation loop, Inferred rule
WHERE_VALUES = ("", "data", "schema", "both")   # optional "where" label of a bug: Data Editor, Schema Editor, both/any


# ---------------------------------------------------------------- the config file (three lists rewritten in place)

_SCALAR = r'(?:"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)'
_SCALAR_LIST = re.compile(r"\[\s*(" + _SCALAR + r"(?:\s*,\s*" + _SCALAR + r")*)\s*\]")


def _format_list(items: list[dict]) -> str:
    """The list as the config file writes it: indented by 2, lists of plain values on one line."""
    text = json.dumps(items, indent=2, ensure_ascii=False)
    text = _SCALAR_LIST.sub(lambda m: "[" + re.sub(r"\s*,\s*", ", ", m.group(1)) + "]", text)
    return text.replace("\n", "\n  ")


def _list_span(raw: str, key: str) -> tuple[int, int] | None:
    """(start, end) of the JSON array after the top-level `"key":` in the file text."""
    m = re.search(r'^  "' + re.escape(key) + r'"\s*:\s*', raw, re.M)
    if not m:
        return None
    _, end = json.JSONDecoder().raw_decode(raw, m.end())
    return m.end(), end


def read_lists(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, list[dict]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: list(raw.get(k, [])) for k in LISTS}


def write_lists(lists: dict[str, list[dict]], path: Path = DEFAULT_CONFIG_PATH) -> Path:
    """Back the config up, then replace the three lists in the file text. Returns the backup."""
    CONFIG_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup = CONFIG_BACKUPS_DIR / f"{path.stem}.{datetime.now():%Y%m%d_%H%M%S}.bak{path.suffix}"
    n = 2
    while backup.exists():   # several updates in the same second (bugs import --all) must not overwrite the earlier backup
        backup = CONFIG_BACKUPS_DIR / f"{path.stem}.{datetime.now():%Y%m%d_%H%M%S}-{n}.bak{path.suffix}"
        n += 1
    shutil.copy2(path, backup)
    with open(path, encoding="utf-8", newline="") as f:   # newline="" keeps the file's line endings (Path.read_text has no newline= before Python 3.13)
        raw = f.read()
    crlf = "\r\n" in raw
    raw = raw.replace("\r\n", "\n")
    previous = KNOWN
    for key in LISTS:   # known_bugs exists; the other two are inserted one after the other behind it the first time
        span = _list_span(raw, key)
        text = _format_list(lists[key])
        if span:
            raw = raw[:span[0]] + text + raw[span[1]:]
        else:
            _, end = _list_span(raw, previous)
            raw = raw[:end] + f',\n  "{key}": ' + text + raw[end:]
        previous = key
    json.loads(raw)   # never leave a broken config behind
    path.write_text(raw.replace("\n", "\r\n") if crlf else raw, encoding="utf-8", newline="")
    return backup


def _next_id(lists: dict[str, list[dict]], summary: str) -> str:
    """New bugs get a short slug of their summary (e.g. "undo-leaves-orphaned-row"). Old BUG-NN ids stay as they are
    and are never reused or renumbered, because replays and reports refer to them."""
    words = re.findall(r"[a-z0-9]+", summary.lower())[:4]
    base = "-".join(words) or "bug"
    taken = {str(b.get("id", "")) for items in lists.values() for b in items}
    bug_id, n = base, 2
    while bug_id in taken:
        bug_id, n = f"{base}-{n}", n + 1
    return bug_id


def _check_labels(found_by: str, git_issue: str, what: str = "") -> None:
    if found_by not in FOUND_BY_VALUES:
        raise ValueError(f"{what}unknown 'found_by' value '{found_by}' (use {', '.join(FOUND_BY_VALUES[1:])})")
    if git_issue and not git_issue.startswith(("http://", "https://")):
        raise ValueError(f"{what}the Git issue link must start with http:// or https://")


def _split(text: str | None) -> list[str]:
    return [t.strip() for t in re.split(r"[,\s]+", text or "") if t.strip()]


def action_kinds() -> list[str]:
    from tests.mutation_testing.reports.triage import action_text as a
    return sorted({k for _, k in a.EDITOR_KINDS} | {k for _, k in a.PATH_KINDS} | set(a.STANDALONE_KINDS.values()))


def invariant_choices() -> list[dict]:
    """Every invariant (hand-written and inferred) as {id, type, text}, for the add form."""
    from tests.mutation_testing.reports.invariant_list import collect_invariants
    return [{"id": r["id"], "type": r["type"], "text": r["text"]} for r in collect_invariants()]


# ---------------------------------------------------------------- applying an update

def _remove(lists: dict[str, list[dict]], bug_id: str, how: str, note: str, now: str) -> str:
    bug = next((b for b in lists[KNOWN] if b["id"] == bug_id), None)
    if bug is None:
        raise ValueError(f"'{bug_id}' is not an active known bug")
    if how not in ("fixed", "not-a-bug"):
        raise ValueError(f"{bug_id}: removal reason must be fixed or not-a-bug, not '{how}'")
    lists[KNOWN].remove(bug)
    if how == "fixed":
        lists[FIXED].append({**bug, "fixed_at": now, "fix_note": note})
    else:
        lists[NOT_A_BUG].append({**bug, "closed_at": now, "note": note})
    return f"{bug_id} removed as {'fixed (watched for a return)' if how == 'fixed' else 'not a bug (archived)'}"


def _delete(lists: dict[str, list[dict]], bug_id: str) -> str:
    """Delete a bug of the archive ('not a bug') for good. Active and fixed bugs must be archived first."""
    bug = next((b for b in lists[NOT_A_BUG] if b["id"] == bug_id), None)
    if bug is None:
        raise ValueError(f"cannot delete '{bug_id}': it is not in the archive (archive it first)")
    lists[NOT_A_BUG].remove(bug)
    hint = (" It was a learned bug: change its decision on the review page too, or the next infer --export-only logs it again."
            if bug.get("learned") else "")
    return f"{bug_id} deleted for good.{hint}"


def _add(lists: dict[str, list[dict]], summary: str, invariants: list[str], kinds: list[str],
         known_invariants: set[str], known_kinds: set[str], do_not_repair: bool = False,
         group: str = "", where: str = "", comment: str = "", now: str = "", name: str = "", bug_id: str = "",
         found_by: str = "", git_issue: str = "") -> str:
    unknown = [i for i in invariants if i not in known_invariants]
    if not summary.strip() or not invariants or unknown:
        raise ValueError("a bug needs a summary and at least one invariant" +
                         (f"; unknown invariant(s): {', '.join(unknown)}" if unknown else ""))
    bad = [k for k in kinds if k not in known_kinds]
    if bad:
        raise ValueError(f"unknown action kind(s): {', '.join(bad)}")
    if where not in WHERE_VALUES[1:]:
        raise ValueError(f"a new bug needs 'where' (use {', '.join(WHERE_VALUES[1:])}), not '{where}'")
    found_by, git_issue = found_by.strip(), git_issue.strip()
    _check_labels(found_by, git_issue)
    bug_id = bug_id.strip()
    if bug_id and any(b.get("id") == bug_id for items in lists.values() for b in items):
        raise ValueError(f"the ID '{bug_id}' is already used by another bug")
    bug = {"id": bug_id or _next_id(lists, summary), "summary": summary.strip(), "invariants": invariants}
    if name.strip():
        bug["name"] = name.strip()
    if kinds:
        bug["when_action_kinds"] = kinds
    if group.strip():
        bug["group"] = group.strip()
    if where:
        bug["where"] = where
    if comment.strip():
        bug["comment"] = comment.strip()
    if found_by:
        bug["found_by"] = found_by
    if git_issue:
        bug["git_issue"] = git_issue
    if do_not_repair:
        bug["do_not_repair"] = True
    if now:
        bug["added_at"] = now
    lists[KNOWN].append(bug)
    return f"{bug['id']} logged as a known bug" + (" (do not repair)" if do_not_repair else "")


def _edit(lists: dict[str, list[dict]], rec: dict, known_invariants: set[str], known_kinds: set[str]) -> str:
    """Change the fields named in `rec` of a bug (in any of the three lists). Empty group / where / comment / found_by / git_issue / kinds remove the field."""
    bug_id = str(rec.get("id", ""))
    bug = next((b for items in lists.values() for b in items if b.get("id") == bug_id), None)
    if bug is None:
        raise ValueError(f"cannot edit '{bug_id}': no such bug")
    if "summary" in rec:   # ("new_id", the rename, is applied separately by apply_update)
        if not str(rec["summary"]).strip():
            raise ValueError(f"{bug_id}: the summary cannot be empty")
        bug["summary"] = str(rec["summary"]).strip()
    if "invariants" in rec:
        invs = list(rec["invariants"])
        unknown = [i for i in invs if i not in known_invariants and i not in bug.get("invariants", [])]   # invariants the bug already has stay, even if the rule is no longer exported
        if not invs or unknown:
            raise ValueError(f"{bug_id}: a bug needs at least one invariant" + (f"; unknown invariant(s): {', '.join(unknown)}" if unknown else ""))
        bug["invariants"] = invs
    if "when_action_kinds" in rec:
        kinds = list(rec["when_action_kinds"])
        bad = [k for k in kinds if k not in known_kinds]
        if bad:
            raise ValueError(f"{bug_id}: unknown action kind(s): {', '.join(bad)}")
        bug["when_action_kinds"] = kinds
        if not kinds:
            bug.pop("when_action_kinds")
    if "where" in rec and str(rec["where"]) not in WHERE_VALUES:
        raise ValueError(f"{bug_id}: unknown 'where' value '{rec['where']}'")
    _check_labels(str(rec.get("found_by", "")).strip(), str(rec.get("git_issue", "")).strip(), f"{bug_id}: ")
    for key in ("name", "group", "where", "comment", "found_by", "git_issue"):
        if key in rec:
            value = str(rec[key]).strip()
            if value:
                bug[key] = value
            else:
                bug.pop(key, None)
    if "confirmed" in rec and bug.get("learned"):
        bug["confirmed"] = bool(rec["confirmed"])
    if "do_not_repair" in rec:
        if rec["do_not_repair"]:
            bug["do_not_repair"] = True
        else:
            bug.pop("do_not_repair", None)
    return f"{bug_id} edited"


REGRESSION_MANIFEST = Path(__file__).resolve().parents[2] / "regression" / "manifest.json"   # tests/regression/manifest.json


def _rename(lists: dict[str, list[dict]], renames: dict[str, str]) -> list[str]:
    """Give bugs new ids, all at once (so ids can be swapped or shifted). The final ids must be unique and non-empty."""
    if not renames:
        return []
    all_bugs = [b for items in lists.values() for b in items]
    missing = [old for old in renames if not any(b.get("id") == old for b in all_bugs)]
    if missing:
        raise ValueError(f"cannot rename '{missing[0]}': no such bug")
    if any(not new for new in renames.values()):
        raise ValueError("a bug ID cannot be empty")
    final = [renames.get(b.get("id"), b.get("id")) for b in all_bugs]
    clash = sorted({i for i in final if final.count(i) > 1})
    if clash:
        raise ValueError(f"after renaming, the ID '{clash[0]}' would belong to more than one bug (rename the other one in the same update)")
    for b in all_bugs:
        if b.get("id") in renames:
            b["id"] = renames[b["id"]]
    return [f"{old} renamed to {new}" for old, new in renames.items()]


def _rename_in_manifest(renames: dict[str, str], path: Path = REGRESSION_MANIFEST) -> str:
    """The regression manifest lists bug ids per recorded sequence; keep them in step with a rename. Returns a note ('' if nothing changed)."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            raw = f.read()
        data = json.loads(raw)
    except (OSError, ValueError):
        return ""
    changed = False
    for entry in data.get("entries", []):
        bugs = entry.get("bugs")
        if isinstance(bugs, list):
            new = [renames.get(b, b) for b in bugs]
            if new != bugs:
                entry["bugs"], changed = new, True
    if not changed:
        return ""
    CONFIG_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, CONFIG_BACKUPS_DIR / f"manifest.{datetime.now():%Y%m%d_%H%M%S}.bak.json")
    text = json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if raw.endswith("\n") else "")
    path.write_text(text.replace("\n", "\r\n") if "\r\n" in raw else text, encoding="utf-8", newline="")
    return f"Regression manifest updated: {path}"


def _set_do_not_repair(lists: dict[str, list[dict]], bug_id: str, value: bool) -> str:
    bug = next((b for b in lists[KNOWN] if b["id"] == bug_id), None)
    if bug is None:
        raise ValueError(f"'{bug_id}' is not an active known bug")
    if value:
        bug["do_not_repair"] = True
    else:
        bug.pop("do_not_repair", None)
    return f"{bug_id}: do not repair {'on' if value else 'off'}"


def _already_listed(lists: dict[str, list[dict]], addition: dict) -> bool:
    """True if a bug with the same ID (when the addition names one) or the same summary is in any of the three lists."""
    bug_id, summary = str(addition.get("bug_id", "")).strip(), str(addition.get("summary", "")).strip()
    return any((bug_id and b.get("id") == bug_id) or (summary and str(b.get("summary", "")).strip() == summary)
               for items in lists.values() for b in items)


def apply_update(file: Path, config: Path = DEFAULT_CONFIG_PATH, skip_existing: bool = False, additions_only: bool = False) -> int:
    """Apply a downloaded known_bugs_update_*.json: all removals, then all additions, in one write.
    Either everything is applied or (on any error) nothing.
    skip_existing: an addition whose summary or ID is already in one of the three lists is skipped instead of logged again.
    additions_only: apply only the additions of the file (for files that were applied before; their removals and edits are not repeated)."""
    try:
        update = json.loads(Path(file).read_text(encoding="utf-8"))
        removals, additions = list(update.get("remove", [])), list(update.get("add", []))
        settings = list(update.get("repair", []))
        edits = list(update.get("edit", []))
        deletions = [str(x) for x in update.get("delete", [])]
        labels = list(update.get("label", []))   # problem labels from the issue report (see problem_labels.py)
    except (OSError, ValueError, AttributeError) as exc:
        print(f"Cannot read {file}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if additions_only:
        removals, settings, edits, deletions, labels = [], [], [], [], []
    if not removals and not additions and not settings and not edits and not deletions and not labels:
        print("The update contains no changes.")
        return EXIT_OK
    lists = read_lists(config)
    skipped: list[str] = []
    if skip_existing:
        kept = []
        for a in additions:
            if _already_listed(lists, a):
                skipped.append(f"skipped, already in the list: {a.get('name') or str(a.get('summary', ''))[:70]}")
            else:
                kept.append(a)
        additions = kept
        if skipped and not (additions or removals or settings or edits or deletions or labels):
            print("\n".join(skipped))
            return EXIT_OK
    now = datetime.now().strftime(TIME_FORMAT)
    known_invariants = {c["id"] for c in invariant_choices()} if (additions or edits) else set()
    known_kinds = set(action_kinds())
    done, errors = [], []
    for r in edits:
        try:
            done.append(_edit(lists, r, known_invariants, known_kinds))
        except ValueError as exc:
            errors.append(str(exc))
    for r in settings:
        try:
            done.append(_set_do_not_repair(lists, str(r.get("id", "")), bool(r.get("do_not_repair"))))
        except ValueError as exc:
            errors.append(str(exc))
    for r in removals:
        try:
            done.append(_remove(lists, str(r.get("id", "")), str(r.get("as", "")), str(r.get("note", "")), now))
        except ValueError as exc:
            errors.append(str(exc))
    for bug_id in deletions:
        try:
            done.append(_delete(lists, bug_id))
        except ValueError as exc:
            errors.append(str(exc))
    renames = {str(r["id"]): str(r["new_id"]).strip() for r in edits if r.get("new_id") is not None and str(r["new_id"]).strip() != str(r.get("id", ""))}
    for a in additions:
        try:
            done.append(_add(lists, str(a.get("summary", "")), list(a.get("invariants", [])),
                             list(a.get("when_action_kinds", [])), known_invariants, known_kinds,
                             bool(a.get("do_not_repair")), str(a.get("group", "")), str(a.get("where", "")),
                             str(a.get("comment", "")), now, str(a.get("name", "")), str(a.get("bug_id", "")),
                             str(a.get("found_by", "")), str(a.get("git_issue", ""))))
        except ValueError as exc:
            errors.append(str(exc))
    try:
        done.extend(_rename(lists, renames))   # last, so everything else in this update refers to the old ids
    except ValueError as exc:
        errors.append(str(exc))
    from tests.mutation_testing.reports import problem_labels
    errors.extend(problem_labels.check(labels))
    if errors:
        print("Not applied:\n  " + "\n  ".join(errors), file=sys.stderr)
        return EXIT_USAGE
    backup_line = ""
    if removals or additions or settings or edits or deletions:   # a labels-only update leaves the config alone
        backup_line = f"\nBackup of the config: {write_lists(lists, config)}"
    note = _rename_in_manifest(renames) if renames else ""
    if labels:
        done.extend(problem_labels.apply_labels(labels))
    print("\n".join(skipped + done) + backup_line + (f"\n{note}" if note else ""))
    return EXIT_OK


# ---------------------------------------------------------------- where the bugs occurred

def _log_uri(run, cfg: dict, step: int | None = None) -> str:
    """file:// link to the run's log (the replay log for a replay), at '#step-N' when a step is given; '' if there is none."""
    suffix = f"#step-{step}" if step else ""
    try:
        replay_log = run.path.parent / "replay_log.html"
        if replay_log.is_file():
            return replay_log.resolve().as_uri() + suffix
        for f in find_run_logs(run, cfg):
            if f.suffix.lower() == ".html":
                return f.resolve().as_uri() + suffix
    except Exception:
        pass
    return ""


def _occurrences(bug: dict, cfg: dict, runs_dir: Path) -> list[dict]:
    """One record per run in which a step fails the bug's invariants (under its action kinds, if it names them)."""
    found = []
    for run_dir in iter_run_dirs(runs_dir):
        history = effective_history(run_dir)
        if history is None:
            continue
        try:
            run = load_run(history, cfg)
        except (OSError, ValueError):
            continue
        matcher = KnownBugMatcher([bug])
        hits = [i + 1 for i, step in enumerate(run.steps) if observe_history_step(matcher, step)[0]]
        if hits:
            found.append({"run_id": run.run_id, "started": run.started, "schema": run.schema, "steps": len(hits),
                          "first_step": hits[0], "total": len(run.steps), "replay": bool(run.replay_id),
                          "log": _log_uri(run, cfg), "log_step": _log_uri(run, cfg, hits[0])})
    return found


def _describe(inv: str, cfg: dict, inferred: dict[str, str]) -> str:
    return (cfg.get("invariants", {}) or {}).get(inv) or inferred.get(inv, "")


def build_rows(runs_dir: Path = RUNS_DIR) -> dict:
    """{'attention': [...], 'fixed': [...], 'known': [...], 'archive': [...]} with the occurrences of each bug."""
    cfg = load_config(DEFAULT_CONFIG_PATH)
    lists = read_lists()
    try:
        from tests.mutation_testing.common.paths import INFERRED_RULES_PATH
        inferred = {r["id"]: r["statement"] for r in json.loads(Path(INFERRED_RULES_PATH).read_text(encoding="utf-8"))["rules"]}
    except (OSError, ValueError, KeyError):
        inferred = {}
    out = {"attention": [], "fixed": [], "known": [], "archive": []}
    for key, section in ((KNOWN, "known"), (FIXED, "fixed"), (NOT_A_BUG, "archive")):
        for bug in lists[key]:
            row = {**bug, "inv_text": {i: _describe(i, cfg, inferred) for i in bug.get("invariants", [])}, "runs": []}
            if section != "archive":
                row["runs"] = _occurrences(bug, cfg, runs_dir)
            if section == "fixed":
                try:
                    fixed_at = datetime.strptime(bug["fixed_at"], TIME_FORMAT)
                except (KeyError, ValueError):
                    fixed_at = None
                for r in row["runs"]:
                    r["after_fix"] = bool(fixed_at and r["started"] > fixed_at)
                if any(r["after_fix"] for r in row["runs"]):
                    section = "attention"
            out[section].append(row)
    return out


# ---------------------------------------------------------------- the page

CSS = ":root{" + DARK_VARS + "}" + """
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}h1{font-size:26px;margin:0}h2{font-size:18px;margin:28px 0 8px}
header{position:sticky;top:0;z-index:3;background:var(--bg);border-bottom:1px solid var(--line)}
header .in{max-width:1180px;margin:0 auto;padding:12px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header .sp{flex:1}.sub{color:var(--mute);font-size:13px}code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:13px}
button{font:inherit;font-size:13.5px;padding:4px 12px;border:1px solid var(--line);border-radius:8px;background:var(--chip);color:var(--ink);cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent)}button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);border-color:var(--accent);color:#14161b;font-weight:600}
input[type=text],textarea{font:inherit;font-size:14px;padding:5px 9px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);width:100%}
input[type=checkbox],input[type=radio]{accent-color:var(--accent)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.card b{display:block;font-size:24px}.card span{color:var(--mute);font-size:13px}
.card.alert{border-color:#5a2a30;background:#2a1a1d}.card.alert b{color:var(--bad)}
details.panel,details.bug{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:6px 0}
details.panel>summary{cursor:pointer;padding:9px 14px;font-weight:600}details.panel .in{padding:4px 16px 14px}
details.bug.alert{border-color:#5a2a30;border-left:4px solid var(--bad)}details.bug.gone{opacity:.6}details.bug.picked{border-color:#6ea8fe;box-shadow:0 0 0 2px #6ea8fe55}
details.bug>summary{cursor:pointer;list-style:none;padding:9px 14px;display:grid;grid-template-columns:110px 210px 1fr 110px;gap:12px;align-items:baseline}
details.bug>summary::-webkit-details-marker{display:none}details.bug>summary:hover{background:#222733}
.id{font-weight:600}.tag{display:inline-block;background:var(--chip);border-radius:20px;padding:1px 9px;font-size:12px;white-space:nowrap}
.tag.returned{background:#3a1c1c;color:var(--bad)}.tag.fixed{color:var(--good)}.tag.known{color:var(--warn)}.tag.archive{color:var(--mute)}.tag.pend{background:#2d2711;color:#f0c76a}
.runs{color:var(--mute);font-size:13px;text-align:right}.body{border-top:1px solid var(--line);padding:10px 16px 14px}
.body dl{margin:0 0 8px;display:grid;grid-template-columns:140px 1fr;gap:5px 14px}.body dt{color:var(--mute);font-size:13px}.body dd{margin:0}
table{border-collapse:collapse;font-size:13.5px;margin-top:6px}th,td{padding:3px 16px 3px 0;text-align:left;border-bottom:1px solid var(--line)}th{color:var(--mute);font-weight:600}
tr.after td{color:var(--bad)}.mute{color:var(--mute)}.none{color:var(--mute);margin:4px 0 0}
.invlist{max-height:220px;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:6px;padding:4px 8px;background:var(--bg)}
.invlist label,.kinds label{display:flex;gap:8px;align-items:baseline;padding:1px 0;font-size:13.5px;cursor:pointer}
.kinds{display:flex;flex-wrap:wrap;gap:4px 16px;margin:4px 0 10px}.lbl{display:block;color:var(--mute);font-size:13px;margin:10px 0 2px}
.pend-row{display:flex;gap:10px;align-items:baseline;padding:5px 0;border-bottom:1px solid var(--line)}.pend-row span:nth-child(2){flex:1}
.rm{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:10px}.rm input[type=text]{width:260px}
#msg{color:var(--good);font-size:13.5px}.bad{color:var(--bad);font-size:14.5px;font-weight:600}
.tg{display:flex;gap:4px;flex-wrap:wrap}.tag.grp{background:#1d2a44;color:var(--accent)}
.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:18px 0 0}.tools select{font:inherit;font-size:13.5px;padding:4px 8px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink)}
.tools input[type=text]{width:260px}.tools label{color:var(--mute);font-size:13px;display:flex;gap:6px;align-items:center}
.grpbox{border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:10px;padding:4px 10px 8px;margin:14px 0;background:rgba(76,141,255,.06)}
.grpbox.plain{border-left-color:var(--line);background:none}.grpbox>p.grp{margin:6px 0 4px;color:var(--accent);font-size:14px;font-weight:600}.grpbox.plain>p.grp{color:var(--mute)}
@media(max-width:760px){details.bug>summary{grid-template-columns:1fr}.body dl{grid-template-columns:1fr}}
"""

JS = r"""
var D=JSON.parse(document.getElementById('data').textContent),adds=[],rems={},dels={},reps={},edits={},editing=null,view={order:'group',where:'',q:''},$=function(i){return document.getElementById(i)};
var WHERE={data:'Data Editor',schema:'Schema Editor',both:'Both / any'},FOUND={loop:'Mutation loop',inferred:'Inferred rule'};
function foundBy(b){return b.found_by||(b.learned?'inferred':'')}
function allBugs(){return [].concat(D.bugs.attention,D.bugs.known,D.bugs.fixed,D.bugs.archive)}
function lastSeen(b){return b.runs.reduce(function(m,r){return r.started>m?r.started:m},'')}
function cmp(x,y){return x<y?-1:x>y?1:0}
var ORD={group:function(a,b){if(!a.group!==!b.group)return a.group?-1:1;return cmp(a.group||'',b.group||'')},runs:function(a,b){return b.runs.length-a.runs.length},
seen:function(a,b){return cmp(lastSeen(b),lastSeen(a))},added:function(a,b){return cmp(b.added_at||'',a.added_at||'')},editor:function(a,b){var r={data:0,schema:1,both:2};var x=a.where in r?r[a.where]:3,y=b.where in r?r[b.where]:3;return x-y||ORD.group(a,b)},name:function(a,b){return 0}};
function ord(a,b){return ORD[view.order](a,b)||cmp(a.id,b.id)}
function shown(b){var w=view.where;if(w==='none'){if(b.where)return false}else if(w==='both'){if(b.where!=='both')return false}else if(w&&b.where!==w&&b.where!=='both')return false;
if(view.q&&((b.id+' '+(b.name||'')+' '+(b.summary||'')+' '+(b.group||'')+' '+(b.comment||'')).toLowerCase().indexOf(view.q)<0))return false;return true}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function tag(c,x){return el('span','tag '+c,x)}
function dl(parts){var d=el('dl');parts.forEach(function(p){d.appendChild(el('dt',null,p[0]));var dd=el('dd');if(typeof p[1]==='string')dd.textContent=p[1];else dd.appendChild(p[1]);d.appendChild(dd)});return d}
function runsTable(runs,fixed){if(!runs.length)return el('p','none','Not seen in any recorded run.');
var t=el('table'),h=el('tr');['Run','Started','Schema','Failing steps','First at step'].concat(fixed?['When']:[]).forEach(function(x){h.appendChild(el('th',null,x))});t.appendChild(h);
runs.slice().sort(function(a,b){return a.started<b.started?1:-1}).forEach(function(r){var tr=el('tr',r.after_fix?'after':null);
[r.run_id+(r.replay?' (replay)':''),r.started,r.schema,r.steps+' of '+r.total,r.first_step].concat(fixed?[r.after_fix?'after the fix':'before the fix']:[]).forEach(function(x,i){var td=el('td'),u=i==0?r.log:(i==4?r.log_step:'');
if(u){var a=el('a',null,String(x));a.href=u;a.target='_blank';a.title=i==0?'Open the log of this run':'Open the log at the first failing step';td.appendChild(a)}else td.textContent=String(x);tr.appendChild(td)});t.appendChild(tr)});return t}
function invs(b){var s=el('span');Object.keys(b.inv_text).forEach(function(i,n){if(n)s.appendChild(el('br'));s.appendChild(el('code',null,i));s.appendChild(document.createTextNode(' '+b.inv_text[i]))});return s}
function repairBox(b){var w=el('div','rm'),lb=el('label'),cb=el('input');cb.type='checkbox';cb.checked=b.id in reps?reps[b.id]:!!b.do_not_repair;
cb.onchange=function(){if(cb.checked===!!b.do_not_repair)delete reps[b.id];else reps[b.id]=cb.checked;draw()};
lb.appendChild(cb);lb.appendChild(document.createTextNode(' Do not repair this bug (the loop skips undo-repair and the stuck-run check for it)'));w.appendChild(lb);return w}
function bugCard(b,sec){var gone=rems[b.id],d=el('details','bug'+(sec==='attention'?' alert':'')+(gone||dels[b.id]?' gone':''));d.dataset.id=sec+':'+b.id;if(SEEN?OPEN[d.dataset.id]:sec==='attention')d.open=true;
var lab={attention:['returned','returned'],fixed:['fixed','fixed, not seen again'],known:['known','open'],archive:['archive','not a bug']}[sec],s=el('summary'),fx=sec==='attention'||sec==='fixed';
s.appendChild(el('span','id',b.id));var t=el('span','tg');t.appendChild(tag(lab[0],lab[1]));if(b.learned)t.appendChild(tag(b.confirmed||(edits[b.id]&&edits[b.id].confirmed)?'':'pend',b.confirmed||(edits[b.id]&&edits[b.id].confirmed)?'learned':'learned - confirm'));if(b.group)t.appendChild(tag('grp',b.group));if(foundBy(b))t.appendChild(tag('',FOUND[foundBy(b)]));t.appendChild(b.where?tag('',WHERE[b.where]||b.where):tag('pend','editor not set'));if(edits[b.id])t.appendChild(tag('pend','edit pending'));if(gone)t.appendChild(tag('pend','will be removed: '+gone.as));s.appendChild(t);
var ttl=el('span');if(b.name){ttl.appendChild(el('b',null,b.name));ttl.appendChild(document.createTextNode(' - '))}ttl.appendChild(document.createTextNode((b.summary||'').slice(0,150)));s.appendChild(ttl);
var n=b.runs.filter(function(r){return r.after_fix}).length;s.appendChild(el('span','runs',sec==='archive'?'':fx?n+' run(s) since the fix':b.runs.length+' run(s)'));d.appendChild(s);
var body=el('div','body'),p=[].concat(b.name?[['Name',b.name]]:[],[['Summary',b.summary||''],['Invariants',invs(b)],['Only under actions',b.when_action_kinds?b.when_action_kinds.join(', '):'any action']]);
if(b.group)p.push(['Group (same root cause)',b.group]);
var sibs=allBugs().filter(function(y){return b.group&&y.group===b.group&&y.id!==b.id});
if(sibs.length){var sp=el('span');sibs.forEach(function(y,n){if(n)sp.appendChild(el('br'));sp.appendChild(el('code',null,y.id));sp.appendChild(document.createTextNode(' '+(y.where?'('+(WHERE[y.where]||y.where)+') ':'')+(y.summary||'').slice(0,90)))});p.push(['Same bug as',sp])}
if(b.where)p.push(['Where',WHERE[b.where]||b.where]);if(foundBy(b))p.push(['Found by',FOUND[foundBy(b)]]);
if(b.git_issue){var ga=el('a',null,b.git_issue);ga.href=b.git_issue;ga.target='_blank';p.push(['Git issue',ga])}if(b.comment)p.push(['Notes',b.comment]);if(b.added_at)p.push(['Added',b.added_at]);
if(sec==='known')p.push(['Repair',b.do_not_repair?'not repaired: the loop skips repair for this bug':'repaired as usual']);if(b.fixed_at)p.push(['Fixed',b.fixed_at+(b.fix_note?' - '+b.fix_note:'')]);if(b.closed_at)p.push(['Closed as not a bug',b.closed_at+(b.note?' - '+b.note:'')]);
body.appendChild(dl(p));if(sec!=='archive')body.appendChild(runsTable(b.runs,fx));
var er=el('div','rm'),eb=el('button',null,edits[b.id]?'Edit again':'Edit');eb.onclick=function(){startEdit(b)};er.appendChild(eb);
if(b.learned&&!b.confirmed&&!(edits[b.id]&&edits[b.id].confirmed)){var cb=el('button',null,'Confirm learned bug');cb.onclick=function(){var e=edits[b.id]||{id:b.id,name:b.name||'',summary:b.summary||'',invariants:b.invariants||[],group:b.group||'',where:b.where||'',comment:b.comment||'',found_by:b.found_by||'',git_issue:b.git_issue||'',when_action_kinds:b.when_action_kinds||[]};e.confirmed=true;edits[b.id]=e;draw()};er.appendChild(cb)}
body.appendChild(er)
if(sec==='archive'){var dr=el('div','rm');if(dels[b.id]){dr.appendChild(el('span','mute','Pending: will be deleted for good'));var ud=el('button',null,'Undo');ud.onclick=function(){delete dels[b.id];draw()};dr.appendChild(ud)}
else{var db=el('button',null,'Delete permanently');db.onclick=function(){if(db.dataset.sure){dels[b.id]=1;draw()}else{db.dataset.sure=1;db.textContent='Really delete? Click again'}};dr.appendChild(db)}body.appendChild(dr)}
if(sec==='known'){var rm=el('div','rm');if(gone){rm.appendChild(el('span','mute','Pending removal as '+gone.as+(gone.note?' - '+gone.note:'')));var u=el('button',null,'Undo');u.onclick=function(){delete rems[b.id];draw()};rm.appendChild(u)}
else{var f=el('label'),r1=el('input');r1.type='radio';r1.name='r'+b.id;r1.value='fixed';r1.checked=true;f.appendChild(r1);f.appendChild(document.createTextNode(' fixed'));
var g=el('label'),r2=el('input');r2.type='radio';r2.name='r'+b.id;r2.value='not-a-bug';g.appendChild(r2);g.appendChild(document.createTextNode(' not a bug'));
var note=el('input');note.type='text';note.placeholder='note (optional)';var go=el('button',null,'Remove bug');
go.onclick=function(){rems[b.id]={as:r2.checked?'not-a-bug':'fixed',note:note.value.trim()};draw()};[f,g,note,go].forEach(function(x){rm.appendChild(x)})}body.appendChild(rm);body.appendChild(repairBox(b))}
d.appendChild(body);return d}
var OPEN={},SEEN=false;
function draw(){if(SEEN){OPEN={};[].slice.call(document.querySelectorAll('details.bug')).forEach(function(x){if(x.open)OPEN[x.dataset.id]=1})}var c=$('cards');c.textContent='';
var K=D.bugs.known,cnt=function(w){return K.filter(function(b){return (b.where||'')===w}).length};
[['known','Bugs discovered',K.length],['data','Data Editor',cnt('data')],['schema','Schema Editor',cnt('schema')],['both','Both editors',cnt('both')],
['unset','Editor not set',K.length-cnt('data')-cnt('schema')-cnt('both')],
['attention','Fixed bugs that returned',D.bugs.attention.length,1],['fixed','Fixed, not seen again',D.bugs.fixed.length]].forEach(function(x){
if(x[0]==='unset'?!x[2]:(x[0]==='attention'||x[0]==='fixed')&&!x[2])return;
var k=el('div','card'+(x[3]?' alert':''));k.appendChild(el('b',null,x[2]));k.appendChild(el('span',null,x[1]));c.appendChild(k)});
fillGroups();
var m=$('sections');m.textContent='';
[['attention','Needs attention: a fixed bug is back'],['known','Bugs discovered'],['fixed','Fixed bugs, not seen again since the fix'],['archive','Archive: not a bug']].forEach(function(x){
var all=D.bugs[x[0]],l=all.filter(shown).sort(ord);if(!all.length&&x[0]!=='known')return;if(!l.length&&x[0]!=='known')return;
m.appendChild(el('h2',null,x[1]+' ('+l.length+(l.length!==all.length?' of '+all.length:'')+')'));
var keyOf=view.order==='group'?function(b){return b.group||''}:view.order==='editor'?function(b){return b.where||''}:null,curKey=null,box=null;
l.forEach(function(b){if(keyOf){var k=keyOf(b);if(box===null||k!==curKey){curKey=k;var n=l.filter(function(y){return keyOf(y)===k}).length;
 box=el('div','grpbox'+(k?'':' plain'));box.appendChild(el('p','grp',(view.order==='group'?(k?'Same root cause: '+k:'Not grouped'):(k?'Editor: '+(WHERE[k]||k):'Editor not set'))+' ('+n+' bug'+(n>1?'s':'')+')'));m.appendChild(box)}box.appendChild(bugCard(b,x[0]))}else m.appendChild(bugCard(b,x[0]))});
if(!l.length)m.appendChild(el('p','none',all.length?'No bug matches the filters.':'None.'))});
var p=$('pending');p.textContent='';var n=0;
Object.keys(edits).forEach(function(id){n++;var r=el('div','pend-row'),e=edits[id];r.appendChild(tag('pend','edit'));r.appendChild(el('span',null,id+(e.new_id?' -> '+e.new_id:'')+(e.name?' "'+e.name+'"':'')+': '+e.summary+'  ['+e.invariants.join(', ')+']'+(e.group?' group: '+e.group:'')+(e.where?' where: '+(WHERE[e.where]||e.where):'')));
var u=el('button',null,'Undo');u.onclick=function(){delete edits[id];draw()};r.appendChild(u);p.appendChild(r)});
adds.forEach(function(a,i){n++;var r=el('div','pend-row');r.appendChild(tag('pend','add'));r.appendChild(el('span',null,(a.bug_id?a.bug_id+': ':'')+(a.name?'"'+a.name+'" ':'')+a.summary+'  ['+a.invariants.join(', ')+(a.when_action_kinds.length?'; '+a.when_action_kinds.join(', '):'')+']'+(a.group?' group: '+a.group:'')+(a.where?' where: '+(WHERE[a.where]||a.where):'')+(a.do_not_repair?' (do not repair)':'')));
var u=el('button',null,'Undo');u.onclick=function(){adds.splice(i,1);draw()};r.appendChild(u);p.appendChild(r)});
Object.keys(dels).forEach(function(id){n++;var r=el('div','pend-row');r.appendChild(tag('pend','delete'));r.appendChild(el('span',null,id+' deleted for good'));var u=el('button',null,'Undo');u.onclick=function(){delete dels[id];draw()};r.appendChild(u);p.appendChild(r)});
Object.keys(rems).forEach(function(id){n++;var r=el('div','pend-row');r.appendChild(tag('pend','remove'));r.appendChild(el('span',null,id+' as '+rems[id].as+(rems[id].note?' - '+rems[id].note:'')));
var u=el('button',null,'Undo');u.onclick=function(){delete rems[id];draw()};r.appendChild(u);p.appendChild(r)});
Object.keys(reps).forEach(function(id){n++;var r=el('div','pend-row');r.appendChild(tag('pend','setting'));r.appendChild(el('span',null,id+': do not repair '+(reps[id]?'on':'off')));
var u=el('button',null,'Undo');u.onclick=function(){delete reps[id];draw()};r.appendChild(u);p.appendChild(r)});
$('psum').textContent='Pending changes ('+n+')';$('upd').textContent='Update'+(n?' ('+n+')':'');$('upd').disabled=!n;$('pend').hidden=!n;SEEN=true}
function drawInv(){var q=$('iq').value.toLowerCase(),l=$('invs');l.textContent='';
D.invariants.filter(function(i){return !q||(i.id+' '+i.type+' '+i.text).toLowerCase().indexOf(q)>=0}).forEach(function(i){var lb=el('label'),cb=el('input');cb.type='checkbox';cb.value=i.id;cb.checked=!!sel[i.id];
cb.onchange=function(){if(cb.checked)sel[i.id]=1;else delete sel[i.id]};lb.appendChild(cb);lb.appendChild(el('code',null,i.id));lb.appendChild(el('span','mute',i.type));lb.appendChild(el('span',null,i.text));l.appendChild(lb)})}
var sel={};
D.kinds.forEach(function(k){var lb=el('label'),cb=el('input');cb.type='checkbox';cb.value=k;lb.appendChild(cb);lb.appendChild(document.createTextNode(k));$('kinds').appendChild(lb)});
$('iq').oninput=drawInv;
function groupNames(){var gs={};allBugs().forEach(function(b){if(b.group)gs[b.group]=1});adds.forEach(function(a){if(a.group)gs[a.group]=1});Object.keys(edits).forEach(function(i){if(edits[i].group)gs[edits[i].group]=1});return Object.keys(gs).sort()}
function fillGroups(){var sl=$('grpsel'),cur=sl.value;sl.textContent='';var o0=el('option',null,'No group');o0.value='';sl.appendChild(o0);
groupNames().forEach(function(g){var o=el('option',null,g);o.value=g;sl.appendChild(o)});var on=el('option',null,'+ New group ...');on.value='__new';sl.appendChild(on);
sl.value=[].slice.call(sl.options).some(function(o){return o.value===cur})?cur:''}
function setGroup(g){fillGroups();var sl=$('grpsel');sl.value=g||'';if(g&&sl.value!==g){var o=el('option',null,g);o.value=g;sl.insertBefore(o,sl.lastChild);sl.value=g}$('grp').hidden=true;$('grp').value=''}
function getGroup(){return $('grpsel').value==='__new'?$('grp').value.trim():$('grpsel').value}
$('grpsel').onchange=function(){var nw=$('grpsel').value==='__new';$('grp').hidden=!nw;if(nw)$('grp').focus()};
function resetForm(){editing=null;$('bid').value='';$('bid').disabled=false;$('nm').value='';$('norep').checked=false;$('sum').value='';setGroup('');$('where').value='';$('cmt').value='';$('foundby').value='';$('gitlink').value='';sel={};document.querySelectorAll('#kinds input').forEach(function(x){x.checked=false});
$('addbtn').textContent='Add to pending changes';$('canc').hidden=true;$('ftitle').textContent='Log a newly discovered bug';drawInv()}
function startEdit(b){resetForm();editing=b.id;$('bid').value=b.id;$('bid').disabled=false;$('nm').value=b.name||'';$('sum').value=b.summary||'';(b.invariants||[]).forEach(function(i){sel[i]=1});
document.querySelectorAll('#kinds input').forEach(function(x){x.checked=(b.when_action_kinds||[]).indexOf(x.value)>=0});
$('norep').checked=!!b.do_not_repair;setGroup(b.group||'');$('where').value=b.where||'';$('cmt').value=b.comment||'';$('foundby').value=foundBy(b);$('gitlink').value=b.git_issue||'';
$('addbtn').textContent='Save edit to pending changes';$('canc').hidden=false;$('ftitle').textContent='Edit '+b.id;$('formpanel').open=true;drawInv();$('formpanel').scrollIntoView({behavior:'smooth'})}
$('canc').onclick=resetForm;
$('addbtn').onclick=function(){var s=$('sum').value.trim(),iv=Object.keys(sel),ks=[].slice.call(document.querySelectorAll('#kinds input:checked')).map(function(x){return x.value});
function fail(msg,id){$('adderr').textContent=msg;var f=$(id);f.style.borderColor='var(--bad)';f.focus();f.scrollIntoView({block:'center',behavior:'smooth'})}
['sum','where','invs','bid'].forEach(function(i){$(i).style.borderColor=''});
if(!s){fail('Not saved: the summary is empty.','sum');return}
if(!iv.length){fail('Not saved: tick at least one invariant.','iq');return}
if(!$('where').value){fail('Not saved: choose "Where the bug happens" (Data Editor, Schema Editor or Both). It is required for every bug.','where');return}var gl=$('gitlink').value.trim();$('gitlink').style.borderColor='';
if(gl&&gl.indexOf('http')!==0){fail('Not saved: the Git issue link must start with http:// or https://.','gitlink');return}$('adderr').textContent='';
var rec={name:$('nm').value.trim(),summary:s,invariants:iv,when_action_kinds:ks,group:getGroup(),where:$('where').value,comment:$('cmt').value.trim(),found_by:$('foundby').value,git_issue:gl,do_not_repair:$('norep').checked};
var nid=$('bid').value.trim();
var cnt={};allBugs().forEach(function(x){var f=x.id===editing?nid:(edits[x.id]&&edits[x.id].new_id)||x.id;cnt[f]=(cnt[f]||0)+1});adds.forEach(function(a){if(a.bug_id)cnt[a.bug_id]=(cnt[a.bug_id]||0)+1});if(!editing&&nid)cnt[nid]=(cnt[nid]||0)+1;
if(editing&&!nid){fail('Not saved: the bug ID cannot be empty.','bid');return}
if(nid&&cnt[nid]>1){fail('Not saved: the ID "'+nid+'" would belong to two bugs. Rename the other bug in the same update (its pending rename counts), or choose another ID.','bid');return}
if(editing){rec.id=editing;if(nid!==editing)rec.new_id=nid;edits[editing]=rec}else{rec.bug_id=nid;adds.push(rec)}resetForm();draw()};
$('vord').onchange=function(){view.order=$('vord').value;draw()};$('vwhere').onchange=function(){view.where=$('vwhere').value;draw()};$('vq').oninput=function(){view.q=$('vq').value.toLowerCase();draw()};
$('upd').onclick=function(){var out={add:adds,edit:Object.keys(edits).map(function(id){return edits[id]}),remove:Object.keys(rems).map(function(id){return {id:id,as:rems[id].as,note:rems[id].note}}),delete:Object.keys(dels),repair:Object.keys(reps).map(function(id){return {id:id,do_not_repair:reps[id]}})};
var d=new Date(),z=function(n){return ('0'+n).slice(-2)},st=d.getFullYear()+z(d.getMonth()+1)+z(d.getDate())+'_'+z(d.getHours())+z(d.getMinutes())+z(d.getSeconds()),name='known_bugs_update_'+st+'.json',text=JSON.stringify(out,null,2);
function done(){adds=[];edits={};rems={};dels={};reps={};resetForm();draw();$('msg').textContent='Saved '+name+'. Now press Enter in the terminal to apply it; then press F5 here.'}
function download(){var a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type:'application/json'}));a.download=name;a.click();done()}
download()};
$('folder').textContent=D.folder;draw();drawInv();
(function(){var id=decodeURIComponent((location.hash||'').slice(1));if(!id)return;
var d=[].slice.call(document.querySelectorAll('details.bug')).filter(function(x){return x.dataset.id.split(':').slice(1).join(':')===id})[0];
if(d){d.open=true;d.scrollIntoView({block:'center'});d.classList.add('picked')}})();
"""

BODY = """<header><div class=in><h1>Bugs discovered</h1><span class=sub>__SUB__</span><span class=sp></span><span id=msg></span>
<button class=primary id=upd disabled>Update</button></div></header>
<main><p class=sub>Update downloads <code>known_bugs_update_&lt;time&gt;.json</code> (your Downloads folder). Then press Enter in the terminal: it is applied and moved to <code id=folder></code>.</p>
<div class=cards id=cards></div>
<div class=tools><label>Order <select id=vord><option value=group>Same root cause together</option><option value=runs>Most runs affected</option><option value=seen>Most recently seen</option><option value=editor>Editor (Data / Schema / Both)</option><option value=added>Newest added</option><option value=name>ID (A-Z)</option></select></label>
<label>Where <select id=vwhere><option value="">Any</option><option value=data>Data Editor</option><option value=schema>Schema Editor</option><option value=both>Both / any only</option><option value=none>Not set</option></select></label>
<input type=text id=vq placeholder="search id, summary, group, notes"></div>
<details class=panel id=pend hidden open><summary id=psum>Pending changes</summary><div class=in id=pending></div></details>
<details class=panel id=formpanel><summary id=ftitle>Log a newly discovered bug</summary><div class=in>
<span class=lbl>Bug ID (new bug: optional, empty = generated from the summary. Existing bug: change it to rename the bug; the config and the regression manifest are updated, old run logs show the new ID)</span><input type=text id=bid placeholder="e.g. BUG-08">
<span class=lbl>Name (optional title shown in the list)</span><input type=text id=nm placeholder="e.g. Blank key added on first required key">
<span class=lbl>Summary</span><textarea id=sum rows=3></textarea>
<span class=lbl>Group: bugs with the same name are one root cause (optional, e.g. the same defect in two editors)</span><select id=grpsel style="font:inherit;font-size:14px;padding:5px 9px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink)"></select> <input type=text id=grp hidden placeholder="name of the new group, e.g. format selector out of sync after undo">
<span class=lbl>Where the bug happens (required)</span><select id=where style="font:inherit;font-size:14px;padding:5px 9px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink)"><option value="">choose ...</option><option value=data>Data Editor</option><option value=schema>Schema Editor</option><option value=both>Both / any</option></select>
<span class=lbl>Found by (optional): the mutation loop, or learned from an inferred rule</span><select id=foundby style="font:inherit;font-size:14px;padding:5px 9px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink)"><option value="">not set</option><option value=loop>Mutation loop</option><option value=inferred>Inferred rule</option></select>
<span class=lbl>Git issue link (optional)</span><input type=text id=gitlink placeholder="https://github.com/.../issues/123">
<span class=lbl>Notes (optional)</span><textarea id=cmt rows=2></textarea>
<span class=lbl>Invariants it makes fail (tick one or more)</span><input type=text id=iq placeholder="search id, type, text"><div class=invlist id=invs></div>
<span class=lbl>Only under these kinds of action (none ticked = any action)</span><div class=kinds id=kinds></div>
<label class=lbl><input type=checkbox id=norep> Do not repair this bug (the loop skips undo-repair and the stuck-run check for it)</label>
<button id=addbtn>Add to pending changes</button> <button id=canc hidden>Cancel edit</button> <span id=adderr class=bad></span></div></details>
<div id=sections></div></main>"""


def page_data() -> dict:
    rows = build_rows()
    for section in rows.values():
        for bug in section:
            for r in bug["runs"]:
                r["started"] = r["started"].strftime(TIME_FORMAT)
    try:
        invariants = invariant_choices()
    except Exception:   # the invariant plugins could not be loaded: fall back to the ids of the triage config
        cfg = load_config(DEFAULT_CONFIG_PATH)
        invariants = [{"id": i, "type": "", "text": t} for i, t in (cfg.get("invariants") or {}).items()]
    return {"bugs": rows, "invariants": invariants, "kinds": action_kinds(), "folder": str(update_folder().resolve())}


def render() -> str:
    data = json.dumps(page_data(), ensure_ascii=False).replace("</", "<\\/")
    sub = f"From triage_config.json and the recorded runs - generated {datetime.now():%Y-%m-%d %H:%M}"
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>Bugs discovered</title>"
            f"<style>{CSS}</style></head><body>{BODY.replace('__SUB__', html.escape(sub))}"
            f'<script id=data type="application/json">{data}</script><script>{JS}</script></body></html>')


def write_page() -> Path:
    KNOWN_BUGS_DIR.mkdir(parents=True, exist_ok=True)
    out = KNOWN_BUGS_DIR / REPORT_NAME
    out.write_text(render(), encoding="utf-8")
    return out


def update_folder() -> Path:
    """The folder the update file is saved in: the setting in triage_config.json, else BUG_UPDATES_DIR. Created if missing."""
    try:
        configured = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")).get(FOLDER_SETTING)
    except (OSError, ValueError):
        configured = None
    folder = Path(configured) if configured else BUG_UPDATES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _folders() -> list[Path]:
    """Where a new update file may be: the inbox (tests/output/inbox) first, then the browser's Downloads folder."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    return [INBOX_DIR, Path.home() / "Downloads"]


def _archive_update(file: Path) -> None:
    """Move an applied update file into the update folder (tests/output/bug_updates), so it is not picked up again."""
    target = update_folder() / file.name
    if file.resolve().parent == target.parent.resolve():
        return
    try:
        shutil.move(str(file), str(target))
        print(f"Moved {file.name} to {target.parent}")
    except OSError as exc:
        print(f"Could not move {file} to {target.parent}: {exc}")


def _newest_update(folders: list[Path], since: float, seen: set[Path]) -> Path | None:
    """Newest known_bugs_update_*.json (modified at or after `since`, not applied yet) in the first folder that has one:
    earlier folders win, even if a file in a later one is newer. A file whose name is already in the update folder was applied."""
    applied = {f.name for f in update_folder().glob(UPDATE_GLOB)}
    for d in folders:
        files = [f for f in d.glob(UPDATE_GLOB) if f.is_file() and f.stat().st_mtime >= since and f.resolve() not in seen and f.name not in applied]
        if files:
            return max(files, key=lambda f: f.stat().st_mtime)
    return None


def _open_and_apply(folders: list[Path]) -> int:
    """Open the page; every time Enter is pressed, apply the Update download that appeared since and rebuild the page."""
    page = write_page()
    webbrowser.open(page.resolve().as_uri())
    print(f"Page: {page.resolve().as_uri()}")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Not a terminal: click Update on the page, then run: python -m tests.cli bugs import")
        return EXIT_OK
    since, seen = time.time() - 2, set()   # -2: the clocks of the file system and of this process may differ a little
    while True:
        if input("\nClick Update on the page, then press Enter here (q = finish): ").strip().lower() in ("q", "quit"):
            return EXIT_OK
        file = _newest_update(folders, since, seen)
        if file is None:
            print(f"No new {UPDATE_GLOB} in {' or '.join(str(f) for f in folders)}. Click Update on the page first "
                  f"(the file lands in Downloads, or in {folders[0]} if you saved it there; if you saved it elsewhere, use: bugs import --json FILE).")
            continue
        seen.add(file.resolve())
        print(f"Applying {file} ...")
        if apply_update(file) != EXIT_OK:
            print("Nothing was changed. Fix the problem on the page and click Update again.")
            continue
        _archive_update(file)
        write_page()
        print("The page was rebuilt: press F5 in the browser to see the new state.")
        since = time.time() - 2


def apply_on_enter(folders: list[Path] | None = None, on_applied=None) -> None:
    """For the issue report: stay open, and every time Enter is pressed apply the bug entry saved on the report page since
    (the same update file and the same apply as the bugs page). q, Ctrl+C or Ctrl+D finish."""
    folders = folders or _folders()
    since, seen = time.time() - 2, set()
    print("\nLogging a bug from the report? Click \"Save bug entry\" there, then press Enter here to add it to the known bugs "
          "(q = finish).")
    while True:
        try:
            answer = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer.strip().lower() in ("q", "quit"):
            return
        file = _newest_update(folders, since, seen)
        if file is None:
            print(f"No new {UPDATE_GLOB} in {' or '.join(str(f) for f in folders)}. Click \"Save bug entry\" on the report first.")
            continue
        seen.add(file.resolve())
        print(f"Applying {file} ...")
        if apply_update(file) != EXIT_OK:
            print("Nothing was changed. Fix the problem on the report and save the bug entry again.")
            continue
        _archive_update(file)
        print("Done. Open the bugs page with: python -m tests.cli bugs")
        if on_applied is not None:
            try:
                on_applied()
            except Exception as e:
                print(f"Could not rebuild the report: {e}")
        since = time.time() - 2


def import_all(folder: Path | None = None) -> int:
    """Apply every update file, oldest first, each one on its own (a file that fails is reported and skipped, the others go on).
    New files (inbox, Downloads, or `folder`) are applied in full and then moved to the update folder.
    Files that are already in the update folder were applied before: only their additions are taken, and only those missing
    from the list. Returns EXIT_OK if every file was applied or had nothing to add."""
    archive = update_folder().resolve()
    sources = [folder] if folder else _folders() + [archive]
    done_names = {f.name for f in archive.glob(UPDATE_GLOB)}
    files: dict[str, tuple[Path, bool]] = {}   # file name -> (file, already applied before)
    for d in sources:
        d = Path(d)
        for f in d.glob(UPDATE_GLOB):
            applied_before = d.resolve() == archive or f.name in done_names
            if f.is_file() and f.name not in files:
                files[f.name] = (f, applied_before)
    if not files:
        print(f"No {UPDATE_GLOB} found in {', '.join(str(d) for d in sources)}.", file=sys.stderr)
        return EXIT_USAGE
    ordered = sorted(files.values(), key=lambda fa: fa[0].stat().st_mtime)
    failed: list[str] = []
    for f, applied_before in ordered:
        print(f"\n== {f.name}" + ("  (already applied before: only missing bugs are added)" if applied_before else ""))
        code = apply_update(f, skip_existing=True, additions_only=applied_before)
        if code != EXIT_OK:
            failed.append(f.name)
        elif not applied_before:
            _archive_update(f)
    print(f"\n{len(ordered) - len(failed)} of {len(ordered)} update file(s) processed" + (f"; NOT applied: {', '.join(failed)}" if failed else "."))
    return EXIT_USAGE if failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command, rest = (argv[0], argv[1:]) if argv and argv[0] in ("page", "import") else ("page", argv)
    raw = argparse.RawDescriptionHelpFormatter
    if command == "import":
        p = argparse.ArgumentParser(
            prog="python -m tests.cli bugs import", formatter_class=raw,
            description="Apply a saved known_bugs_update_*.json. Default: the newest new one in tests/output/inbox, else in Downloads; it is moved to tests/output/bug_updates afterwards.")
    else:
        p = argparse.ArgumentParser(
            prog="python -m tests.cli bugs", formatter_class=raw,
            description="Open the Known bugs page. Click Update on it, then press Enter here to apply.",
            epilog="Apply a saved update file instead: bugs import [--json FILE]")
    if command == "import":
        p.add_argument("--json", help="the update file (default: the newest new known_bugs_update_*.json in the inbox, else in Downloads)")
        p.add_argument("--folder", help="where to look (default: the inbox first, then your Downloads folder)")
        p.add_argument("--all", action="store_true", help="apply every update file, oldest first (new ones in the inbox and Downloads, "
                       "plus the missing bugs of the files already in the update folder); bugs already in the list are skipped")
        a = p.parse_args(rest)
        if a.all:
            if a.json:
                p.error("--all and --json cannot be combined")
            return import_all(Path(a.folder) if a.folder else None)
        file = Path(a.json) if a.json else None
        if file is None:
            for folder in ([Path(a.folder)] if a.folder else _folders()):   # the update folder wins over Downloads
                file = _newest_update([folder], 0, set())
                if file is not None:
                    break
        if file is None:
            print(f"No {UPDATE_GLOB} found in {a.folder or ' or '.join(str(f) for f in _folders())}; use --json FILE or --folder DIR.", file=sys.stderr)
            return EXIT_USAGE
        print(f"Applying {file}")
        code = apply_update(file)
        if code == EXIT_OK and not a.json:
            _archive_update(file)
        return code
    p.parse_args(rest)
    return _open_and_apply(_folders())


if __name__ == "__main__":
    sys.exit(main())