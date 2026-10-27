"""Plain-language statements and descriptions of rules."""
from __future__ import annotations

from tests.mutation_testing.inference.categories import ACTION_CATEGORY_DESCRIPTIONS
from tests.mutation_testing.inference.facets import FACET_DEFINITIONS, RESTORE_STORE_DEFINITIONS, TEXT_VIEWS
from tests.mutation_testing.inference.guards import GUARD_DEFINITIONS, GUARD_STATEMENTS


def _stability_target(params: dict) -> str:
    if "path" in params:
        return "schema." + ".".join(params["path"])
    return params["facet"]


# view -> (what the store is called, which editor's GUI View)
_VIEW_LABELS = {"data": ("data", "Data Editor"), "schema": ("edited schema", "Schema Editor")}


def rule_statement(template: str, category: str, params: dict, positive: bool) -> str:
    """One-line statement of a rule."""
    prefix = "" if category == "*" else f"after {category}: "
    p = params
    if template == "stability":
        s = f"{_stability_target(p)} is unchanged" if positive else f"{_stability_target(p)} changes"
    elif template == "change_count":
        s = (f"{p['facet']} has exactly {p.get('expected', 'k')} DeepDiff change(s)" if positive
             else f"{p['facet']} does not have exactly {p.get('expected', 'k')} DeepDiff change(s)")
    elif template == "value_to_key":
        s = (f"a key newly added to {p['a']} is a key of {p['b']} afterwards" if positive
             else f"a key newly added to {p['a']} is not a key of {p['b']} afterwards")
    elif template == "gui_validation_agreement":
        tag = " (newly introduced)" if p.get("transition") else ""
        s = (f"the GUI validation-error icon agrees with actual schema validity{tag}" if positive
             else f"the GUI validation-error icon disagrees with actual schema validity{tag}")
    elif template in ("gui_required_agreement", "gui_value_agreement"):
        what = ("required-star matches the schema's required list" if template == "gui_required_agreement"
                else "displayed value matches the data value")
        tag = " (newly introduced)" if p.get("transition") else ""
        s = (f"the GUI {what}{tag}" if positive else f"the GUI {what.split(' matches')[0]} does not match{tag}")
    elif template == "format_agreement":
        label = f"{p['view'].capitalize()} Editor"
        if p.get("transition"):
            s = (f"if the {label}'s Text View content matched its format selector before the step, it still matches after"
                 if positive else
                 f"the {label}'s Text View content stops matching its format selector")
        else:
            s = (f"the {label}'s Text View content is in the format its format selector shows" if positive
                 else f"the {label}'s Text View content is not in the format its format selector shows")
    elif template == "text_store_agreement":
        label = f"{p['view'].capitalize()} Editor"
        s = (f"the {label}'s Text View shows the same content as its store" if positive
             else f"the {label}'s Text View does not match its store")
    elif template == "undo_inverse":
        s = (f"undoing an edit restores the {p['store']} to its state before the edit" if positive
             else f"undoing an edit leaves the {p['store']} different from before the edit")
    elif template == "gui_row_set":
        tag = " (newly introduced)" if p.get("transition") else ""
        s = (f"every GUI row belongs to the data or the schema{tag}" if positive
             else f"the GUI has a row that belongs to neither the data nor the schema{tag}")
    elif template == "gui_renders":
        tag = " (newly introduced)" if p.get("transition") else ""
        s = (f"the GUI shows rows for the data and no loading spinner{tag}" if positive
             else f"the GUI is blank or stuck on a loading spinner{tag}")
    elif template == "app_health":
        s = ("the app writes no console error and throws no exception" if positive
             else "the app writes a console error or throws an exception")
    elif template == "validity_agreement":
        if p["variant"] == "app_flags_imply_meta_invalid":
            s = ("when the app logs that the schema is invalid, the meta-schema rejects the schema too" if positive
                 else "the app logs that the schema is invalid although the meta-schema accepts it")
        else:
            s = ("when the meta-schema rejects the schema, the app logs that it is invalid" if positive
                 else "the meta-schema rejects the schema without the app logging it")
    elif template == "edit_distance":
        k = p.get("k", "k")
        if p["mode"] == "exact":
            s = (f"the {p['facet']} is exactly {k} edit(s) away from its state before the step" if positive
                 else f"the {p['facet']} is not exactly {k} edit(s) away from its state before the step")
        else:
            s = (f"the {p['facet']} is at most {k} edit(s) away from its state before the step" if positive
                 else f"the {p['facet']} is more than {k} edit(s) away from its state before the step")
    elif template == "root_type":
        s = (f"the root value of the {p['store']} has the JSON type {p['value_type']}" if positive
             else f"the root value of the {p['store']} does not have the JSON type {p['value_type']}")
    elif template == "key_count_stability":
        s = (f"the number of top-level keys of {p['facet']} is unchanged" if positive
             else f"the number of top-level keys of {p['facet']} changes")
    elif template == "gui_rows_complete":
        label = _VIEW_LABELS[p["view"]]
        s = (f"every top-level key of the {label[0]} has a row in the {label[1]} GUI View" if positive
             else f"some top-level key of the {label[0]} has no row in the {label[1]} GUI View")
    elif template == "gui_store_value":
        label = _VIEW_LABELS[p["view"]]
        s = (f"the value shown in a {label[1]} GUI input equals the {label[0]}'s value" if positive
             else f"the value shown in a {label[1]} GUI input differs from the {label[0]}'s value")
    elif template == "gui_icon_presence":
        s = ("the Schema Editor GUI shows a validation-error icon exactly when the edited schema violates the meta-schema"
             if positive else
             "the Schema Editor GUI shows a validation-error icon when the schema is valid, or none when it is invalid")
    else:
        s = f"{template} {p}"
    if p.get("guard"):
        s += f", when {GUARD_STATEMENTS[p['guard']]}"
    return prefix + s


def describe_rule(rule: dict) -> dict[str, str]:
    """Full description of a rule: trigger, when it applies, what it asserts and what each name refers to."""
    t, cat, p = rule["template"], rule["category"], rule["params"]
    pos = rule["direction"] == "positive"
    plain_cat, _, stratum = cat.partition("#")
    base, _, tab = plain_cat.partition("@")
    use_text = {"first": "; only the FIRST such step of a run",
                "later": "; only the second and later such steps of a run"}.get(stratum, "")

    if cat == "*":
        trigger = "every step, whatever the action and tab"
    else:
        tab_text = {"data": "the Data Editor", "schema": "the Schema Editor",
                                                   "any": "either editor"}.get(tab, f"tab '{tab}'")
        trigger = (f"steps whose action is in category '{base}' ({ACTION_CATEGORY_DESCRIPTIONS.get(base, base)}), "
                   f"performed while {tab_text} was active - the tab showing in the snapshot BEFORE the step; "
                   f"Text View actions use the editor named in the action{use_text}")

    facets: list[str] = []
    definitions = None
    if t == "stability":
        if "path" in p:
            target = "snapshot['de_schema']" + "".join(f"[{k!r}]" for k in p["path"])
            applies = f"{target} exists before or after the step"
            asserts = (f"{target} has the same value before and after the step" if pos
                       else f"{target} has a different value after the step")
        else:
            facets = [p["facet"]]
            applies = "always"
            asserts = (f"{p['facet']} is deep-equal before and after the step" if pos
                       else f"{p['facet']} differs after the step")
    elif t == "change_count":
        facets = [p["facet"]]
        applies = "always"
        k = p.get("expected", "k")
        asserts = (f"DeepDiff(before {p['facet']}, after {p['facet']}, ignore_order=True) reports exactly {k} change entries"
                   if pos else f"DeepDiff reports a number of change entries other than {k} for {p['facet']}")
    elif t == "value_to_key":
        facets = [p["a"], p["b"]]
        applies = f"a top-level key was newly added to {p['a']} - checked once per added key"
        asserts = (f"that key is a top-level key of {p['b']} after the step" if pos
                   else f"that key is not a top-level key of {p['b']} after the step")
    elif t == "gui_validation_agreement":
        applies = ("a property row has both a GUI validation-error-icon reading and a declared "
                   "schema" + (" that were IN AGREEMENT before this step" if p.get("transition") else "")
                   + ", checked after the step")
        asserts = ("the icon's shown/hidden state agrees with whether the value actually violates its declared schema"
                   if pos else
                   "the icon's shown/hidden state disagrees with whether the value actually violates its declared schema")
        definitions = ("icon_shown = whether a validation-error-icon DOM element is rendered in that property's "
                       "GUI row (PropertyMetadata.vue's own isInvalid() reading); actually violates = "
                       "_value_violates_schema (Draft202012Validator + RefResolver) against the property's declared "
                       "schema - same check INV-GUI-ERR-01 uses. Rows at any depth ('address.city', 'nickNames[0]'); "
                       "rows with no data value, no single declared schema, or a $ref that cannot be followed are skipped.")
    elif t in ("gui_required_agreement", "gui_value_agreement"):
        req = t == "gui_required_agreement"
        applies = ("a property row has a GUI reading" + (" that AGREED before this step" if p.get("transition") else "")
                   + ", checked after the step")
        asserts = (("the required-star is shown when the name is required (always or under a condition) and hidden when it is not always required" if req
                    else "the GUI input value equals the data value") if pos else
                   ("the required-star disagrees with schema['required']" if req
                    else "the GUI input value differs from the data value"))
        definitions = ("de_gui[key] from capture_gui_facts; rows at any depth ('address.city', 'nickNames[0]')"
                       + ("; the parent's required list is read through $ref, allOf, anyOf, oneOf, then/else and "
                          "dependentRequired; array elements are skipped" if req
                          else "; primitive values only (numbers compared as floats)"))
    elif t == "undo_inverse":
        store = RESTORE_STORE_DEFINITIONS[p["store"]]
        applies = (f"an edit that changed the data or the schema is directly followed by an undo in the same run, and the {p['store']} "
                   f"({store}) is available before the edit and after the undo")
        asserts = (f"the {p['store']} after the undo deep-equals the {p['store']} before the edit" if pos
                   else f"the {p['store']} after the undo differs from the {p['store']} before the edit")
        definitions = (f"{p['store']} = {store}. Checked on (snapshot before the edit, snapshot after the undo); the edit "
                       f"is named in the 'undone' field of a counter-example. The loop checks it on an undo that directly follows such an edit.")
    elif t == "gui_row_set":
        applies = ("the Data Editor's GUI has rows after the step" + (" and had none that were stale before it" if p.get("transition") else ""))
        asserts = ("every GUI row is a path in the data or a path the schema declares" if pos
                   else "at least one GUI row is neither a path in the data nor a path the schema declares")
        definitions = ("rows = the keys of snapshot['de_gui'] without the settings rows (textEditor.*, guiEditor.*); a path is "
                       "looked up in snapshot['de_data'] and, through $ref/allOf/anyOf/oneOf/then/else, in snapshot['de_schema']; "
                       "a path that runs into a $ref that cannot be followed counts as declared. One observation per step; "
                       "a counter-example lists the stale rows.")
    elif t == "text_store_agreement":
        tk, sk = TEXT_VIEWS[p["view"]]
        applies = f"snapshot['{tk}']['parsed'] is present after the step (that editor's Text View was showing and parsed)"
        asserts = (f"snapshot['{tk}']['parsed'] deep-equals snapshot['{sk}'] (YAML: ISO timestamps compared as instants)"
                   if pos else f"snapshot['{tk}']['parsed'] differs from snapshot['{sk}']")
    elif t == "format_agreement":
        tk = TEXT_VIEWS[p["view"]][0]
        judgeable = (f"snapshot['{tk}']['format'] is set and snapshot['{tk}']['raw'] has a detectable format")
        if p.get("transition"):
            applies = (f"BEFORE the step: {judgeable} and the two match; AFTER the step: {judgeable}")
        else:
            applies = f"after the step, {judgeable}"
        asserts = (f"after the step, the detected format of snapshot['{tk}']['raw'] equals snapshot['{tk}']['format']"
                   if pos else
                   f"after the step, the detected format of snapshot['{tk}']['raw'] differs from snapshot['{tk}']['format']")
        definitions = (f"snapshot['{tk}']['format'] = the label shown by the format selector ('json'/'yaml'); "
                       f"detected format = 'json' if json.loads gives an object/array, 'yaml' if yaml.safe_load gives "
                       f"a mapping/list and the text does not start with '{{' or '[', otherwise not judgeable "
                       f"(empty, scalar, or half-edited text is skipped)")
    elif t == "gui_renders":
        applies = ("the Data Editor's GUI View is showing, the data is a non-empty object and the spinner was read"
                   + (" - and the GUI had rendered before the step" if p.get("transition") else "") + ", checked after the step")
        asserts = ("at least one data key has a GUI row and no loading spinner is shown" if pos
                   else "no data key has a GUI row (blank GUI), or a loading spinner is shown")
        definitions = ("rows = the top-level names of snapshot['de_gui'] without the settings rows (textEditor.*, guiEditor.*); "
                       "blank = no data key has a row (the reading of INV-D4 'GUI is blank'); spinner = snapshot['spinner_present'] "
                       "(the TreeTable loading icon of INV-G2, read when the snapshot is taken)")
    elif t == "app_health":
        applies = "snapshot['console_errors_since_last_step'] was recorded for the step"
        asserts = ("no console error and no uncaught exception was recorded during the step" if pos
                   else "at least one console error or uncaught exception was recorded during the step")
        definitions = ("console errors = Playwright 'console' events of type error plus 'pageerror' events, cut per snapshot "
                       "(tests/invariants/app_errors.py, kept apart from INV-G1's list). The app reloads the schema from a debounced "
                       "callback, so a message can land on the snapshot after the edit that caused it.")
    elif t == "validity_agreement":
        if p["variant"] == "app_flags_imply_meta_invalid":
            applies = "the app logged that the schema is invalid during the step, and the meta-schema verdict was recorded"
            asserts = ("the meta-schema also rejects the edited schema" if pos
                       else "the meta-schema accepts the edited schema")
        else:
            applies = "the meta-schema rejects the edited schema after the step, and the app's log was recorded"
            asserts = ("the app logged that the schema is invalid during the step" if pos
                       else "the app did not log that the schema is invalid during the step")
        definitions = ("meta-schema verdict = snapshot['schema_valid_meta'] (schema_validity.py, incl. schemas Ajv refuses to compile); "
                       "app verdict = snapshot['app_logged_invalid'] (the app wrote 'schema is invalid' to the console). Recorded apart "
                       "so that a disagreement stays visible. The app logs only when it reloads the schema, so the second direction "
                       "need not hold.")
    elif t == "edit_distance":
        field = {"data": "de_data", "schema": "de_schema"}[p["facet"]]
        k = p.get("k", "k")
        applies = f"snapshot['{field}'] is recorded before and after the step and the distance fits the work limit ({p['work_limit']})"
        asserts = ((f"the edit distance of {p['facet']} between before and after is exactly {k}" if p["mode"] == "exact"
                    else f"the edit distance of {p['facet']} between before and after is at most {k}") if pos else
                   (f"the edit distance of {p['facet']} between before and after is not {k}" if p["mode"] == "exact"
                    else f"the edit distance of {p['facet']} between before and after is greater than {k}"))
        definitions = ("edit distance = minimum number of node inserts, deletes and renames that turn the JSON before into the JSON "
                       "after (JEDI, Hütter et al., SIGMOD 2022): objects unordered, arrays ordered, one key or one value is one "
                       "node. Computed top-down (inference/json_edit_distance.py), which is an upper bound on the true JEDI "
                       "distance. k is the most common non-zero distance of the action category in the traces.")
    elif t == "root_type":
        field = {"data": "de_data", "schema": "de_schema"}[p["store"]]
        applies = f"snapshot['{field}'] is recorded after the step"
        asserts = (f"snapshot['{field}'] is a JSON {p['value_type']}" if pos
                   else f"snapshot['{field}'] is not a JSON {p['value_type']}")
        definitions = ("JSON types: object, array, string, number (int and float alike), boolean, null. Checks the root of the store "
                       "only, after the step. A counter-example names the type found.")
    elif t == "key_count_stability":
        facets = [p["facet"]]
        applies = "always"
        asserts = (f"the set of top-level keys of {p['facet']} has the same size before and after the step" if pos
                   else f"the set of top-level keys of {p['facet']} has a different size after the step")
    elif t == "gui_rows_complete":
        gui, store = {"data": ("de_gui", "de_data"), "schema": ("se_gui", "se_data")}[p["view"]]
        applies = f"snapshot['{gui}'] and snapshot['{store}'] (an object with at least one matchable key) are recorded after the step"
        asserts = (f"every matchable top-level key of snapshot['{store}'] is a row key of snapshot['{gui}']" if pos
                   else f"some matchable top-level key of snapshot['{store}'] is not a row key of snapshot['{gui}']")
        definitions = (f"rows = the keys of snapshot['{gui}'] (capture_gui_facts); matchable key = a non-empty top-level key without '.', '[' "
                       "or ']'. One observation per step; a counter-example lists the keys without a row.")
    elif t == "gui_store_value":
        gui, store = {"data": ("de_gui", "de_data"), "schema": ("se_gui", "se_data")}[p["view"]]
        applies = f"a row of snapshot['{gui}'] has an input value and its path holds a primitive in snapshot['{store}'], checked after the step"
        asserts = ("the GUI input value equals the stored value" if pos else "the GUI input value differs from the stored value")
        definitions = (f"rows at any depth ('a.b', 'list[0]'); numbers compared as floats; rows without a path in snapshot['{store}'] "
                       "(settings rows) and object / array values are skipped. One observation per row.")
    elif t == "gui_icon_presence":
        applies = "snapshot['se_gui'] and snapshot['schema_valid_meta_only'] are recorded after the step"
        asserts = ("at least one Schema Editor GUI row shows a validation-error icon exactly when the edited schema violates the meta-schema" if pos
                   else "the Schema Editor GUI shows an icon although the schema is valid, or none although it is invalid")
        definitions = ("icon = icon_shown of any row in snapshot['se_gui']; meta-schema verdict = snapshot['schema_valid_meta_only'] "
                       "(the edited schema against the meta-schema only, without the app's compile checks, because the icons reflect "
                       "nothing else; schema_validity.py). Coarse on purpose: any icon or none. The snapshot is taken at once, so an "
                       "icon that the GUI draws a moment late counts as a mismatch.")
    else:
        applies, asserts = "?", "?"

    if p.get("guard"):
        applies = f"{applies}; and the guard holds: {GUARD_DEFINITIONS[p['guard']]}"
    if definitions is None:
        definitions = "; ".join(f"{f} = {FACET_DEFINITIONS[f]}" for f in dict.fromkeys(facets))
    return {"trigger": trigger, "applies_when": applies, "asserts": asserts,
            "definitions": definitions or "-"}


def _params_label(template: str, params: dict) -> str:
    label = _template_label(template, params)
    return f"{label} [guard: {params['guard']}]" if params.get("guard") else label


def _template_label(template: str, params: dict) -> str:
    if template == "edit_distance":
        return f"edit distance of {params['facet']} ({params['mode']})"
    if template == "gui_renders":
        return "GUI renders" + (" (transition)" if params.get("transition") else "")
    if template == "app_health":
        return "app health (console)"
    if template == "validity_agreement":
        return f"schema validity: {params['variant']}"
    if template == "stability":
        return _stability_target(params)
    if template == "change_count":
        return params["facet"]
    if template == "text_store_agreement":
        return f"text({params['view']}) vs store({params['view']})"
    if template == "format_agreement":
        return f"format label vs content ({params['view']}{', transition' if params.get('transition') else ''})"
    if template == "undo_inverse":
        return f"undo restores {params['store']}"
    if template == "gui_row_set":
        return "GUI rows vs data and schema" + (" (transition)" if params.get("transition") else "")
    if template == "gui_validation_agreement":
        return "GUI icon vs schema validity" + (" (transition)" if params.get("transition") else "")
    if template == "gui_required_agreement":
        return "GUI required-star vs schema" + (" (transition)" if params.get("transition") else "")
    if template == "gui_value_agreement":
        return "GUI value vs data" + (" (transition)" if params.get("transition") else "")
    if template == "root_type":
        return f"root type of {params['store']}: {params['value_type']}"
    if template == "key_count_stability":
        return f"key count of {params['facet']}"
    if template == "gui_rows_complete":
        return f"GUI rows complete ({params['view']})"
    if template == "gui_store_value":
        return f"GUI value vs store ({params['view']})"
    if template == "gui_icon_presence":
        return f"GUI icon vs meta-schema ({params['view']})"
    return f"{params['a']} | {params['b']}"