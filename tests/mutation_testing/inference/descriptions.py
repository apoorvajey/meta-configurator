"""Plain-language statements and descriptions of rules."""
from __future__ import annotations

from tests.mutation_testing.inference.categories import ACTION_CATEGORY_DESCRIPTIONS
from tests.mutation_testing.inference.facets import FACET_DEFINITIONS, TEXT_VIEWS


def _stability_target(params: dict) -> str:
    if "path" in params:
        return "schema." + ".".join(params["path"])
    return params["facet"]


def rule_statement(template: str, category: str, params: dict, positive: bool) -> str:
    """One-line statement of a rule."""
    prefix = "" if category == "*" else f"after {category}: "
    p = params
    if template == "stability":
        s = f"{_stability_target(p)} is unchanged" if positive else f"{_stability_target(p)} changes"
    elif template == "change_count":
        s = (f"{p['facet']} has exactly {p.get('expected', 'k')} DeepDiff change(s)" if positive
             else f"{p['facet']} does not have exactly {p.get('expected', 'k')} DeepDiff change(s)")
    elif template == "correlation":
        s = (f"if the top-level keys of {p['a']} change, the top-level keys of {p['b']} change too" if positive
             else f"if the top-level keys of {p['a']} change, the top-level keys of {p['b']} stay the same")
    elif template == "path_key":
        s = (f"a key added to / removed from {p['a']} is added to / removed from {p['b']} too" if positive
             else f"a key added to / removed from {p['a']} is not added to / removed from {p['b']}")
    elif template == "value_to_key":
        s = (f"a key newly added to {p['a']} is a key of {p['b']} afterwards" if positive
             else f"a key newly added to {p['a']} is not a key of {p['b']} afterwards")
    elif template == "key_containment":
        s = (f"every key of {p['a']} is also a key of {p['b']}" if positive
             else f"some key of {p['a']} is not a key of {p['b']}")
    elif template == "order_stability":
        s = (f"the order of {p['facet']} is unchanged" if positive
             else f"the order of {p['facet']} changes even though its set of elements doesn't")
    elif template == "gui_validation_agreement":
        tag = " (newly introduced)" if p.get("transition") else ""
        s = (f"the GUI validation-error icon agrees with actual schema validity{tag}" if positive
             else f"the GUI validation-error icon disagrees with actual schema validity{tag}")
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
    else:
        s = f"{template} {p}"
    return prefix + s


def describe_rule(rule: dict) -> dict[str, str]:
    """Full description of a rule: trigger, when it applies, what it asserts and what each name refers to."""
    t, cat, p = rule["template"], rule["category"], rule["params"]
    pos = rule["direction"] == "positive"
    base, _, tab = cat.partition("@")

    if cat == "*":
        trigger = "every step, whatever the action and tab"
    else:
        tab_text = {"data": "the Data Editor", "schema": "the Schema Editor"}.get(tab, f"tab '{tab}'")
        trigger = (f"steps whose action is in category '{base}' ({ACTION_CATEGORY_DESCRIPTIONS.get(base, base)}), "
                   f"performed while {tab_text} was active - the tab showing in the snapshot BEFORE the step; "
                   f"Text View actions use the editor named in the action")

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
    elif t == "correlation":
        facets = [p["a"], p["b"]]
        applies = f"the set of top-level keys of {p['a']} differs between before and after"
        asserts = (f"the set of top-level keys of {p['b']} also differs between before and after" if pos
                   else f"the set of top-level keys of {p['b']} is identical before and after")
    elif t == "path_key":
        facets = [p["a"], p["b"]]
        applies = f"at least one top-level key was added to or removed from {p['a']}"
        asserts = (f"at least one of those keys was added to {p['b']} (if added) or removed from {p['b']} (if removed)"
                   if pos else f"none of those keys was added to / removed from {p['b']} in the same way")
    elif t == "value_to_key":
        facets = [p["a"], p["b"]]
        applies = f"a top-level key was newly added to {p['a']} - checked once per added key"
        asserts = (f"that key is a top-level key of {p['b']} after the step" if pos
                   else f"that key is not a top-level key of {p['b']} after the step")
    elif t == "key_containment":
        facets = [p["a"], p["b"]]
        applies = f"{p['a']} is non-empty after the step"
        asserts = (f"every top-level key of {p['a']} is a top-level key of {p['b']} after the step" if pos
                   else f"at least one top-level key of {p['a']} is not a top-level key of {p['b']} after the step")
    elif t == "order_stability":
        facets = [p["facet"]]
        applies = f"the SET of {p['facet']} is unchanged (its order might still differ)"
        asserts = (f"the ORDER of {p['facet']} is also unchanged" if pos
                   else f"the ORDER of {p['facet']} differs even though its set of elements doesn't")
    elif t == "gui_validation_agreement":
        applies = ("a root-level property has both a GUI validation-error-icon reading and a declared "
                   "schema" + (" that were IN AGREEMENT before this step" if p.get("transition") else "")
                   + ", checked after the step")
        asserts = ("the icon's shown/hidden state agrees with whether the value actually violates its declared schema"
                   if pos else
                   "the icon's shown/hidden state disagrees with whether the value actually violates its declared schema")
        definitions = ("icon_shown = whether a validation-error-icon DOM element is rendered in that property's "
                       "GUI row (PropertyMetadata.vue's own isInvalid() reading); actually violates = "
                       "_value_violates_schema (Draft202012Validator + RefResolver) against the ROOT-LEVEL "
                       "property's declared schema - same check INV-GUI-ERR-01 uses. ROOT-LEVEL properties only.")
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
    else:
        applies, asserts = "?", "?"

    if definitions is None:
        definitions = "; ".join(f"{f} = {FACET_DEFINITIONS[f]}" for f in dict.fromkeys(facets))
    return {"trigger": trigger, "applies_when": applies, "asserts": asserts,
            "definitions": definitions or "-"}


def _params_label(template: str, params: dict) -> str:
    if template == "stability":
        return _stability_target(params)
    if template in ("change_count", "order_stability"):
        return params["facet"]
    if template == "text_store_agreement":
        return f"text({params['view']}) vs store({params['view']})"
    if template == "format_agreement":
        return f"format label vs content ({params['view']}{', transition' if params.get('transition') else ''})"
    if template == "gui_validation_agreement":
        return "GUI icon vs schema validity" + (" (transition)" if params.get("transition") else "")
    return f"{params['a']} | {params['b']}"
