"""
log_exporter.py
===============
Located at: tests/mutation_testing/log_exporter.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.mutation_testing.mutation_loop import StepResult


def _describe_action(step: "StepResult") -> str:
    testid = step.kwargs.get("testid", "")
    value  = step.kwargs.get("value", "")

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
        return f'Changed data format'
    return f'<code>{step.action_id}</code>'


def _format_mismatch(mismatch: dict) -> str:
    prop   = mismatch.get("property", "?")
    should = mismatch.get("should_show_error", False)
    shows  = mismatch.get("gui_shows_error", False)
    value  = mismatch.get("value", "?")
    expected_str = "error expected" if should else "no error expected"
    actual_str   = "error icon shown" if shows else "no error icon shown"
    value_json   = json.dumps(value, indent=2, default=str)
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
            </div>
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

def export_html_log(
    history: list["StepResult"],
    path: str = "mutation_log.html",
    screenshots_dir: str = "mutation_screenshots",
):
    total  = len(history)
    errors = sum(1 for s in history if not s.action_succeeded)
    fails  = sum(1 for s in history if s.action_succeeded and s.report and not s.report.passed)
    passes = total - errors - fails

    steps_html = ""
    for i, s in enumerate(history):
        # Status
        if not s.action_succeeded:
            badge = '<span class="badge badge-error">⚠ Error</span>'
            step_class = "step-error"
        elif s.report is None:
            badge = '<span class="badge badge-skip">⏭ Skipped</span>'
            step_class = "step-skip"
        elif s.report.passed:
            badge = '<span class="badge badge-pass">✅ Pass</span>'
            step_class = "step-pass"
        else:
            badge = '<span class="badge badge-fail">❌ Fail</span>'
            step_class = "step-fail"

        mode_tag = f'<span class="mode-tag">{s.report.mode}</span>' if s.report else ""
        description = _describe_action(s)
        inv_line = f'<div class="inv-line">{s.report.summary()}</div>' if s.report else ""

        # Evidence for all invariants (passing + failing)
        all_evidence_html = ""
        if s.report:
            for r in s.report.results:
                if hasattr(r, 'evidence') and r.evidence:
                    icon = "✅" if r.passed else "❌"
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

            # Group by category
            from itertools import groupby
            category_labels = {
                "1_expand": "Expand / Collapse",
                "2_edit_field": "Edit Fields",
                "3_dropdown": "Dropdowns",
                "4_add": "Add",
                "5_rename": "Rename",
                "6_remove": "Remove",
                "7_mode": "Mode",
                "8_format": "Format",
                "9_other": "Other",
            }

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
                items_html = ""
                for testid, label, interaction in items:
                    selected = testid == selected_testid
                    highlight = ' class="action-selected"' if selected else ""
                    star = " ★" if selected else ""
                    items_html += f'<div{highlight}>{label}{star}</div>'
                groups_html += f'<div class="action-group"><div class="action-group-title">{cat}</div>{items_html}</div>'

            actions_html = f"""
            <details class="actions-wrap">
                <summary>Available actions ({len(s.available_actions)}) — ★ selected</summary>
                <div class="actions-grid">{groups_html}</div>
            </details>"""


        steps_html += f"""
        <div class="step {step_class}">
            <div class="step-header">
                <span class="step-num">Step {i+1}</span>
                {badge}
                {mode_tag}
            </div>
            <div class="step-desc">{description}</div>
            {inv_line}
            {all_evidence_html}
            {error_html}
            {failures_html}
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
</div>

<div class="summary">
    <div class="sbox sbox-total">📊 Total: {total}</div>
    <div class="sbox sbox-pass">✅ Pass: {passes}</div>
    <div class="sbox sbox-fail" onclick="scrollToFail()" style="cursor:pointer" title="Click to jump to first failure">❌ Fail: {fails}</div>
    <div class="sbox sbox-error" onclick="scrollToError()" style="cursor:pointer" title="Click to jump to first error">⚠ Errors: {errors}</div>
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
    abs_path = os.path.abspath(path)
    print(f"\n📄 Log → {abs_path}")
    # print(f"   Open: file:///{abs_path.replace(chr(92), '/')}")
    import subprocess
    try:
        subprocess.run(['clip'], input=abs_path.encode(), check=True)
        print(f"   (path copied to clipboard — Ctrl+V in browser address bar)")
    except Exception:
        pass