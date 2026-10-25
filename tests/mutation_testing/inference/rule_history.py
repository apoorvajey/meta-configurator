"""When a rule was first accepted, and every change of its review state since.

Nothing extra has to be recorded: the review folder already keeps every saved review
(config/rule_reviews/rule_review_<YYYYMMDD_HHMMSS>.json, never overwritten), so the history of a rule is derived by
walking those files oldest to newest. The time of an event is the time in the file name, i.e. when the review was saved
or downloaded from the review page (not when it was imported). A rule that is missing from a later file is "unreviewed"
again.

    python -m tests.cli review history              write rule_history.json and list the accepted rules by first acceptance
    python -m tests.cli review history INF-13916E7A the full log of one rule

rule_history.json (tests/output/reports/inference/) is rewritten after every review import; it can always be rebuilt.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tests.mutation_testing.inference.review_store import REVIEW_DIR, STAMP_FORMAT, _NAME_RE, review_files

HISTORY_PATH = "tests/output/reports/inference/rule_history.json"
INFERRED_RULES = "tests/output/inferred_rules.json"
TRACKED = ("status", "tier", "minSupport", "bug", "note")   # decision fields whose change is an event
UNREVIEWED = {"status": "unreviewed"}


def _stamp(path: Path) -> str:
    """ISO time (local, as saved) from the file name."""
    m = _NAME_RE.match(path.name)
    return datetime.strptime(m.group(1), STAMP_FORMAT).isoformat(timespec="seconds")


def _state(entry: dict | None) -> dict:
    if not isinstance(entry, dict):
        return dict(UNREVIEWED)
    return {k: entry[k] for k in TRACKED if entry.get(k) not in (None, "")}


def build_history(review_dir: str = REVIEW_DIR) -> dict:
    """{rule id: {first_seen, first_accepted, accepted_since, last_changed, current, events: [...]}}, from all reviews."""
    files = review_files(review_dir)
    history: dict[str, dict] = {}
    for path in files:
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue   # an unreadable file is not a state change
        at = _stamp(path)
        for rid in sorted(set(review) | set(history)):
            new = _state(review.get(rid))
            h = history.setdefault(rid, {"first_seen": at, "first_accepted": None, "accepted_since": None,
                                         "last_changed": at, "current": dict(UNREVIEWED), "events": []})
            old = h["current"]
            if new == old:
                continue
            h["events"].append({"at": at, "file": path.name, "from": old, "to": new})
            h["current"], h["last_changed"] = new, at
            if new.get("status") == "accepted" and old.get("status") != "accepted":
                h["accepted_since"] = at
                h["first_accepted"] = h["first_accepted"] or at
            elif new.get("status") != "accepted":
                h["accepted_since"] = None
    return {rid: h for rid, h in history.items() if h["events"]}


def write_history(review_dir: str = REVIEW_DIR, out: str = HISTORY_PATH) -> Path:
    data = build_history(review_dir)
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"built_from": str(review_dir), "rules": data}, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    return target


def _statements() -> dict[str, str]:
    try:
        rules = json.loads(Path(INFERRED_RULES).read_text(encoding="utf-8"))["rules"]
    except (OSError, ValueError, KeyError):
        return {}
    return {r["id"]: r.get("statement", "") for r in rules}


def _fmt(state: dict) -> str:
    extra = [f"{k}={state[k]}" for k in ("tier", "minSupport", "bug") if k in state]
    return state.get("status", "?") + (f" ({', '.join(extra)})" if extra else "")


def show(rule_id: str | None = None, review_dir: str = REVIEW_DIR) -> None:
    path = write_history(review_dir)
    data = json.loads(path.read_text(encoding="utf-8"))["rules"]
    texts = _statements()
    if rule_id:
        h = data.get(rule_id)
        if h is None:
            raise SystemExit(f"[History] {rule_id} was never in a review file in {review_dir}")
        print(f"{rule_id}  {texts.get(rule_id, '')}")
        print(f"  first seen {h['first_seen']} · first accepted {h['first_accepted'] or '-'} · "
              f"accepted since {h['accepted_since'] or '-'} · now: {_fmt(h['current'])}")
        for e in h["events"]:
            print(f"  {e['at']}  {_fmt(e['from'])}  ->  {_fmt(e['to'])}   [{e['file']}]")
        return
    accepted = sorted((h["first_accepted"], rid) for rid, h in data.items() if h["first_accepted"])
    print(f"[History] {len(data)} rule(s) with a review history, written to {path}")
    print("first accepted       rule           now          changes  statement")
    for first, rid in accepted:
        h = data[rid]
        print(f"{first}  {rid}  {h['current'].get('status', '?'):11s}  {len(h['events']):7d}  {texts.get(rid, '')[:70]}")


_CACHE: dict[str, dict] = {}


def rule_history(rule_id: str, review_dir: str = REVIEW_DIR) -> dict | None:
    """The history of one rule (built once per process), or None if it never appeared in a review file."""
    if review_dir not in _CACHE:
        _CACHE[review_dir] = build_history(review_dir)
    return _CACHE[review_dir].get(rule_id)


def _day(iso: str) -> str:
    return iso.replace("T", " ")[:16]


def accepted_note(rule_id: str, review_dir: str = REVIEW_DIR) -> str:
    """'accepted 2026-10-24 02:21' for a short tag in a failure message; '' if the rule is not accepted or unknown."""
    h = rule_history(rule_id, review_dir)
    if not h or not h["accepted_since"]:
        return ""
    return f"accepted {_day(h['accepted_since'])}"


def detail_rows(rule_id: str, review_dir: str = REVIEW_DIR) -> dict[str, str]:
    """Rows for the invariant report: first accepted, accepted since (if it differs) and the log of state changes."""
    h = rule_history(rule_id, review_dir)
    if not h:
        return {}
    rows = {"First accepted": _day(h["first_accepted"]) if h["first_accepted"] else "never"}
    if h["accepted_since"] and h["accepted_since"] != h["first_accepted"]:
        rows["Accepted since"] = _day(h["accepted_since"])
    rows["Review log"] = "\n".join(f"{_day(e['at'])}  {_fmt(e['from'])}  ->  {_fmt(e['to'])}" for e in h["events"])
    return rows