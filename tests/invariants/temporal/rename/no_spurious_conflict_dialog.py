"""TEMP-RENAME-05."""
from __future__ import annotations

import re

from tests.invariants.invariants_core import InvariantResult

ID = "TEMP-RENAME-05"
NAME = "_inv_temp_rename05_no_spurious_conflict_dialog"

_NAME_IN_DIALOG = re.compile(r'field named "(.*?)" at this location', re.S)


def _all_keys(node) -> set[str]:
    """Every object key anywhere in a JSON value."""
    keys: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            keys.update(str(k) for k in cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return keys


def check(ctx) -> InvariantResult | None:
    """The app's "Rename conflict" dialog ("The data already contains a field named X at this location") may only
    open if the name that was committed already existed. A name that did not exist before the action cannot
    conflict, so the dialog is a bug (the name commit runs twice, on Enter and again on blur, and the second run
    finds the key the first one created).

    The dialog texts come from the page (snapshot field rename_conflict_dialogs), because the executor dismisses the
    dialog straight away. The previous state is searched for the name at any depth of the edited document (the user
    schema in the Schema Editor, the data in the Data Editor), which can only make the rule more lenient.
    """
    if ctx.prev_step == 0 and not ctx.prev:
        return None
    dialogs = ctx.curr.get("rename_conflict_dialogs")
    if dialogs is None:
        return None   # not recorded for this step: unknown, not "none"
    is_add_or_rename = ctx.action.startswith(("rename-", "add-property-"))
    if not dialogs and not is_add_or_rename:
        return None

    name = "TEMP-RENAME-05 no rename-conflict dialog for a new name"
    if not dialogs:
        return InvariantResult(name, True, "no rename-conflict dialog ✓", {},
                               evidence=[f"action={ctx.action}", "dialogs=[]"])

    edited = ctx.prev.get("se_data") if ctx.curr.get("active_mode") == "schemaEditor" else ctx.prev.get("de_data")
    existing = _all_keys(edited)
    spurious = []
    for text in dialogs:
        m = _NAME_IN_DIALOG.search(text)
        new_name = m.group(1) if m else None
        if new_name is None or new_name not in existing:
            spurious.append(new_name if new_name is not None else text[:80])

    passed = not spurious
    return InvariantResult(
        name, passed,
        "dialog only for a name that already existed ✓" if passed
        else f"'Rename conflict' dialog for a name that did not exist before: {spurious}",
        {} if passed else {"spurious_names": spurious, "dialogs": dialogs},
        evidence=[
            f"action={ctx.action}",
            f"dialogs={len(dialogs)} spurious={spurious}",
            f"→ {'name already existed ✓' if passed else 'no key with that name before the action ✗'}",
        ],
    )