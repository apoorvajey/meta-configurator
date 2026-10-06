"""HTML step log of a mutation-loop run or a replay."""

from __future__ import annotations

from html import escape as _esc
import json
import os
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.mutation_testing.loop.step_result import StepResult

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

    # Repair steps have no testid (they are direct undo/redo clicks), so describe them from their kwargs
    if step.action_id.startswith("repair:"):
        failures = step.kwargs.get("persisting_failures", [])
        role = step.kwargs.get("role", "attempt")
        actions = step.action_id[len("repair:"):]
        failures_str = ", ".join(f"<code>{f}</code>" for f in failures) or "?"
        if role == "succeeded":
            return (f'Auto-repair (<code>{actions}</code>): cleared persisting '
                    f'failure(s) {failures_str}')
        if role == "attempt":
            return (f'Auto-repair attempt (<code>{actions}</code>) for {failures_str} '
                    f'— checking whether this cleared it')
        if role in ("mode_switch_back", "mode_switch_forward"):
            direction = "back to the prior mode" if role == "mode_switch_back" else "forward again"
            return f'Switched mode ({direction}) around an auto-repair'
        if role == "clear_text_auto_undo":
            return f'Auto-undo (<code>{actions}</code>) of the preceding clear-text action'
        if role == "invalid_schema_recovery":
            return (f'Recovery (<code>{actions}</code>): undoing toward a valid schema '
                    f'({_esc(step.kwargs.get("recovery_reason", ""))})')
        if role == "invalid_schema_recovery_revert":
            return (f'Recovery did not reach a valid schema - reverted '
                    f'(<code>{actions}</code>) and stopped forcing recovery for this run')
        # remaining role: "revert"
        return (f'Repair attempt did not clear {failures_str} — reverted '
                f'(<code>{actions}</code>), giving up on further auto-repair for '
                f'this failure unless it resolves and recurs later')

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
        return f'Removed <b>{prop}</b> property'
    if testid.startswith("rename-property-"):
        prop = testid[len("rename-property-"):]
        return f'Renamed <b>{prop}</b> to <code>{value}</code>'
    if testid == "mode-active-false":
        mode = step.report.mode if step.report else "unknown"
        return f'Switched to <b>{mode}</b> mode'
    if testid == "format-selector":
        return 'Changed data format'
    text_desc = _describe_text_view_action(testid, value, step.action_id)
    if text_desc:
        return text_desc
    return f'<code>{step.action_id}</code>'


def _format_mismatch(mismatch: dict) -> str:
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
                <pre>{value_json}</pre>
            </details>
        </div>"""


def _format_failure(failure) -> str:
    mismatches_html = ""
    if "mismatches" in failure.details:
        for m in failure.details["mismatches"]:
            mismatches_html += _format_mismatch(m)
    elif failure.details:
        mismatches_html = f"<pre>{json.dumps(failure.details, indent=2, default=str)}</pre>"

    # Evidence section
    evidence_html = ""
    if hasattr(failure, 'evidence') and failure.evidence:
        items = "".join(
            f'<div class="ev-item">{e}</div>'
            for e in failure.evidence
        )
        evidence_html = f"""
        <details class="evidence-wrap">
            <summary>Evidence ({len(failure.evidence)} checks)</summary>
            <div class="evidence-list">{items}</div>
        </details>"""

    return f"""
        <div class="failure">
            <div class="failure-name">✗ {failure.name}</div>
            <div class="failure-msg">{failure.message}</div>
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

def export_html_log(
    history: list["StepResult"],
    path: str = "mutation_log.html",
    screenshots_dir: str = "mutation_screenshots",
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
    fails  = sum(1 for s in history if s.action_succeeded and s.report and not s.report.passed)
    passes = total - errors - fails

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
        uri = os.path.abspath(source_mutation_log_path).replace("\\", "/")
        source_mutation_log_line = (
            f'<br>Source mutation log: <a href="file:///{uri}">{source_mutation_log_path}</a>')

    steps_html = ""
    for i, s in enumerate(history):
        # Status
        if not s.action_succeeded:
            badge = '<span class="badge badge-error">Error</span>'
            step_class = "step-error"
        elif s.report is None:
            badge = '<span class="badge badge-skip">Skipped</span>'
            step_class = "step-skip"
        elif s.report.passed:
            badge = '<span class="badge badge-pass">Pass</span>'
            step_class = "step-pass"
        else:
            badge = '<span class="badge badge-fail">Fail</span>'
            step_class = "step-fail"

        mode_tag = f'<span class="mode-tag">{s.report.mode}</span>' if s.report else ""
        description = _describe_action(s)
        value_html = _entered_value_html(s)
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
        # Failures
        failures_html = ""
        if s.report and s.report.failures:
            failures_html = '<div class="failures">'
            for f in s.report.failures:
                failures_html += _format_failure(f)
            failures_html += "</div>"

        # State diff: only for failing steps that carry a snapshot
        state_diff_html = ""
        if render_state_diffs and step_class == "step-fail" and s.snapshot is not None:
            prev_snapshot = history[i - 1].snapshot if i > 0 else None
            state_diff_html = _format_state_diff(prev_snapshot, s.snapshot)

        # Error
        error_html = ""
        if s.action_error:
            error_html = f'<div class="action-error"><b>Error:</b> {s.action_error}</div>'

        # Screenshot
        screenshot_html = ""
        if s.screenshot_path:
            abs_path = os.path.abspath(s.screenshot_path)
            if os.path.exists(abs_path):
                uri = abs_path.replace("\\", "/")
                screenshot_html = f'''
                <details class="screenshot-wrap">
                    <summary>Screenshot</summary>
                    <img src="file:///{uri}" class="screenshot"/>
                </details>'''

        # Available actions (collapsible)
        actions_html = ""
        if hasattr(s, 'available_actions') and s.available_actions:
            selected_testid = s.kwargs.get("testid", "")

            # Build category groups
            grouped = {}
            for testid, label, interaction in s.available_actions:
                # Derive category from testid
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

/* Steps */
.step {{ background: white; border-radius: 10px; padding: 14px 18px;
         margin-bottom: 12px; border-left: 5px solid #ddd;
         box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
.step-pass  {{ border-left-color: #4caf50; }}
.step-fail  {{ border-left-color: #f44336; }}
.step-error {{ border-left-color: #ff9800; }}
.step-skip  {{ border-left-color: #bdbdbd; }}

.step-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
.step-num {{ font-weight: 700; color: #555; font-size: 0.9em; min-width: 56px; }}

/* Badges */
.badge {{ padding: 2px 10px; border-radius: 10px; font-size: 0.78em; font-weight: 600; }}
.badge-pass  {{ background: #e8f5e9; color: #2e7d32; }}
.badge-fail  {{ background: #fce4ec; color: #c62828; }}
.badge-error {{ background: #fff3e0; color: #e65100; }}
.badge-skip  {{ background: #f5f5f5; color: #888; }}
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
           color: #444; background: #fafafa; }}
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
.nav-btn-fail:disabled  {{ opacity: 0.4; cursor: default; }}
.nav-btn-error:disabled {{ opacity: 0.4; cursor: default; }}
.nav-label {{ color: #888; font-size: 0.8em; min-width: 40px; text-align: center; }}
</style>
</head>
<body>

<h1>Mutation Loop Log</h1>
<div class="meta">
    Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    &nbsp;·&nbsp; {total} steps
    {schema_line}
    {source_history_line}
    {source_mutation_log_line}
</div>

<div class="summary">
    <div class="sbox sbox-total">Total: {total}</div>
    <div class="sbox sbox-pass">Pass: {passes}</div>
    <div class="sbox sbox-fail" onclick="scrollToFail()" style="cursor:pointer" title="Click to jump to first failure">Fail: {fails}</div>
    <div class="sbox sbox-error" onclick="scrollToError()" style="cursor:pointer" title="Click to jump to first error">Errors: {errors}</div>
</div>

{steps_html}

<div class="nav-bar" id="navBar">
    <button class="nav-btn nav-btn-fail" onclick="navigate('fail', -1)">◀ Fail</button>
    <span class="nav-label" id="failLabel">0/0</span>
    <button class="nav-btn nav-btn-fail" onclick="navigate('fail', 1)">Fail ▶</button>
    &nbsp;
    <button class="nav-btn nav-btn-error" onclick="navigate('error', -1)">◀ Error</button>
    <span class="nav-label" id="errorLabel">0/0</span>
    <button class="nav-btn nav-btn-error" onclick="navigate('error', 1)">Error ▶</button>
</div>

<script>
const state = {{
    fail:  {{ els: [], idx: -1 }},
    error: {{ els: [], idx: -1 }},
}};

window.onload = function() {{
    state.fail.els  = [...document.querySelectorAll('.step-fail')];
    state.error.els = [...document.querySelectorAll('.step-error')];
    updateLabel('fail');
    updateLabel('error');

    // Hide nav bar if nothing to navigate
    if (!state.fail.els.length && !state.error.els.length) {{
        document.getElementById('navBar').style.display = 'none';
    }}
}};

function navigate(type, dir) {{
    const s = state[type];
    if (!s.els.length) return;
    s.idx = (s.idx + dir + s.els.length) % s.els.length;

    // Remove previous highlight
    document.querySelectorAll('.step-highlight').forEach(e => {{
        e.style.outline = '';
        e.classList.remove('step-highlight');
    }});

    // Highlight and scroll
    const el = s.els[s.idx];
    el.style.outline = type === 'fail' ? '3px solid #f44336' : '3px solid #ff9800';
    el.classList.add('step-highlight');
    el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    updateLabel(type);
}}

function updateLabel(type) {{
    const s = state[type];
    const id = type === 'fail' ? 'failLabel' : 'errorLabel';
    const el = document.getElementById(id);
    if (s.els.length === 0) {{
        el.textContent = '0/0';
    }} else {{
        el.textContent = (s.idx + 1) + '/' + s.els.length;
    }}
}}
</script>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nLog: {os.path.abspath(path)}")
