"""HTML step log of a mutation-loop run or a replay."""

from __future__ import annotations

from html import escape as _esc
import json
import os
from datetime import datetime

from tests.mutation_testing.common.report_theme import RUN_LOG_DARK
from tests.mutation_testing.runs.known_bugs import KnownBugMatcher, bug_label_for, failure_details, invariant_id
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.mutation_testing.runs.step_result import StepResult

# Optional: field-level diff for _format_state_diff(); without it the full before/after JSON is shown
try:
    from deepdiff import DeepDiff
except ImportError:
    DeepDiff = None

# Max actions listed per category in a step's "Available actions" (the selected one is always shown)
_MAX_ACTIONS_PER_CATEGORY = 20

_TEXT_VIEW_PREFIXES = (
    "edit-text-", "add-child-text-", "change-type-text-",
    "remove-child-text-", "rename-child-text-",
    "add-array-item-text-", "remove-array-item-text-", "clear-text-",
)

# Snapshot facets diffed on a failing step; de_gui/se_gui (raw DOM facts) are left out as noisy
_SNAPSHOT_FACETS = ("de_data", "de_schema", "se_schema", "se_data", "de_text", "se_text")


def _format_text_path(segments: list[str], mode: str) -> tuple[str, str]:
    """Returns (leaf, parents_breadcrumb) for a Text View path.
    In the Schema Editor, structural keywords are hidden from the breadcrumb:
    'properties' is dropped and 'additionalProperties' becomes '(any key)'.
    The leaf is always kept as-is."""
    if not segments:
        return "document root", ""
    leaf, parents = segments[-1], segments[:-1]
    if mode == "schemaEditor":
        parents = [("(any key)" if p == "additionalProperties" else p)
                   for p in parents if p != "properties"]
    return leaf, " › ".join(parents)


def _describe_text_view_action(testid: str, value: str, action_id: str) -> str | None:
    """Plain-English description for Text View testids of the form
    '{prefix}{mode}:{path}' (add-child-text has a 3rd ':{prop_name}' part).
    Returns None if the testid is not a Text View action."""
    prefix = next((p for p in _TEXT_VIEW_PREFIXES if testid.startswith(p)), None)
    if prefix is None:
        return None

    mode, _, rest = testid[len(prefix):].partition(":")
    path_str, _, extra = rest.partition(":")
    mode_label = {"schemaEditor": "Schema Editor",
                  "dataEditor": "Data Editor"}.get(mode, mode or "?")
    segments = [s for s in path_str.split(".") if s]
    blank_key = prefix in ("remove-child-text-", "rename-child-text-") and (not path_str or path_str.endswith("."))
    if blank_key:
        # An empty path segment is a key named "": '' is the blank key at the top level, 'a.' is the blank key inside 'a'.
        _, parents = _format_text_path(segments + [""], mode)
        leaf = "a field with an empty name"
    else:
        leaf, parents = _format_text_path(segments, mode)
    where = f' <i>(in {parents})</i>' if parents else ''
    val = f' <code>{value}</code>' if value else ''

    if prefix == "edit-text-":
        text = f'Edited <b>{leaf}</b>{where}' + (f' to{val}' if value else '')
    elif prefix == "add-child-text-":
        text = f'Added <b>{extra or "a new property"}</b> to <b>{leaf}</b>{where}'
    elif prefix == "change-type-text-":
        text = f'Changed the type of <b>{leaf}</b>{where}' + (f' to{val}' if value else '')
    elif prefix == "remove-child-text-":
        text = f'Removed <b>{leaf}</b>{where}'
    elif prefix == "rename-child-text-":
        text = f'Renamed <b>{leaf}</b>{where}' + (f' to{val}' if value else '')
    elif prefix == "add-array-item-text-":
        text = f'Added an item to array <b>{leaf}</b>{where}'
    elif prefix == "remove-array-item-text-":
        text = f'Removed array item <b>{leaf}</b>{where}'
    else:  # clear-text-
        return (f'Cleared the entire {mode_label} text '
                f'<br><small><code>{action_id}</code></small>')

    # Plain English first, raw id underneath
    return (f'{text} in the {mode_label} text view'
            f'<br><small><code>{action_id}</code></small>')


def _describe_action(step: "StepResult") -> str:
    testid = step.kwargs.get("testid", "")
    value  = _esc(step.kwargs.get("value", ""))
    # Text View actions pick their new value inside the executor, so kwargs["value"] is empty;
    # the mutation loop records what actually changed as "observed_value".
    if not value and step.kwargs.get("observed_value"):
        value = _esc(step.kwargs["observed_value"])

    if testid.startswith("input-string-"):
        prop = testid[len("input-string-"):]
        return f'Typed <code>{value}</code> into <b>{prop}</b> string field'
    if testid.startswith("input-select-"):
        prop = testid[len("input-select-"):]
        return f'Edited <b>{prop}</b> field'
    if testid.startswith("add-item-"):
        prop = testid[len("add-item-"):]
        return f'Added new item to <b>{prop}</b> array'
    if testid.startswith("add-property-"):
        suffix = testid[len("add-property-"):]
        target = f'<b>{suffix}</b>' if suffix else 'current view root'
        return f'Added new property to {target}'
    if testid.startswith("expand-"):
        prop = testid[len("expand-"):]
        return f'Expanded <b>{prop}</b> node'
    if testid.startswith("collapse-"):
        prop = testid[len("collapse-"):]
        return f'Collapsed <b>{prop}</b> node'
    if testid.startswith("remove-property-"):
        prop = testid[len("remove-property-"):]
        if not prop or prop.endswith("."):
            return f'Removed a property with an empty name' + (f' <i>(in {prop[:-1]})</i>' if prop else '')
        return f'Removed <b>{prop}</b> property'
    if testid.startswith("rename-property-"):
        prop = testid[len("rename-property-"):]
        return f'Renamed <b>{prop}</b> to <code>{value}</code>'
    if testid == "mode-active-false":
        mode = step.report.mode if step.report else "unknown"
        return f'Switched to <b>{mode}</b> mode'
    if testid == "format-selector":
        return 'Changed data format'
    if testid in ("undo", "redo", "schema_undo", "schema_redo"):
        verb = "Undo" if testid.endswith("undo") else "Redo"
        mode = step.report.mode if step.report else ""
        panel = "Schema" if testid.startswith("schema_") or mode == "schemaEditor" else "Data"
        return f'Clicked <b>{verb}</b> ({panel} panel)<br><small><code>{step.action_id}</code></small>'
    text_desc = _describe_text_view_action(testid, value, step.action_id)
    if text_desc:
        return text_desc
    return f'<code>{step.action_id}</code>'


def _format_star_mismatch(mismatch: dict) -> str:
    """INV-D3: a row exists, but its required star disagrees with schema.required."""
    prop     = _esc(str(mismatch.get("property", "?")))
    required = bool(mismatch.get("schema_says_required"))
    shows    = bool(mismatch.get("gui_shows_star"))
    expected_str = ("required star shown (property is in schema.required)" if required
                    else "no star (property is not in schema.required)")
    actual_str   = "star shown" if shows else "no star shown"
    return f"""
        <div class="mismatch">
            <div class="mismatch-prop">Property: <code>{prop}</code></div>
            <div class="mismatch-row">
                <span class="label">Expected:</span>
                <span class="badge-pass">{expected_str}</span>
            </div>
            <div class="mismatch-row">
                <span class="label">Actual:</span>
                <span class="badge-fail">{actual_str}</span>
            </div>
        </div>"""


def _format_mismatch(mismatch: dict) -> str:
    if "gui_shows_star" in mismatch:
        return _format_star_mismatch(mismatch)
    prop   = mismatch.get("property", "?")
    should = mismatch.get("should_show_error", False)
    shows  = mismatch.get("gui_shows_error", False)
    value  = mismatch.get("value", "?")
    expected_str = "error expected" if should else "no error expected"
    actual_str   = "error icon shown" if shows else "no error icon shown"
    value_json   = json.dumps(value, indent=2, default=str)

    # Why the schema says the value is invalid (INV-D6 "reason" list of
    # {"path", "message"} dicts - every simultaneous violation).
    reason_html = ""
    violations = mismatch.get("reason") or []
    if isinstance(violations, list) and violations:
        lines = ""
        for v in violations:
            v_path = _esc(str(v.get("path", "?")))
            v_msg  = _esc(str(v.get("message", "")))
            lines += f'<div>at <code>{v_path}</code>: {v_msg}</div>'
        reason_html = f'''
            <div class="mismatch-row">
                <span class="label">Reason:</span>
                <div>{lines}</div>
            </div>'''

    # What the GUI's own validation tooltip said, if an icon was shown.
    gui_html = ""
    gui_msgs = mismatch.get("gui_messages") or []
    if isinstance(gui_msgs, list) and gui_msgs:
        lines = "".join(f"<div>{_esc(str(m))}</div>" for m in gui_msgs)
        gui_html = f'''
            <div class="mismatch-row">
                <span class="label">GUI said:</span>
                <div>{lines}</div>
            </div>'''

    return f"""
        <div class="mismatch">
            <div class="mismatch-prop">Property: <code>{prop}</code></div>
            <div class="mismatch-row">
                <span class="label">Expected:</span>
                <span class="{'badge-fail' if should else 'badge-pass'}">{expected_str}</span>
            </div>
            <div class="mismatch-row">
                <span class="label">Actual:</span>
                <span class="{'badge-fail' if shows else 'badge-pass'}">{actual_str}</span>
            </div>{reason_html}{gui_html}
            <details>
                <summary>Current value</summary>
                <pre>{_esc(value_json)}</pre>
            </details>
        </div>"""


def _leaf_diffs(a, b, path: str = "") -> list[tuple[str, object, object]]:
    """Every differing field between two values. Recurses into dicts and equal-length lists; a type change
    (1 vs True) counts as a difference."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            key = str(k) or '""'   # an empty key is shown as "", not mistaken for the root
            p = f"{path}.{key}" if path else key
            if k not in a:
                out.append((p, "<missing>", b[k]))
            elif k not in b:
                out.append((p, a[k], "<missing>"))
            else:
                out.extend(_leaf_diffs(a[k], b[k], p))
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(_leaf_diffs(x, y, f"{path}[{i}]"))
    elif a != b or type(a) is not type(b):
        out.append((path or "(root)", a, b))
    return out


def _short_value(v, limit: int = 120) -> str:
    s = v if v == "<missing>" else json.dumps(v, default=str, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def _format_compare_diff(details: dict) -> str:
    """Only the differing fields for INV-D1 / INV-S1 style failures. Returns "" for any other details shape."""
    left_key = next((k for k in ("internal_state", "internal_schema") if k in details), None)
    if not left_key or "text_view" not in details:
        return ""
    diffs = _leaf_diffs(details[left_key], details["text_view"])
    if not diffs:
        return ""
    left_label = "internal data" if left_key == "internal_state" else "internal schema"
    rows = "".join(
        f"<tr><td><code>{_esc(p)}</code></td><td><code>{_esc(_short_value(a))}</code></td>"
        f"<td><code>{_esc(_short_value(b))}</code></td></tr>"
        for p, a, b in diffs
    )
    other = {k: v for k, v in details.items() if k not in (left_key, "text_view")}
    other_html = f"<pre>{_esc(json.dumps(other, indent=2, default=str))}</pre>" if other else ""
    return (f"<div>{len(diffs)} field(s) differ</div>"
            f"<table class='diff-table'><tr><th>field</th><th>{left_label}</th><th>Text View</th></tr>{rows}</table>"
            f"{other_html}")


def _violation_rows_html(violations: list) -> str:
    """Violations of an inferred rule that carry a field-level diff ({"changes": [{"path","before","after"}]})
    as one table per violation; "" if the details have another shape."""
    blocks = []
    for n, v in enumerate(violations, 1):
        changes = v.get("changes") if isinstance(v, dict) else None
        if not (isinstance(changes, list) and changes and all(isinstance(c, dict) and "path" in c for c in changes)):
            return ""
        total = v.get("changed_fields", len(changes))
        rows = "".join(f"<tr><td><code>{_esc(c['path'])}</code></td><td><code>{_esc(c.get('before'))}</code></td>"
                       f"<td><code>{_esc(c.get('after'))}</code></td></tr>" for c in changes)
        more = f"<div class='muted'>… and {total - len(changes)} more field(s)</div>" if total > len(changes) else ""
        blocks.append(f"<div>Observation {n}: {total} field(s) differ</div>"
                      f"<table class='diff-table'><tr><th>field</th><th>before</th><th>after</th></tr>{rows}</table>{more}")
    return "".join(blocks)


def _format_violations(details: dict) -> str:
    """Inferred-rule failure details: the rule's assertion plus the exact differing fields."""
    violations = details.get("violations")
    if not isinstance(violations, list) or not violations:
        return ""
    tables = _violation_rows_html(violations)
    if not tables:
        return ""
    asserts = f"<div>Asserts: {_esc(details['asserts'])}</div>" if details.get("asserts") else ""
    where = (f"<div class='muted'>Where: {_esc(details['definitions'])}</div>"
             if details.get("definitions") and details["definitions"] != "-" else "")
    return asserts + where + tables


def _format_failure(failure, known_bug: str | None = None) -> str:
    mismatches_html = ""
    if "mismatches" in failure.details:
        for m in failure.details["mismatches"]:
            mismatches_html += _format_mismatch(m)
    elif failure.details:
        mismatches_html = _format_compare_diff(failure.details) or _format_violations(failure.details) or (
            f"<pre>{_esc(json.dumps(failure.details, indent=2, default=str))}</pre>")

    evidence_html = ""
    if hasattr(failure, 'evidence') and failure.evidence:
        items = "".join(
            f'<div class="ev-item">{_esc(str(e))}</div>'
            for e in failure.evidence
        )
        evidence_html = f"""
        <details class="evidence-wrap">
            <summary>Evidence ({len(failure.evidence)} checks)</summary>
            <div class="evidence-list">{items}</div>
        </details>"""

    return f"""
        <div class="failure">
            <div class="failure-name">✗ {_esc(failure.name)}{f' <span class="known-tag">known bug {_esc(bug_label_for(known_bug))}</span>' if known_bug else ""}</div>
            <div class="failure-msg">{_esc(failure.message)}</div>
            {mismatches_html}
            {evidence_html}
        </div>"""


def _format_state_diff(before: dict | None, after: dict | None) -> str:
    """Exact before/after diff of the tracked facets of a failing step, or "" if `after` is None or nothing changed."""
    if after is None:
        return ""

    changed_facets = [f for f in _SNAPSHOT_FACETS if (before or {}).get(f) != after.get(f)]
    if not changed_facets:
        return ""

    facet_html = ""
    for facet in changed_facets:
        b, a = (before or {}).get(facet), after.get(facet)
        if DeepDiff is not None:
            delta = DeepDiff(b, a, ignore_order=True, verbose_level=1)
            body = _esc(delta.pretty()) if delta else "(values differ, but DeepDiff found no field-level detail)"
        else:
            body = _esc(
                "DeepDiff is not installed (pip install deepdiff) -- showing full before/after instead:\n\n"
                f"Before:\n{json.dumps(b, indent=2, default=str)}\n\n"
                f"After:\n{json.dumps(a, indent=2, default=str)}"
            )
        facet_html += f"""
        <details class="evidence-wrap" open>
            <summary><code>{_esc(facet)}</code> changed</summary>
            <pre>{body}</pre>
        </details>"""

    before_note = "" if before is not None else (
        '<div class="action-error">No "before" state available for this step '
        '(it is the first replayed step, or the previous action failed).</div>')

    return f"""
        <div class="state-diff">
            <div class="state-diff-title">Exact state diff ({len(changed_facets)} facet(s) changed)</div>
            {before_note}
            {facet_html}
        </div>"""


def _entered_value_html(step: "StepResult") -> str:
    """The exact value passed to the action (escaped), plus the created property name for add-property steps."""
    parts = []
    rep = step.kwargs.get("value_repr")
    if rep is None:  # no value_repr recorded
        raw = step.kwargs.get("value", "")
        rep = repr(raw) if raw != "" else None
    if rep is not None:
        parts.append(f'Value entered: <code>{_esc(rep)}</code>')
    created = step.kwargs.get("created_name")
    if created:
        parts.append(f'Property name created: <code>{_esc(repr(created))}</code>')
    if not parts:
        return ""
    return '<div class="entered-value">' + "<br>".join(parts) + "</div>"

def _effect_items_html(effects: list) -> str:
    """One line per change: [data|schema] path: before -> after (exact values). Entries recorded before the document
    tag existed have no tag."""
    return "".join(
        f'<div class="ev-item">'
        + (f'<b>[{_esc(str(e["document"]))}]</b> ' if e.get("document") else "")
        + f'<code>{_esc(str(e.get("path", "")))}</code>: '
        f'{_esc(str(e.get("before", "")))} → {_esc(str(e.get("after", "")))}</div>'
        for e in effects
    )


def _effect_html(step: "StepResult") -> str:
    """Collapsible list of what the step changed in the data and the schema (path: before -> after, exact values)."""
    effects = step.kwargs.get("observed_effect") or []
    if not effects:
        return ""
    items = _effect_items_html(effects)
    return (f'<details class="evidence-wrap"><summary>Document changes</summary>'
            f'<div class="evidence-list">{items}</div></details>')


def _repairs_html(step: "StepResult") -> str:
    """The repairs this step triggered (kwargs["repairs"], see repair_engine.repair_block): what was clicked and how it
    ended, one collapsible block. A replay's "not_needed" repair has no clicks."""
    blocks = step.kwargs.get("repairs") or []
    if not blocks:
        return ""
    titles = {"failure": "Auto-repair", "clear_text": "Auto-undo of clear-text", "invalid_schema": "Invalid-schema recovery"}
    out = ""
    for b in blocks:
        title = titles.get(b.get("kind"), "Repair")
        summary = f'{title}: {_esc(str(b.get("outcome", "")))}'
        if b.get("undos") or b.get("redos"):
            summary += f' ({b.get("undos", 0)} undo, {b.get("redos", 0)} redo)'
        rows = ""
        if b.get("persisting_failures"):
            rows += ('<div class="ev-item">Persisting failure(s): '
                     + ", ".join(f"<code>{_esc(str(f))}</code>" for f in b["persisting_failures"]) + "</div>")
        if b.get("reason"):
            rows += f'<div class="ev-item">Reason: {_esc(str(b["reason"]))}</div>'
        for c in b.get("clicks", []):
            state = "passed" if c.get("passed") else "still failing: " + ", ".join(
                _esc(str(f).partition(" ")[0]) for f in c.get("failed_invariants", []))
            rows += f'<div class="ev-item"><code>{_esc(str(c.get("testid", "")))}</code> → {state}</div>'
            rows += _effect_items_html(c.get("effect") or [])
        if not b.get("clicks"):
            rows += '<div class="ev-item">No click was needed.</div>'
        out += (f'<details class="evidence-wrap"><summary>{summary}</summary>'
                f'<div class="evidence-list">{rows}</div></details>')
    return out


def _link_to(target: str, log_path: str) -> str:
    """URL of `target` as seen from the log file: relative, so the links keep working when the run folder is renamed
    or moved. Falls back to a file:// URL when no relative path exists (another drive on Windows)."""
    from urllib.parse import quote
    target_abs, log_dir = os.path.abspath(target), os.path.dirname(os.path.abspath(log_path))
    try:
        return quote(os.path.relpath(target_abs, log_dir).replace("\\", "/"))
    except ValueError:
        return "file:///" + target_abs.replace("\\", "/")


def _run_folder_ref(folder: str) -> str:
    """'tests/output/runs/<run_id>' for a run folder (a __<tag> suffix is dropped); the folder itself if its name is not a run id."""
    from tests.mutation_testing.runs.run_layout import RUNS_DIR, run_id_of
    name = os.path.basename(os.path.normpath(folder))
    rid = run_id_of(name)
    return f"{RUNS_DIR.as_posix()}/{rid}" if rid else folder.replace("\\", "/")


def export_html_log(
    history: list["StepResult"],
    path: str = "mutation_log.html",
    source_history_path: str | None = None,
    source_mutation_log_path: str | None = None,
    schema_option_name: str | None = None,
    data_file: str | None = None,
    render_state_diffs: bool = False,
):
    """Write the step log to `path`.

    render_state_diffs: show a before/after state diff on failing steps. Off by default; the replay turns it on.
    """
    total  = len(history)
    errors = sum(1 for s in history if not s.action_succeeded)

    # Known bugs (runs/known_bugs.py): their failures stay in the log, labelled; a step that fails only on known
    # bugs is counted apart from the failures. A replay hands over the result per step (StepResult.known_bugs).
    matcher = KnownBugMatcher()
    known_by_step: list[dict[str, str]] = []
    known_only: list[bool] = []
    for s in history:
        failing_now = s.action_succeeded and s.report is not None and not s.report.passed
        if s.known_bugs is not None:
            known = s.known_bugs
            other = {invariant_id(r.name) for r in s.report.failures} - set(known) if failing_now else set()
        elif s.action_succeeded and s.report is not None:
            checked = [r.name for r in s.report.results if not getattr(r, "skipped", False)]
            known, other = matcher.observe(s.action_id, [r.name for r in s.report.failures], checked,
                                         failure_details(s.report.failures))
        else:
            known, other = {}, set()
        known_by_step.append(known)
        known_only.append(bool(failing_now and known and not other))
    known_bug_steps = sum(known_only)
    fails  = sum(1 for s in history if s.action_succeeded and s.report and not s.report.passed) - known_bug_steps
    passes = total - errors - fails - known_bug_steps

    schema_line = ""
    schema_parts = []
    if schema_option_name:
        schema_parts.append(f'Schema: <b>{_esc(schema_option_name)}</b>')
    if data_file:
        schema_parts.append(f'Data file: <code>{_esc(data_file)}</code>')
    if schema_parts:
        schema_line = '<br>' + ' &nbsp;·&nbsp; '.join(schema_parts)

    source_history_line = ""
    if source_history_path:
        source_history_line = f'<br>Replayed from: <code>{source_history_path}</code>'

    source_mutation_log_line = ""
    if source_mutation_log_path:
        source_mutation_log_line = (
            f'<br>Source mutation log: <a href="{_link_to(source_mutation_log_path, path)}">'
            f'{source_mutation_log_path}</a>')

    # Command that replays this run again; it names the run folder by its run id (tests/output/runs/<run_id>),
    # which replay_runner uses to find the folder. A replay log points back to the original run.
    if source_history_path:
        replay_history_file = _run_folder_ref(os.path.dirname(source_history_path))
    else:
        replay_history_file = _run_folder_ref(os.path.dirname(os.path.abspath(path)))
    replay_cmd = f"python -m tests.cli replay --history {replay_history_file}"
    replay_cmd_html = (f'<div class="replay-cmd"><b>Replay this run again</b><code>{_esc(replay_cmd)}</code>'
                       f'<button type="button" data-text="{_esc(replay_cmd, quote=True)}">Copy replay command</button></div>')

    # Summary boxes and the navigation bar. A box is clickable (jumps to the first such step) only when its count is
    # not 0; the bar has a group for known bugs, failures and errors, each only when its count is not 0, and no bar at
    # all when there is nothing to navigate to. The JS below finds the steps by their step-<kind> class.
    def _box(css: str, label: str, count: int, kind: str, what: str) -> str:
        if not count:
            return f'<div class="sbox {css}">{label}: {count}</div>'
        return (f'<div class="sbox {css} sbox-click" role="button" tabindex="0" onclick="goFirst(\'{kind}\')" '
                f'onkeydown="if(event.key===\'Enter\')goFirst(\'{kind}\')" title="Click to jump to the first {what}">'
                f'{label}: {count}</div>')

    summary_boxes_html = "\n    ".join(
        [f'<div class="sbox sbox-total">Total: {total}</div>',
         _box("sbox-pass", "Pass", passes, "pass", "passing step")]
        + ([_box("sbox-known", "Known bugs", known_bug_steps, "known", "known-bug step")] if known_bug_steps else [])
        + [_box("sbox-fail", "Fail", fails, "fail", "failure"),
           _box("sbox-error", "Errors", errors, "error", "error")])

    nav_groups = [
        (f'<span class="nav-group" id="{kind}Group">'
         f'<button class="nav-btn nav-prev nav-hidden nav-btn-{kind}" aria-label="Previous {label.lower()}" onclick="navigate(\'{kind}\', -1)">&#9664;</button>'
         f'<span class="nav-title nav-title-{kind}">{label}</span>'
         f'<span class="nav-label"><input class="nav-input" id="{kind}Input" type="text" inputmode="numeric" autocomplete="off" '
         f'aria-label="{label} number" value="0" size="2">/{count}</span>'
         f'<button class="nav-btn nav-next nav-btn-{kind}" aria-label="Next {label.lower()}" onclick="navigate(\'{kind}\', 1)">&#9654;</button>'
         f'</span>')
        for kind, label, count in (("known", "Known bug", known_bug_steps), ("fail", "Fail", fails),
                                   ("error", "Error", errors)) if count]
    nav_inner = "".join(nav_groups)
    nav_html = f'<div class="nav-bar" id="navBar">{nav_inner}</div>' if nav_groups else ""

    steps_html = ""
    for i, s in enumerate(history):
        if not s.action_succeeded:
            badge = '<span class="badge badge-error">Error</span>'
            step_class = "step-error"
        elif s.report is None:
            badge = '<span class="badge badge-skip">Skipped</span>'
            step_class = "step-skip"
        elif s.report.passed:
            badge = '<span class="badge badge-pass">Pass</span>'
            step_class = "step-pass"
        elif known_only[i]:
            badge = '<span class="badge badge-known">Known bug</span>'
            step_class = "step-known"
        else:
            badge = '<span class="badge badge-fail">Fail</span>'
            step_class = "step-fail"

        mode_tag = f'<span class="mode-tag">{s.report.mode}</span>' if s.report else ""
        description = _describe_action(s)
        value_html = _entered_value_html(s)
        effect_html = _effect_html(s) + _repairs_html(s)
        inv_line = f'<div class="inv-line">{s.report.summary()}</div>' if s.report else ""

        # Evidence for all invariants (passing + failing)
        all_evidence_html = ""
        if s.report:
            for r in s.report.results:
                if hasattr(r, 'evidence') and r.evidence:
                    icon = "✓" if r.passed else "✗"
                    items = "".join(f'<div class="ev-item">{e}</div>' for e in r.evidence)
                    all_evidence_html += f"""
                    <div class="inv-evidence">
                        <div class="inv-evidence-name">{icon} {r.name}</div>
                        <div class="evidence-list">{items}</div>
                    </div>"""
            if all_evidence_html:
                all_evidence_html = f"""
                <details class="evidence-wrap">
                    <summary>Invariant evidence</summary>
                    {all_evidence_html}
                </details>"""
        failures_html = ""
        if s.report and s.report.failures:
            failures_html = '<div class="failures">'
            for f in s.report.failures:
                failures_html += _format_failure(f, known_by_step[i].get(invariant_id(f.name)))
            failures_html += "</div>"

        # State diff: only for failing steps that carry a snapshot
        state_diff_html = ""
        if render_state_diffs and step_class == "step-fail" and s.snapshot is not None:
            prev_snapshot = history[i - 1].snapshot if i > 0 else None
            state_diff_html = _format_state_diff(prev_snapshot, s.snapshot)

        error_html = ""
        if s.action_error:
            error_html = f'<div class="action-error"><b>Error:</b> {s.action_error}</div>'

        screenshot_html = ""
        if s.screenshot_path:
            abs_path = os.path.abspath(s.screenshot_path)
            if os.path.exists(abs_path):
                screenshot_html = f'''
                <details class="screenshot-wrap">
                    <summary>Screenshot</summary>
                    <img src="{_link_to(abs_path, path)}" class="screenshot"/>
                </details>'''

        prev_screenshot_html = ""
        if getattr(s, "prev_screenshot_path", None):
            prev_abs = os.path.abspath(s.prev_screenshot_path)
            if os.path.exists(prev_abs):
                prev_screenshot_html = f'''
                <details class="screenshot-wrap">
                    <summary>Screenshot before this step</summary>
                    <img src="{_link_to(prev_abs, path)}" class="screenshot"/>
                </details>'''
            screenshot_html = prev_screenshot_html + screenshot_html

        actions_html = ""
        if hasattr(s, 'available_actions') and s.available_actions:
            selected_testid = s.kwargs.get("testid", "")

            grouped = {}
            for testid, label, interaction in s.available_actions:
                if testid.startswith("expand-") or testid.startswith("collapse-"):
                    cat = "Expand / Collapse"
                elif testid.startswith("input-string-") or testid.startswith("input-number-") or testid.startswith(
                        "input-boolean-"):
                    cat = "Edit Fields"
                elif testid.startswith("input-select-"):
                    cat = "Dropdowns"
                elif testid.startswith("add-"):
                    cat = "Add"
                elif testid.startswith("rename-"):
                    cat = "Rename"
                elif testid.startswith("remove-"):
                    cat = "Remove"
                elif testid == "mode-active-false":
                    cat = "Mode"
                elif testid == "format-selector":
                    cat = "Format"
                else:
                    cat = "Other"
                grouped.setdefault(cat, []).append((testid, label, interaction))

            groups_html = ""
            for cat, items in grouped.items():
                # Cap the list: a Schema-tab step can discover thousands of add-child-text actions, which
                # made the log huge. The selected action is always kept; the full list is in history.json.
                shown = items[:_MAX_ACTIONS_PER_CATEGORY]
                if selected_testid and not any(t == selected_testid for t, _, _ in shown):
                    selected_entry = next((it for it in items if it[0] == selected_testid), None)
                    if selected_entry:
                        shown = shown + [selected_entry]
                items_html = ""
                for testid, label, interaction in shown:
                    selected = testid == selected_testid
                    highlight = ' class="action-selected"' if selected else ""
                    star = " ★" if selected else ""
                    items_html += f'<div{highlight}>{label}{star}</div>'
                remaining = len(items) - len(shown)
                if remaining > 0:
                    items_html += f'<div class="action-more">+{remaining} more (not shown)</div>'
                groups_html += (f'<div class="action-group"><div class="action-group-title">'
                                 f'{cat} ({len(items)})</div>{items_html}</div>')

            actions_html = f"""
            <details class="actions-wrap">
                <summary>Available actions ({len(s.available_actions)}) — ★ selected</summary>
                <div class="actions-grid">{groups_html}</div>
            </details>"""


        steps_html += f"""
        <div class="step {step_class}" id="step-{i+1}">
            <div class="step-header">
                <span class="step-num">Step {i+1}</span>
                {badge}
                {mode_tag}
            </div>
            <div class="step-desc">{description}</div>
            {value_html}
            {effect_html}
            {inv_line}
            {all_evidence_html}
            {error_html}
            {failures_html}
            {state_diff_html}
            {actions_html}
            {screenshot_html}
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Mutation Loop Log</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f0f2f5; color: #1a1a2e; padding: 24px; }}

h1 {{ font-size: 1.5em; font-weight: 700; margin-bottom: 4px; }}
.meta {{ color: #666; font-size: 0.85em; margin-bottom: 20px; }}

/* Summary bar */
.summary {{ display: flex; gap: 12px; margin-bottom: 28px; flex-wrap: wrap; }}
.sbox {{ padding: 12px 20px; border-radius: 10px; font-weight: 700;
         font-size: 0.95em; border: 1px solid transparent; }}
.sbox-total {{ background: #e8eaf6; color: #3949ab; border-color: #c5cae9; }}
.sbox-pass  {{ background: #e8f5e9; color: #2e7d32; border-color: #c8e6c9; }}
.sbox-fail  {{ background: #fce4ec; color: #c62828; border-color: #f8bbd0; }}
.sbox-error {{ background: #fff3e0; color: #e65100; border-color: #ffe0b2; }}
.sbox-known {{ background: #e3f2fd; color: #1565c0; border-color: #bbdefb; }}

/* Steps */
.step {{ background: white; border-radius: 10px; padding: 14px 18px;
         margin-bottom: 12px; border-left: 5px solid #ddd;
         box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
.step-pass  {{ border-left-color: #4caf50; }}
.step-fail  {{ border-left-color: #f44336; }}
.step-error {{ border-left-color: #ff9800; }}
.step-known {{ border-left-color: #42a5f5; }}
.step-skip  {{ border-left-color: #bdbdbd; }}

.step-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
.step-num {{ font-weight: 700; color: #555; font-size: 0.9em; min-width: 56px; }}

/* Badges */
.badge {{ padding: 2px 10px; border-radius: 10px; font-size: 0.78em; font-weight: 600; }}
.badge-pass  {{ background: #e8f5e9; color: #2e7d32; }}
.badge-fail  {{ background: #fce4ec; color: #c62828; }}
.badge-error {{ background: #fff3e0; color: #e65100; }}
.badge-skip  {{ background: #f5f5f5; color: #888; }}
.badge-known {{ background: #e3f2fd; color: #1565c0; }}
.known-tag {{ background: #e3f2fd; color: #1565c0; border-radius: 8px; padding: 1px 8px; font-size: 0.8em; font-weight: 600; margin-left: 6px; }}
.badge-pass-sm  {{ background: #e8f5e9; color: #2e7d32; padding: 1px 6px;
                   border-radius: 6px; font-size: 0.78em; }}
.badge-fail-sm  {{ background: #fce4ec; color: #c62828; padding: 1px 6px;
                   border-radius: 6px; font-size: 0.78em; }}

.mode-tag {{ padding: 2px 8px; border-radius: 8px; font-size: 0.75em;
             background: #e3f2fd; color: #1565c0; font-weight: 500; }}

.step-desc {{ font-size: 0.93em; margin-bottom: 4px; line-height: 1.5; }}
.inv-line  {{ font-size: 0.82em; color: #666; margin-bottom: 6px; }}
.entered-value {{ font-size: 0.85em; margin-bottom: 6px; }}
.entered-value code {{ white-space: pre-wrap; word-break: break-all; }}

.evidence-wrap {{ margin-top: 6px; }}
.evidence-list {{ margin-top: 4px; }}
.ev-item {{ padding: 2px 8px; border-left: 3px solid #e0e0e0;
           margin-bottom: 2px; font-size: 0.8em; font-family: monospace;
           color: #444; background: #fafafa;
           white-space: pre-wrap; overflow-wrap: anywhere; }}
.inv-evidence {{ margin-bottom: 8px; }}
.inv-evidence-name {{ font-size: 0.82em; font-weight: 600;
                     color: #555; margin-bottom: 3px; }}
/* Failures */
.failures {{ margin-top: 8px; }}
.failure {{ background: #fff8f8; border: 1px solid #ffcdd2;
            border-radius: 6px; padding: 10px 14px; margin-bottom: 6px; }}
.failure-name {{ font-weight: 600; color: #c62828; font-size: 0.88em; margin-bottom: 4px; }}
.failure-msg  {{ font-size: 0.85em; color: #666; margin-bottom: 6px; }}

.mismatch {{ background: white; border: 1px solid #e0e0e0;
             border-radius: 4px; padding: 8px 12px; margin-bottom: 4px; }}
.mismatch-prop {{ font-weight: 600; font-size: 0.85em; margin-bottom: 4px; }}
.mismatch-row  {{ font-size: 0.82em; margin-bottom: 2px; display: flex; gap: 6px; }}
.label {{ color: #888; font-weight: 500; min-width: 70px; }}

/* State diff (replay-captured before/after) */
.state-diff {{ background: #f3f8ff; border: 1px solid #bcd6f7;
               border-radius: 6px; padding: 10px 14px; margin-top: 8px; }}
.state-diff-title {{ font-weight: 600; color: #1565c0; font-size: 0.88em; margin-bottom: 4px; }}

/* Error */
.action-error {{ background: #fff3e0; border: 1px solid #ffe0b2;
                 border-radius: 4px; padding: 8px 12px;
                 font-size: 0.85em; color: #e65100; margin-top: 6px; }}

/* Details/summary */
details {{ margin-top: 8px; }}
summary {{ cursor: pointer; font-size: 0.82em; color: #1565c0;
           user-select: none; padding: 2px 0; }}
summary:hover {{ color: #0d47a1; }}
pre {{ background: #f5f5f5; border-radius: 4px; padding: 8px 12px;
       font-size: 0.78em; overflow-x: auto; margin-top: 6px;
       max-height: 180px; border: 1px solid #e0e0e0; }}

/* Screenshot */
.screenshot {{ max-width: 100%; border: 1px solid #e0e0e0;
               border-radius: 4px; margin-top: 6px; display: block; }}

/* Available actions */
.actions-list {{ margin-top: 6px; font-size: 0.8em; }}
.action-item {{ padding: 3px 6px; border-radius: 4px; margin-bottom: 2px;
                display: flex; gap: 8px; align-items: baseline; }}
.action-selected {{ background: #e3f2fd; font-weight: 600; }}
.action-cat {{ color: #888; min-width: 50px; font-size: 0.9em; }}
.action-testid {{ color: #aaa; margin-left: auto; font-size: 0.85em; }}
.actions-grid {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
.action-group {{ background: #f8f9fa; border-radius: 6px; padding: 8px 10px;
                min-width: 160px; flex: 1; }}
.action-group-title {{ font-size: 0.75em; font-weight: 700; color: #888;
                      text-transform: uppercase; margin-bottom: 4px; }}
.action-group div {{ font-size: 0.8em; color: #444; padding: 1px 0; }}
.action-selected {{ color: #1565c0 !important; font-weight: 600; }}
.action-more {{ color: #999 !important; font-style: italic; }}

code {{ background: #f0f0f0; padding: 1px 5px; border-radius: 3px;
        font-size: 0.88em; font-family: monospace; }}
.nav-bar {{
    position: fixed; bottom: 20px; right: 20px;
    display: flex; gap: 8px; z-index: 1000;
    background: white; padding: 8px 12px;
    border-radius: 10px; box-shadow: 0 2px 12px rgba(0,0,0,0.15);
    align-items: center; font-size: 0.85em;
}}
.nav-btn {{
    padding: 6px 12px; border-radius: 6px; border: none;
    cursor: pointer; font-weight: 600; font-size: 0.85em;
}}
.nav-btn-fail  {{ background: #fce4ec; color: #c62828; }}
.nav-btn-error {{ background: #fff3e0; color: #e65100; }}
.nav-btn-known {{ background: #17263a; color: #7fb2f0; }}
.nav-group {{ display: flex; align-items: center; gap: 8px; }}
.nav-group + .nav-group {{ border-left: 1px solid #2c313b; padding-left: 12px; margin-left: 4px; }}
.nav-btn.nav-hidden {{ visibility: hidden; }}   /* keeps its space, so the bar does not shift */
.nav-label {{ display: inline-flex; align-items: center; gap: 2px; }}
.nav-input {{ width: 3.4ch; text-align: center; font: inherit; color: #e8eaf0; background: #14161b;
             border: 1px solid #2c313b; border-radius: 4px; padding: 2px 0; }}
.nav-input:focus {{ outline: none; border-color: #8aa4ff; }}
.nav-title {{ font-weight: 600; }}
.nav-title-known {{ color: #7fb2f0; }} .nav-title-fail {{ color: #f58a80; }} .nav-title-error {{ color: #f0a85a; }}
.sbox-click {{ cursor: pointer; user-select: none; }}
.sbox-click:hover {{ filter: brightness(0.95); border-color: currentColor; }}
.nav-label {{ color: #888; font-size: 0.8em; min-width: 40px; text-align: center; }}
.replay-cmd {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 0 0 14px; }}
.replay-cmd code {{ background: #262b35; color: #e8eaf0; padding: 3px 8px; border-radius: 6px; font-size: 0.85em; }}
.replay-cmd button {{ font: inherit; font-size: 12px; padding: 2px 8px; border: 1px solid #2c313b; border-radius: 6px; background: #262b35; color: #e8eaf0; cursor: pointer; }}
.replay-cmd button:hover {{ border-color: #8aa4ff; }}
{RUN_LOG_DARK}
</style>
</head>
<body>

<h1>Mutation Loop Log</h1>
{replay_cmd_html}
<div class="meta">
    Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    &nbsp;·&nbsp; {total} steps
    {schema_line}
    {source_history_line}
    {source_mutation_log_line}
</div>

<div class="summary">
    {summary_boxes_html}
</div>

{steps_html}

{nav_html}

<script>
// kind -> the step class it navigates and the outline colour of the step it lands on
const KINDS = {{
    pass:  {{ cls: '.step-pass',  color: '#4caf50' }},
    known: {{ cls: '.step-known', color: '#1565c0' }},
    fail:  {{ cls: '.step-fail',  color: '#f44336' }},
    error: {{ cls: '.step-error', color: '#ff9800' }},
}};
const state = {{}};
for (const k in KINDS) state[k] = {{ els: [], idx: -1 }};

window.onload = function() {{
    for (const k in KINDS) {{
        state[k].els = [...document.querySelectorAll(KINDS[k].cls)];
        updateLabel(k);
        const inp = document.getElementById(k + 'Input');
        if (!inp) continue;   // no bar group when the count is 0
        inp.addEventListener('focus', () => inp.select());
        inp.addEventListener('blur', () => updateLabel(k));   // drop anything typed but not confirmed
        inp.addEventListener('keydown', e => {{
            if (e.key === 'Enter') {{ goTo(k, inp.value); inp.blur(); }}
            else if (e.key === 'Escape') inp.blur();
        }});
    }}
}};

function show(type) {{
    const s = state[type];
    // one kind at a time: the other kinds go back to 0/N
    for (const k in KINDS) if (k !== type && state[k].idx !== -1) {{ state[k].idx = -1; updateLabel(k); }}
    document.querySelectorAll('.step-highlight').forEach(e => {{ e.style.outline = ''; e.classList.remove('step-highlight'); }});
    const el = s.els[s.idx];
    el.style.outline = '3px solid ' + KINDS[type].color;
    el.classList.add('step-highlight');
    el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    updateLabel(type);
}}

// Summary box click: the first step of that kind
function goFirst(type) {{
    if (!state[type].els.length) return;
    state[type].idx = 0;
    show(type);
}}

// Typed number (1..N): that step of the kind; anything else puts the old number back
function goTo(type, value) {{
    const n = parseInt(value, 10);
    if (n >= 1 && n <= state[type].els.length) {{ state[type].idx = n - 1; show(type); }}
    else updateLabel(type);
}}

function navigate(type, dir) {{
    const s = state[type];
    if (!s.els.length) return;
    s.idx = Math.min(Math.max(s.idx + dir, 0), s.els.length - 1);   // no wrap-around: the end arrows are hidden
    show(type);
}}

function updateLabel(type) {{
    const inp = document.getElementById(type + 'Input');
    if (!inp) return;
    const s = state[type];
    inp.value = s.idx + 1;
    const g = document.getElementById(type + 'Group');
    g.querySelector('.nav-prev').classList.toggle('nav-hidden', s.idx <= 0);                  // nothing before the first
    g.querySelector('.nav-next').classList.toggle('nav-hidden', s.idx >= s.els.length - 1);   // nothing after the last
}}
</script>
<script>
document.addEventListener('click', function (e) {{
    var b = e.target.closest('button[data-text]'); if (!b) return;
    var t = b.dataset.text, o = b.textContent;
    var done = function () {{ b.textContent = 'Copied \\u2713'; setTimeout(function () {{ b.textContent = o; }}, 1300); }};
    var fallback = function () {{ var a = document.createElement('textarea'); a.value = t; document.body.appendChild(a); a.select();
        try {{ document.execCommand('copy'); done(); }} catch (err) {{}} a.remove(); }};
    if (navigator.clipboard && window.isSecureContext) {{ navigator.clipboard.writeText(t).then(done, fallback); }} else {{ fallback(); }}
}});
</script>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nLog: {os.path.abspath(path)}")