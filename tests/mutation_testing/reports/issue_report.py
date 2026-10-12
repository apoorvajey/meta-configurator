"""Problem report: one short list of distinct problems instead of one card per failure.

Layer 1: one line per problem (what breaks, after which action, in how many runs, how early).
Layer 2 (click a line): what is wrong, what you would see, the last actions to reproduce it, copy buttons (replay
command, GitHub issue text) and a screenshot.
Layer 3 (inside layer 2): technical details (check message, where it likely started, other runs).

Failures are grouped by (rules that broke first, kind of action that triggered it), so a problem seen in 40 runs is
one line. Known bugs and test-tool problems (crashed checks, failed actions) go to collapsed sections.
Settings: triage_config.json, block "issue_report".
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from tests.mutation_testing.common.paths import ISSUES_DIR
from tests.mutation_testing.runs.run_layout import RUNS_DIR, original_history, run_dir_of, run_id_of
from tests.mutation_testing.reports.triage.run_loading import DEFAULT_CONFIG_PATH, _rel, find_log_files, find_run_logs, load_config, load_run, parse_time_arg, run_start_time
from tests.mutation_testing.reports.triage.episodes import _panel, assign_tier, build_episodes, look_for_text, repro_steps, rule_text, split_invariant, step_label, trigger_index
from tests.mutation_testing.reports.triage.candidates import _snapshot_dir, draft_issue
from tests.mutation_testing.reports.triage.action_text import parse_action
from tests.mutation_testing.reports.triage.screenshots import shots_for_step
from tests.mutation_testing.reports.triage import causal_analysis as C
from tests.mutation_testing.inference.common import leaf_changes
from tests.mutation_testing.common.report_theme import ISSUE_REPORT_DARK


def _e(x) -> str:
    return html.escape(str(x), quote=True)


def _attr(text: str) -> str:
    return _e(text).replace("\n", "&#10;")


# classification
def onset_rules(ep, cfg: dict) -> dict[str, str]:
    """Rules that broke at the FIRST failing step of the episode (later ones are usually fallout)."""
    i = ep.fail_steps[0] if ep.fail_steps else ep.start
    out: dict[str, str] = {}
    for entry in ep.run.steps[i].get("failed_invariants") or []:
        if not isinstance(entry, str):
            continue
        parsed = split_invariant(entry)
        if parsed:
            out[parsed[0]] = parsed[1]
        else:
            rid, _, rest = entry.partition(" ")
            out[rid] = rest or rid
    return out or dict(ep.invariants)


BLANK_GUI_ID = "INV-D4:blank"   # title lookup only, not a real rule id


def _onset_message(ep, rid: str) -> str:
    i = ep.fail_steps[0] if ep.fail_steps else ep.start
    for ev in ep.run.steps[i].get("failure_evidence") or []:
        if isinstance(ev, dict) and ev.get("id") == rid:
            return ev.get("message") or ""
    return ""


def is_blank_gui(ep, rules: dict, cfg: dict) -> bool:
    """INV-D4 failed at onset with the 'GUI is blank' message (every data key lacks a row)."""
    prefix = cfg.get("issue_report", {}).get("blank_gui_prefix", "GUI is blank")
    return "INV-D4" in rules and _onset_message(ep, "INV-D4").startswith(prefix)


def classify(ep, cfg: dict) -> tuple[str, dict[str, str]]:
    """-> ('issue' | 'tool' | 'ignore', rules)."""
    if ep.tier == "artifact":
        return "ignore", {}
    retired = set(cfg["retired_invariants"])
    rules = {k: v for k, v in onset_rules(ep, cfg).items() if k not in retired}
    tool = {k: v for k, v in rules.items() if k.startswith("CHK-ERR-")}
    real = {k: v for k, v in rules.items() if k not in tool}
    if real:
        if is_blank_gui(ep, real, cfg):
            # no rows means no stars and no icons, so these are fallout, not separate problems
            implied = set(cfg.get("issue_report", {}).get("blank_gui_implies", ["INV-D3", "INV-D6"]))
            real = {k: v for k, v in real.items() if k not in implied}
        return "issue", real
    if tool:
        return "tool", tool
    return "ignore", {}


def _kind(run, i: int) -> dict:
    return parse_action(run.steps[i].get("action_id", ""))


def fingerprint(ep, rules: dict, cfg: dict) -> tuple:
    p = _kind(ep.run, trigger_index(ep))
    key = (tuple(sorted(rules)), p["kind"], p.get("editor") or p.get("standalone") or "")
    # "blank" is appended only for blank GUIs, so every other problem keeps its P-xxxxxx id
    return key + ("blank",) if is_blank_gui(ep, rules, cfg) else key


def match_known(ep, rules: dict, cfg: dict) -> dict | None:
    """A known bug matches on its rule(s) AND, when it names them, the kind of action involved
    (when_action_kinds)."""
    fail_kind = _kind(ep.run, ep.start)["kind"]
    trig_kind = _kind(ep.run, trigger_index(ep))["kind"]
    for bug in cfg["known_bugs"]:
        if not set(rules) & set(bug["invariants"]):
            continue
        kinds = bug.get("when_action_kinds")
        if kinds and fail_kind not in kinds and trig_kind not in kinds:
            continue
        return bug
    return None


def repro_len(ep) -> int:
    """Number of steps from a fresh load up to and including the first failing step."""
    return sum(1 for i in range(ep.start + 1) if ep.run.kinds[i] != "walkback")


class Issue:
    def __init__(self, key: tuple, members: list):
        self.key = key
        self.members = members                          # [(Episode, rules)]
        self.eps = [m[0] for m in members]
        self.best = min(self.eps, key=lambda e: (repro_len(e), e.run.run_id))   # shortest sequence
        self.repro = repro_len(self.best)
        self.rules = dict(members[0][1])
        self.runs = {e.run.run_id for e in self.eps}
        self.uid = "P-" + hashlib.sha1(repr(key).encode()).hexdigest()[:6]
        self.known = None
        self.inferred = False
        self.title = ""
        self.trigger = ""


def build_issues(eps: list, runs: list, cfg: dict) -> tuple[list[Issue], Counter, list]:
    groups: dict[tuple, list] = defaultdict(list)
    tool_eps = []
    for ep in eps:
        kind, rules = classify(ep, cfg)
        if kind == "issue":
            groups[fingerprint(ep, rules, cfg)].append((ep, rules))
        elif kind == "tool":
            tool_eps.append((ep, rules))
    prefixes = tuple(cfg.get("issue_report", {}).get("inferred_rule_prefixes", ["INF-"]))
    issues = []
    for key, members in groups.items():
        iss = Issue(key, members)
        bugs = [match_known(e, r, cfg) for e, r in members]
        if bugs and all(b is not None for b in bugs) and len({b["id"] for b in bugs}) == 1:
            iss.known = bugs[0]
        iss.inferred = all(r.startswith(prefixes) for r in iss.rules)
        primary = primary_rule(iss, prefixes)
        extra = len(iss.rules) - 1
        trig = step_label(iss.best.run, trigger_index(iss.best), cfg, include_path=False)
        title_id = BLANK_GUI_ID if iss.key[-1] == "blank" else primary
        iss.title = rule_name(title_id, iss.rules.get(primary, ""), cfg) + (f" (+{extra} more)" if extra else "")
        iss.trigger = trig
        issues.append(iss)
    issues.sort(key=lambda i: (i.inferred, -len(i.runs), i.repro))
    harness = Counter()
    for r in runs:
        for i, s in enumerate(r.steps):
            if s.get("action_succeeded") is False:
                harness[_kind(r, i)["kind"]] += 1
    return issues, harness, tool_eps


def primary_rule(iss: Issue, prefixes: tuple) -> str:
    return sorted(iss.rules, key=lambda r: (r.startswith(prefixes), r))[0]


DEFAULT_SHORT_NAMES = {
    "INV-D1": "Data Text View differs from the internal data",
    "INV-D3": "Required-field stars in the GUI are wrong",
    "INV-D4": "GUI rows do not match the data (orphaned or missing row)",
    "INV-D4:blank": "The GUI is blank (no data key has a row)",
    "INV-D5": "String values in the GUI differ from the data",
    "INV-D6": "Validation icons in the GUI are wrong",
    "INV-S0": "The schema is no longer a valid object",
    "INV-S1": "Schema Text View differs from the internal schema",
    "INV-S2": "A top-level schema key has no GUI row",
    "INV-S3": "Schema title field differs from the internal schema",
    "INV-GUI-ERR-01": "Text View shows a validation error the GUI does not",
    "TEMP-ADD-03": "Adding a required item does not reach the data",
}


def rule_name(rid: str, logged: str, cfg: dict) -> str:
    """A complete sentence fragment, never cut off: short name if known, else the full rule text."""
    short = {**DEFAULT_SHORT_NAMES, **cfg.get("issue_report", {}).get("short_names", {})}
    if rid in short:
        return short[rid]
    t = (cfg["invariants"].get(rid) or logged or rid).strip()
    return t[:1].upper() + t[1:]


# rendering pieces
def _lead(iss: Issue, cfg: dict, primary: str) -> str:
    ep = iss.best
    if not C.has_causal_fields(ep.run):
        return ""
    try:
        a = C.analyse(ep, primary, cfg, parse_action, lambda s: _panel(s, cfg))
    except Exception:
        return ""
    if a["lead"]:
        r = a["lead"]
        cands = a.get("candidates") or []
        many = (f" (one of {len(cands)} candidates: steps {', '.join(str(c['i'] + 1) for c in cands)})"
                if len(cands) > 1 else "")
        return f"Likely started at step {r['i'] + 1}: {step_label(ep.run, r['i'], cfg)}{many}."
    return "Started at the failing step itself."


def _embed(path: Path, cfg: dict) -> tuple[str, str]:
    """Data URI for a screenshot. Keeps full quality: small PNGs are embedded untouched; big ones are
    only downscaled above screenshot_max_width and saved as high-quality JPEG."""
    ir = cfg.get("issue_report", {})
    maxw, quality = ir.get("screenshot_max_width", 1800), ir.get("screenshot_quality", 92)
    data = path.read_bytes()
    mime = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    try:
        import io
        from PIL import Image
        with Image.open(path) as im:
            if im.width > maxw or len(data) > 1_500_000:
                if im.width > maxw:
                    im = im.resize((maxw, round(im.height * maxw / im.width)), Image.LANCZOS)
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True, subsampling=0)
                data, mime = buf.getvalue(), "image/jpeg"
    except Exception:
        pass
    return mime, base64.b64encode(data).decode("ascii")


def _screens_html(ep, cfg: dict) -> str:
    if not cfg["screenshots"].get("enabled"):
        return ""
    figs = []
    wanted = ([(ep.start - 1, "Before")] if ep.start > 0 else []) + [(ep.start, "When it fails")]
    for i, label in wanted:
        try:
            for p in shots_for_step(ep.run, i, cfg):
                mime, b64 = _embed(p, cfg)
                figs.append(
                    f'<figure><a href="{_e(p.resolve().as_uri())}" target="_blank" title="Open the original file">'
                    f'<img src="data:{mime};base64,{b64}" alt=""></a>'
                    f'<figcaption><b>{label}</b> &middot; step {i + 1}: {_e(step_label(ep.run, i, cfg))}</figcaption></figure>')
        except Exception:
            continue
    if not figs:
        return ""
    return ('<details class="sub"><summary>Screenshots (click an image to open the original)</summary>'
            + "".join(figs) + "</details>")


def _log_uri(ep, cfg: dict) -> str | None:
    try:
        replay_log = ep.run.path.parent / "replay_log.html"
        if replay_log.is_file():
            return replay_log.resolve().as_uri() + f"#step-{ep.start + 1}"
        for f in find_run_logs(ep.run, cfg):
            if f.suffix.lower() == ".html":
                return f.resolve().as_uri() + f"#step-{ep.start + 1}"
    except Exception:
        pass
    return None


def _diff_table(rows: list[dict], total: int, cols: tuple[str, str, str] = ("field", "before", "after")) -> str:
    body = "".join(f"<tr><td><code>{_e(r['path'])}</code></td><td><code>{_e(r.get('before'))}</code></td>"
                   f"<td><code>{_e(r.get('after'))}</code></td></tr>" for r in rows)
    more = f'<p class="muted">&hellip; and {total - len(rows)} more field(s) not listed</p>' if total > len(rows) else ""
    return (f'<table class="diff"><tr><th>{_e(cols[0])}</th><th>{_e(cols[1])}</th><th>{_e(cols[2])}</th></tr>{body}</table>{more}')


def _details_html(details: dict) -> str:
    """What the mutation log shows for a failed check, as tables: the exact differing fields where the details
    carry them, otherwise the details as JSON."""
    parts = []
    asserts = details.get("asserts")
    if asserts:
        parts.append(f'<p><b>The rule asserts:</b> {_e(asserts)}</p>')
    definitions = details.get("definitions")
    if definitions and definitions != "-":
        parts.append(f'<p class="muted"><b>Where:</b> {_e(definitions)}</p>')
    violations = details.get("violations")
    if isinstance(violations, list) and violations:
        for n, v in enumerate(violations, 1):
            ch = v.get("changes") if isinstance(v, dict) else None
            if isinstance(ch, list) and ch:
                total = v.get("changed_fields", len(ch))
                parts.append(f'<p><b>Observation {n}:</b> {total} field(s) changed</p>' + _diff_table(ch, total))
            else:
                parts.append(f'<p><b>Observation {n}:</b></p><pre>{_e(json.dumps(v, indent=2, default=str, ensure_ascii=False))}</pre>')
        rest = {k: v for k, v in details.items() if k not in ("violations", "asserts", "definitions")}
    else:
        rest = {k: v for k, v in details.items() if k != "asserts"}
        left = next((k for k in ("internal_state", "internal_schema") if k in rest), None)
        if left and "text_view" in rest:
            rows, total = leaf_changes(rest.pop(left), rest.pop("text_view"), limit=40)
            if rows:
                parts.append(f'<p>{total} field(s) differ</p>'
                             + _diff_table(rows, total, ("field", "internal " + ("data" if left == "internal_state" else "schema"), "Text View")))
    if rest:
        parts.append(f'<pre>{_e(json.dumps(rest, indent=2, default=str, ensure_ascii=False))}</pre>')
    return "".join(parts)


def _rule_card(rid: str) -> dict:
    try:
        from tests.mutation_testing.inference.inferred_rules import load_inferred_rules
        for r in load_inferred_rules():
            if r.get("id") == rid:
                return r
    except Exception:
        pass
    return {}


def _report_html(ep, rid: str, cfg: dict, log_link: str = "") -> str:
    """The failed check as the mutation log shows it: name, message, rule description, details, evidence."""
    i = ep.fail_steps[0] if ep.fail_steps else ep.start
    entry = next((ev for ev in ep.run.steps[i].get("failure_evidence") or []
                  if isinstance(ev, dict) and ev.get("id") == rid), None)
    if entry is None:
        return ""
    full = next((n for n in ep.run.steps[i].get("failed_invariants") or [] if str(n).startswith(rid)), rid)
    out = [f'<p class="lbl">What the check reported (step {i + 1})</p><div class="rep">',
           f'<div class="repname">&#10007; {_e(full)}</div>']
    if entry.get("message"):
        out.append(f'<div class="repmsg">{_e(entry["message"])}</div>')
    card = _rule_card(rid).get("card") or {}
    if card.get("applies_when") or card.get("trigger"):
        out.append('<p class="muted"><b>Checked when:</b> '
                   f'{_e(card.get("trigger", ""))}; <b>applies when:</b> {_e(card.get("applies_when", ""))}</p>')
    if entry.get("details"):
        out.append(_details_html(entry["details"]))
    else:
        out.append('<p class="muted">No field-level details were saved for this failure '
                   '(a run recorded before details were kept, or a check that reports only a message). '
                   + (f'The run log has the full report: {log_link}.' if log_link else 'See the run log.') + '</p>')
    if entry.get("evidence"):
        items = "".join(f'<div class="ev">{_e(x)}</div>' for x in entry["evidence"])
        out.append(f'<details class="sub"><summary>Evidence ({len(entry["evidence"])} checks)</summary>{items}</details>')
    out.append('</div>')
    return "".join(out)


def _step_li(s: dict) -> str:
    label = _e(s["label"].replace("Test tool: ", "(tool) "))
    hint = f' <span class="muted">({_e(s["hint"])})</span>' if s["hint"] else ""
    mark = ' <b class="here">&larr; problem appears here</b>' if s["failed"] else ""
    return f'<li value="{s["step"] + 1}" class="{"fail" if s["failed"] else ""}">{label}{hint}{mark}</li>'


def _example_html(ep, cfg: dict, limit: int, first: bool, rid: str = "") -> str:
    run = ep.run
    full = repro_steps(ep, cfg, full=True)
    complete = len(full) <= limit
    shown = full if complete else full[-limit:]
    heading = (f"Complete sequence from a fresh load &mdash; {len(full)} steps" if complete
               else f"Last {len(shown)} of {len(full)} steps before the failure")
    log = _log_uri(ep, cfg)
    link = (f'<a class="log" href="{_e(log)}" target="_blank" onclick="event.stopPropagation()">'
            f'Open run log at step {ep.start + 1} &#8599;</a>' if log else '<span class="muted">run log not found</span>')
    loc = run_dir_of(run.path)   # tests/output/runs/<run_id> (no tag, no spaces)
    replay = ("python -m tests.mutation_testing.replay.replay_runner --history "
              + (f"{RUNS_DIR.as_posix()}/{run_id_of(loc[0].name)}" if loc else _rel(original_history(run.path))))
    steps_for_issue = full if len(full) <= 12 else repro_steps(ep, cfg)
    issue_text = draft_issue(ep, cfg, steps_for_issue)
    if len(full) <= 12:
        issue_text = issue_text.replace(
            "(the last part of a longer session; if it does not reproduce, earlier actions may matter)",
            "(complete sequence from a fresh load)")
    more = ""
    if not complete:
        more = (f'<details class="sub"><summary>Show all {len(full)} steps</summary>'
                f'<ol class="steps">{"".join(_step_li(s) for s in full)}</ol></details>')
    return (
        f'<details class="ex"{" open" if first else ""}><summary>'
        f'<span class="exrun">{_e(run.run_id)}</span>'
        f'<span class="muted"> &middot; {_e(run.schema)} &middot; fails at step {ep.start + 1} of {len(run.steps)}</span>'
        f'<span class="exlink">{link}</span></summary><div class="exbody">'
        f'<p class="muted">Setup: load schema &ldquo;{_e(run.schema)}&rdquo; and data file &ldquo;{_e(run.data_file)}&rdquo;.</p>'
        + (_report_html(ep, rid, cfg, link) if rid else "") +
        f'<p class="lbl">{heading}</p><ol class="steps">{"".join(_step_li(s) for s in shown)}</ol>{more}'
        f'<p class="btns"><button data-text="{_attr(replay)}">Copy replay command</button>'
        f'<button data-text="{_attr(issue_text)}">Copy GitHub issue text</button></p>'
        + (_screens_html(ep, cfg) if first else "") + '</div></details>')


def _issue_html(n: int, iss: Issue, cfg: dict, prefixes: tuple) -> str:
    ir = cfg.get("issue_report", {})
    limit, n_examples = ir.get("max_repro_steps", 6), ir.get("max_examples", 5)
    primary = primary_rule(iss, prefixes)
    msg, paths = _evidence(iss, cfg, primary)
    look = cfg.get("invariant_details", {}).get(primary, {}).get("look_for")
    what = look or (f"The check reports: {msg}" if msg else look_for_text(primary, cfg))

    per_run: dict[str, tuple[int, object]] = {}
    for e in iss.eps:
        k = repro_len(e)
        if e.run.run_id not in per_run or k < per_run[e.run.run_id][0]:
            per_run[e.run.run_id] = (k, e)
    ordered = [e for _, e in sorted(per_run.values(), key=lambda t: (t[0], t[1].run.run_id))]
    examples, rest = ordered[:n_examples], ordered[n_examples:]
    ex_html = "".join(_example_html(e, cfg, limit, first=(i == 0), rid=primary) for i, e in enumerate(examples))
    rest_html = (f'<p class="muted">Also in {len(rest)} more run{"s" if len(rest) != 1 else ""}: '
                 f'{_e(", ".join(e.run.run_id for e in rest[:12]))}{" …" if len(rest) > 12 else ""}</p>' if rest else "")

    rules_li = "".join(f"<li><code>{_e(r)}</code> {_e(rule_text(r, iss.rules[r], cfg))}</li>" for r in sorted(iss.rules))
    lead = _lead(iss, cfg, primary)
    snap = _snapshot_dir(iss.best.run, cfg) / f"step_{iss.best.start}_after.json"
    tech = ('<details class="sub"><summary>Technical details</summary><div class="tech">'
            f'<p><b>Rules that broke first</b></p><ul>{rules_li}</ul>'
            + (f'<p><b>Check said:</b> {_e(msg)}</p>' if msg else "")
            + (f'<p><b>Paths named:</b> {", ".join(f"<code>{_e(p)}</code>" for p in paths[:6])}</p>' if paths else "")
            + (f"<p>{_e(lead)}</p>" if lead else "")
            + (f"<p>State before/after: <code>{_e(_rel(snap.parent))}</code></p>" if snap.is_file() else
               "<p class=\"muted\">No state snapshot for this run yet.</p>")
            + f'<p class="muted">Problem id {iss.uid}</p></div></details>')

    n_runs = len(iss.runs)
    badges = ""
    if iss.inferred:
        badges += '<span class="badge inf" title="Found only by an automatically mined rule; the rule itself may be wrong">inferred rule</span>'
    if iss.known:
        badges += f'<span class="badge known">known: {_e(iss.known["id"])}</span>'
    return (
        f'<details class="issue"><summary class="row"><span class="n">{n}</span>'
        f'<span class="t"><span class="title">{_e(iss.title)}</span>{badges}'
        f'<span class="sub">after: {_e(iss.trigger)}</span></span>'
        f'<span class="c"><b>{n_runs}</b> run{"s" if n_runs != 1 else ""}</span>'
        f'<span class="c"><b>{iss.repro}</b> step{"s" if iss.repro != 1 else ""}</span></summary>'
        f'<div class="body"><p class="lbl">What you would see</p><p>{_e(what)}</p>'
        f'<p class="lbl">Examples ({len(examples)} of {n_runs} run{"s" if n_runs != 1 else ""}, shortest first)</p>'
        f'{ex_html}{rest_html}{tech}</div></details>')


def _evidence(iss: Issue, cfg: dict, primary: str) -> tuple[str, list[str]]:
    ep = iss.best
    i = ep.fail_steps[0] if ep.fail_steps else ep.start
    for ev in ep.run.steps[i].get("failure_evidence") or []:
        if isinstance(ev, dict) and ev.get("id") == primary:
            return ev.get("message") or "", [str(p) for p in ev.get("paths") or []]
    return "", []


def _list_html(issues: list[Issue], cfg: dict, prefixes: tuple, start: int = 1) -> str:
    head = ('<div class="row head"><span class="n">#</span><span class="t">Problem</span>'
            '<span class="c">Seen in</span><span class="c" title="Steps from a fresh load to the failure, '
            'in the shortest run where it happened">Repro length</span></div>')
    return head + "".join(_issue_html(n, i, cfg, prefixes) for n, i in enumerate(issues, start))


def _tool_html(tool_eps: list, harness: Counter, cfg: dict) -> str:
    by_rule: dict[str, list] = defaultdict(list)
    for ep, rules in tool_eps:
        for rid in rules:
            by_rule[rid].append(ep)
    rows = []
    for rid, eps in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        ep = eps[0]
        i = ep.fail_steps[0] if ep.fail_steps else ep.start
        msg = next((ev.get("message", "") for ev in ep.run.steps[i].get("failure_evidence") or []
                    if isinstance(ev, dict) and ev.get("id") == rid), "")
        rows.append(f"<tr><td><code>{_e(rid)}</code></td><td>{len(eps)}</td>"
                    f"<td>{len({e.run.run_id for e in eps})}</td><td>{_e(msg[:160])}</td></tr>")
    out = ""
    if rows:
        out += ("<p><b>Checks that crashed</b> (the check itself broke; the app may be fine)</p>"
                "<table><tr><th>Check</th><th>Times</th><th>Runs</th><th>Error</th></tr>" + "".join(rows) + "</table>")
    if harness:
        out += ("<p><b>Actions the test tool could not perform</b></p><p>"
                + ", ".join(f"{_e(k)} &times;{v}" for k, v in harness.most_common(8)) + "</p>")
    return out or "<p>None.</p>"


CSS = """
*{box-sizing:border-box}
body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;margin:0;padding:28px 16px 60px;color:#1f2430;background:#f4f5f8}
.wrap{max-width:1040px;margin:0 auto}
h1{font-size:24px;margin:0 0 10px;letter-spacing:-.01em}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.chip{background:#fff;border:1px solid #dcdfe6;border-radius:999px;padding:3px 12px;font-size:13.5px;color:#444}
.chip.strong{background:#1f4fbf;border-color:#1f4fbf;color:#fff;font-weight:600}
.hint{color:#6b7280;font-size:13.5px;margin:2px 0 16px}
.list{background:#fff;border:1px solid #dcdfe6;border-radius:10px;overflow:hidden}
.row{display:grid;grid-template-columns:2.4rem 1fr 6rem 8.5rem;gap:12px;align-items:center;padding:12px 16px}
.row.head{font-size:11.5px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;background:#fafbfc;border-bottom:1px solid #e5e7eb;padding-top:8px;padding-bottom:8px}
details.issue{border-bottom:1px solid #eceef2}details.issue:last-child{border-bottom:0}
details.issue>summary{cursor:pointer;list-style:none}details.issue>summary::-webkit-details-marker{display:none}
details.issue>summary:hover{background:#f7f9fd}details[open].issue>summary{background:#eef3fc}
.n{color:#9aa1ad;text-align:right;font-variant-numeric:tabular-nums}
.t{display:block;min-width:0}.title{display:block;font-weight:600;font-size:15.5px;line-height:1.35}
.t .sub{display:block;color:#5b6472;font-size:13.5px;margin-top:2px}
.c{text-align:right;white-space:nowrap;color:#444;font-size:14px}.c b{font-size:16px}
.badge{display:inline-block;font-size:11px;border:1px solid #c4c9d4;border-radius:999px;padding:0 8px;color:#5b6472;margin:3px 6px 0 0;vertical-align:middle}
.badge.known{border-color:#d9b75a;color:#8a6410;background:#fff8e5}.badge.inf{border-color:#b8a9e0;color:#5a45a0;background:#f4f0ff}
.body{padding:4px 20px 18px 56px;background:#fbfcfe;border-top:1px solid #e5e9f2}
.lbl{font-size:11.5px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;margin:16px 0 6px;font-weight:600}
.body p{margin:4px 0}.muted{color:#6b7280;font-size:13px}
details.ex{background:#fff;border:1px solid #dfe3ea;border-radius:8px;margin:8px 0}
details.ex>summary{cursor:pointer;list-style:none;display:flex;gap:6px;align-items:baseline;padding:9px 12px;flex-wrap:wrap}
details.ex>summary::-webkit-details-marker{display:none}details.ex>summary:before{content:"\\25B8";color:#8a93a3;margin-right:2px}
details[open].ex>summary:before{content:"\\25BE"}
.exrun{font-weight:600;font-family:ui-monospace,Consolas,monospace;font-size:13.5px}.exlink{margin-left:auto}
a.log{font-size:13px;color:#1f4fbf;text-decoration:none;border:1px solid #c6d4f2;border-radius:6px;padding:2px 9px;background:#f3f7ff}
a.log:hover{background:#e3edff}
.exbody{padding:0 14px 12px;border-top:1px solid #eef0f4}
ol.steps{margin:6px 0 8px;padding-left:34px}ol.steps li{padding:3px 8px;border-radius:4px;margin:1px 0}
ol.steps li::marker{color:#8a93a3;font-variant-numeric:tabular-nums}
li.fail{background:#fdecea;color:#9b1c1c}.here{font-weight:600}
.btns{margin-top:10px}.btns button{font:inherit;font-size:13px;padding:5px 12px;margin-right:8px;border:1px solid #b7bfcd;background:#fff;border-radius:6px;cursor:pointer}
.btns button:hover{background:#eef3fc;border-color:#8ea6d8}
details.sub{margin:10px 0 2px}details.sub>summary{cursor:pointer;color:#1f4fbf;font-size:13.5px}
.tech{font-size:13.5px;border-left:3px solid #dfe3ea;padding-left:14px;margin-top:8px}.tech ul{margin:4px 0 6px 18px;padding:0}
figure{margin:12px 0}figure img{max-width:100%;border:1px solid #cfd4dd;border-radius:6px;display:block}
figcaption{font-size:12.5px;color:#5b6472;margin-top:4px}
details.fold{margin-top:22px;background:#fff;border:1px solid #dcdfe6;border-radius:10px;padding:10px 16px}
details.fold>summary{cursor:pointer;font-weight:600}details.fold .list{margin-top:10px}
table{border-collapse:collapse;font-size:13px;margin:6px 0}td,th{border:1px solid #dfe3ea;padding:4px 10px;text-align:left}th{background:#f4f5f8}
code{font-size:12.5px;background:#eef0f4;padding:1px 5px;border-radius:4px}
.rep{border:1px solid #f0c9c4;border-left:4px solid #d64545;border-radius:6px;background:#fff;padding:8px 14px;margin:6px 0}
.repname{font-weight:600;color:#9b1c1c}.repmsg{margin:4px 0 6px;font-size:13.5px}
table.diff{width:100%;table-layout:fixed}table.diff td,table.diff th{overflow-wrap:anywhere;vertical-align:top}
table.diff td code{background:none;padding:0;white-space:pre-wrap}
pre{background:#f4f5f8;border:1px solid #dfe3ea;border-radius:6px;padding:8px 10px;font-size:12.5px;overflow:auto;max-height:340px}
.ev{font-size:12.5px;border-left:3px solid #dfe3ea;padding:2px 8px;margin:3px 0;color:#444}
@media(max-width:720px){.row{grid-template-columns:1.6rem 1fr}.row .c{grid-column:2;text-align:left}.body{padding-left:20px}}
"""

JS = """
function fb(t,d){var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();
try{document.execCommand('copy');d()}catch(e){}a.remove()}
document.addEventListener('click',function(e){var b=e.target.closest('button[data-text]');if(!b)return;
var t=b.dataset.text,o=b.textContent,d=function(){b.textContent='Copied \\u2713';setTimeout(function(){b.textContent=o},1300)};
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(d,function(){fb(t,d)})}else{fb(t,d)}});
"""

def _sources_html(requested: list, runs: list, outside: int, clean: int = 0, unfinished: list | None = None) -> str:
    folders = ", ".join(_e(str(Path(p).resolve())) for p in requested)
    skipped = f" {outside} run(s) outside the time window were skipped." if outside else ""
    skipped += f" {clean} clean run(s) were left out (RUN_CLEAN.json)." if clean else ""
    skipped += (f" {len(unfinished)} unfinished run(s) have no history.json (crashed or stopped early): "
                + ", ".join(_e(u) for u in unfinished) + ".") if unfinished else ""
    rows = "".join(
        f"<tr><td>{_e(r.run_id)}{f' (replay {_e(r.replay_id)})' if r.replay_id else ''}</td><td>{_e(r.schema)}</td><td>{_e(r.data_file)}</td>"
        f"<td>{len(r.steps)}</td>"
        f"<td>{_e(r.started.strftime('%Y-%m-%d %H:%M') if r.started else '?')}</td>"
        f"<td>{_e(r.weights_profile or '-')}</td></tr>"
        for r in sorted(runs, key=lambda r: r.started or datetime.min)
    )
    return (f"<details class='fold'><summary>Reports analysed: {len(runs)} run(s)</summary>"
            f"<p class='hint'>Folder(s): {folders}.{skipped}</p>"
            "<table><tr><th>Run</th><th>Schema</th><th>Data file</th><th>Steps</th>"
            f"<th>Started</th><th>Weights</th></tr>{rows}</table></details>")


ORIGIN_ENV = "MT_ORIGIN_COMMAND"   # set by tests/cli.py: the command line the user ran
RUN_ID_FORMAT = "%Y%m%d_%H%M%S"    # accepted by --since / --until (see triage_config.json, time_filter)


def _quote(arg: str) -> str:
    return f'"{arg}"' if (not arg or " " in arg) else arg


def _repeat_command(args, requested_given: bool, since, until) -> str:
    """The report command with the time window written out as fixed times (so --last-hours can be repeated)."""
    parts = ["python -m tests.cli report"]
    if requested_given:
        parts += [_quote(str(p)) for p in args.paths]
    if since:
        parts += ["--since", since.strftime(RUN_ID_FORMAT)]
    if since or until:
        # Always write the end out too: a --since-only command run later would also pick up newer runs.
        parts += ["--until", (until or datetime.now()).strftime(RUN_ID_FORMAT)]
    if args.config != DEFAULT_CONFIG_PATH:
        parts += ["--config", _quote(str(args.config))]
    return " ".join(parts)


def _command_html(started_with: str, repeat: str) -> str:
    def block(title: str, hint: str, text: str) -> str:
        return (f"<h3>{_e(title)}</h3><p class='hint'>{_e(hint)}</p><pre style='background:#14161b;color:#e8eaf0;border-color:#2c313b;white-space:pre-wrap;word-break:break-all'>{_e(text)}</pre>"
                f"<p class='btns'><button data-text=\"{_attr(text)}\">Copy command</button></p>")
    body = ""
    if started_with:
        body += block("Started with", "Run from the repository root. This repeats the whole thing, new runs included.",
                      started_with)
    body += block("Build this report again",
                  "Same time window (--since to --until) and folders, so it gives the same set of runs even if newer ones "
                  "exist. Use it after code changes. To include runs started later, remove --until or move it forward.", repeat)
    return f"<details class='fold'><summary>How to repeat this report</summary>{body}</details>"


def render(runs: list, issues: list[Issue], harness: Counter, tool_eps: list, cfg: dict, window_note: str,
           sources_html: str = "", frame: str = "", command_html: str = "") -> str:
    prefixes = tuple(cfg.get("issue_report", {}).get("inferred_rule_prefixes", ["INF-"]))
    new = [i for i in issues if not i.known]
    known = [i for i in issues if i.known]
    cap = cfg.get("issue_report", {}).get("max_listed", 25)
    n_steps = sum(len(r.steps) for r in runs)
    plural = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Test results</title>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'><style>{CSS}{ISSUE_REPORT_DARK}</style></head>"
             "<body><div class='wrap'><h1>Test results</h1><div class='chips'>"
             f"<span class='chip strong'>{plural(len(new), 'new problem')}</span>"
             f"<span class='chip'>{plural(len(known), 'known bug')} seen again</span>"
             f"<span class='chip'>{plural(len(runs), 'run')} &middot; {n_steps} steps</span>"
             + (f"<span class='chip'>{_e(frame)}</span>" if frame else "") + "</div>"
             f"<p class='hint'>{_e(window_note + ' ') if window_note else ''}Click a problem for what you would see and "
             "how to reproduce it. Step numbers match the run logs.</p>"]
    parts.append(sources_html)
    parts.append(command_html)
    if new:
        parts.append("<div class='list'>" + _list_html(new[:cap], cfg, prefixes) + "</div>")
        if len(new) > cap:
            parts.append(f"<details class='fold'><summary>{len(new) - cap} more problems (seen in fewer runs)</summary>"
                         "<div class='list'>" + _list_html(new[cap:], cfg, prefixes, start=cap + 1) + "</div></details>")
    else:
        parts.append("<p>No new problems found in these runs.</p>")
    if known:
        parts.append(f"<details class='fold'><summary>Known bugs seen again ({len(known)})</summary><div class='list'>"
                     + _list_html(known, cfg, prefixes, start=len(new) + 1) + "</div></details>")
    parts.append(f"<details class='fold'><summary>Test-tool problems, not app bugs "
                 f"({plural(len(tool_eps), 'crashed check')}, {plural(sum(harness.values()), 'failed action')})</summary>"
                 + _tool_html(tool_eps, harness, cfg) + "</details>")
    parts.append(f"</div><script>{JS}</script></body></html>")
    return "".join(parts)


# CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Short list of distinct problems from mutation-testing runs.")
    ap.add_argument("paths", type=Path, nargs="*", help="Folders / history.json files. Default: runs.")
    ap.add_argument("--last-hours", type=float, default=None)
    ap.add_argument("--since", default=None, help='e.g. "2026-09-29 22:52"')
    ap.add_argument("--until", default=None)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--html", type=Path, default=None, help="Output file. Default: tests/output/reports/issues/issues_<timestamp>.html")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)
    typed_args = list(sys.argv[1:] if argv is None else argv)
    if args.html is None: args.html = ISSUES_DIR / f"issues_{datetime.now():%Y%m%d_%H%M%S}.html"

    cfg = load_config(args.config)
    if args.last_hours is not None and (args.since or args.until):
        print("[!] Use either --last-hours or --since/--until.", file=sys.stderr)
        return 2
    try:
        since = parse_time_arg(args.since, cfg) if args.since else None
        until = parse_time_arg(args.until, cfg) if args.until else None
    except ValueError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2
    if args.last_hours is not None:
        since = datetime.now() - timedelta(hours=args.last_hours)

    files: list[Path] = []
    requested = args.paths or [Path(x) for x in cfg["defaults"]["paths"]]
    for p in requested:
        try:
            files.extend(find_log_files(p))
        except FileNotFoundError as e:
            print(f"[!] {e}", file=sys.stderr)
    files = sorted(dict.fromkeys(f.resolve() for f in files))
    if not files:
        print("No history.json files found.", file=sys.stderr)
        return 1
    kept = [f for f in files if not (f.parent / "RUN_CLEAN.json").is_file()]   # clean runs have nothing to report
    clean_files = [f for f in files if f not in kept]
    clean_skipped, files = len(clean_files), kept

    def _in_window(started) -> bool:
        return not ((since and started < since) or (until and started > until))

    # Run folders without a history.json: the run crashed or was stopped before it finished
    unfinished = []
    for root in requested:
        if Path(root).is_dir():
            for d in sorted(Path(root).iterdir()):
                if d.is_dir() and not (d / "history.json").is_file():
                    try:
                        started = datetime.strptime(run_id_of(d.name) or d.name, cfg["time_filter"]["run_id_format"])
                    except ValueError:
                        continue
                    if _in_window(started):
                        unfinished.append(d.name)
    clean_in = sum(1 for f in clean_files if _in_window(run_start_time(f.parent.name, f, cfg)))
    if not files:
        print(f"All {clean_skipped} run(s) are clean, nothing to report.")
        return 0

    runs, eps, outside = [], [], 0
    for p in files:
        try:
            r = load_run(p, cfg)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[!] Skipping {p}: {e}", file=sys.stderr)
            continue
        if (since and r.started < since) or (until and r.started > until):
            outside += 1
            continue
        runs.append(r)
        for e in build_episodes(r, cfg):
            assign_tier(e, cfg)
            eps.append(e)
    if not runs:
        if clean_in:
            print(f"All {clean_in} run(s) in the time window are clean, nothing to report ({outside} older run(s) outside it).")
            return 0
        print(f"No runs inside the time window ({outside} outside).", file=sys.stderr)
        return 1

    issues, harness, tool_eps = build_issues(eps, runs, cfg)
    note = ""
    if since or until:
        note = (f"Runs from {since.strftime('%Y-%m-%d %H:%M') if since else 'the beginning'} to "
                f"{until.strftime('%Y-%m-%d %H:%M') if until else 'now'}.")
    frame = (f"{len(runs) + clean_in + len(unfinished)} runs in this time frame: {len(runs)} analysed, "
             f"{clean_in} clean (left out)" + (f", {len(unfinished)} unfinished" if unfinished else ""))
    started_with = os.environ.get(ORIGIN_ENV) or " ".join(
        ["python -m tests.mutation_testing.reports.issue_report", *map(_quote, typed_args)])
    command_html = _command_html(started_with, _repeat_command(args, bool(args.paths), since, until))
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text(
        render(runs, issues, harness, tool_eps, cfg, note,
               _sources_html(requested, runs, outside, clean_in, unfinished), frame, command_html),
        encoding="utf-8")
    new = sum(1 for i in issues if not i.known)
    print(f"Report: {args.html.resolve()}  ({args.html.stat().st_size // 1024} KB)")
    print(f"{len(runs)} run(s) -> {new} new problem(s), {len(issues) - new} known, "
          f"{len(tool_eps)} crashed check(s), {sum(harness.values())} failed action(s).")
    print(frame + ".")
    if not args.no_open and cfg["report"].get("open_in_browser"):
        webbrowser.open(args.html.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())