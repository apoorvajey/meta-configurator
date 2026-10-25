"""Weighted choice of the next action (Mode > View > Action type > element) with a bias towards the current area of the document."""
from __future__ import annotations

import random
from dataclasses import dataclass
from tests.mutation_testing.discovery.element import ElementDescriptor
from tests.mutation_testing.discovery.paths import testid_path_string_to_list
from tests.mutation_testing.loop.action_weights import (
    _MUTATING_CATEGORIES,
    _UNDO_REDO_TESTIDS,
    _element_action_key,
    _element_action_key_by_testid,
    _load_action_weights,
    _mode_label,
)


# Locality bias: the Mode, View and element picks favour the area the loop worked in recently, like a
# user who stays in one part of the document for a few actions. The action type is not biased, since the
# kind of action is not spatial.
# The parameters are set by the "locality_bias" block of run_config.json (see profile_config.resolve_locality_config);
# the field defaults below are the fallback when that block is missing or invalid.
@dataclass(frozen=True)
class LocalityConfig:
    decay_steps: int = 4                 # steps without reinforcement until the focus expires
    mode_view_multiplier: float = 4.0    # max multiplier on the focus Mode/View weight (at age 0)
    element_multiplier: float = 6.0      # max multiplier per shared path segment at the element pick
    min_shared_depth: int = 1            # shared leading segments needed to count as "nearby"


# Text View testids are "{prefix}{mode}:{path}" (add-child-text adds ":{prop_name}"); GUI View testids
# are the prefix followed by the path.
_TEXT_VIEW_PATH_PREFIXES = (
    "edit-text-", "add-child-text-", "change-type-text-",
    "remove-child-text-", "rename-child-text-",
    "add-array-item-text-", "remove-array-item-text-",
)


_GUI_VIEW_PATH_PREFIXES = (
    "input-string-", "input-number-", "input-boolean-", "input-select-",
    "add-item-", "add-property-", "expand-", "collapse-",
    "rename-property-", "remove-property-",
)


def _focus_key_for_element(el: ElementDescriptor) -> tuple[str, str, list] | None:
    """(mode_label, view, parsed_path) for an action that targets one node of the document tree, else None.

    None covers mode switch, format selector, undo/redo and clear-text (which targets the whole document).
    parsed_path is a list, so "Vehicles.car1" and "Vehicles.car10" do not share a segment.
    """
    testid = el.testid
    mode_label = _mode_label(el)

    for prefix in _TEXT_VIEW_PATH_PREFIXES:
        if testid.startswith(prefix):
            mode_and_rest = testid[len(prefix):]
            _, _, rest = mode_and_rest.partition(":")
            path_str, _, _ = rest.partition(":")  # drop add-child-text's :prop_name
            return (mode_label, "Text View", testid_path_string_to_list(path_str))

    if testid.startswith("clear-text-"):
        return None

    for prefix in _GUI_VIEW_PATH_PREFIXES:
        if testid.startswith(prefix) and testid != prefix:
            path_str = testid[len(prefix):]
            return (mode_label, "GUI View", testid_path_string_to_list(path_str))

    return None  # mode switch, format selector, undo/redo


def _subtree_proximity(path_a: list, path_b: list) -> int:
    """Number of leading segments two parsed paths share (0 = unrelated branches)."""
    common = 0
    for a, b in zip(path_a, path_b):
        if a != b:
            break
        common += 1
    return common


def _locality_strength(age: int, decay_steps: int) -> float:
    """Linear fade from 1.0 (just touched) to 0.0 (age >= decay_steps)."""
    if decay_steps <= 0 or age >= decay_steps:
        return 0.0
    return 1.0 - (age / decay_steps)


def _update_focus(
    current_mode: str | None, current_view: str | None, current_path: list | None, current_age: int,
    touched_key: tuple[str, str, list] | None, locality: LocalityConfig,
) -> tuple[str | None, str | None, list | None, int]:
    """Locality-focus transition, called once per successful step with that step's focus key.

    - reinforce: the touched key is in the same (mode, view) and shares enough leading path segments with the
      focus: it becomes the focus and the age resets.
    - new anchor: there is no focus or it has expired: the touched key becomes the focus.
    - drift: otherwise (including untargeted actions) the old focus stays and its age grows by one.
    """
    if touched_key is not None:
        touched_mode, touched_view, touched_path = touched_key
        if (current_path is not None
                and touched_mode == current_mode
                and touched_view == current_view
                and _subtree_proximity(current_path, touched_path) >= locality.min_shared_depth):
            return touched_mode, touched_view, touched_path, 0
        if current_path is None or current_age >= locality.decay_steps:
            return touched_mode, touched_view, touched_path, 0

    if current_path is not None:
        return current_mode, current_view, current_path, current_age + 1
    return current_mode, current_view, current_path, current_age


def followup_element(elements: list[ElementDescriptor], testid: str | None) -> ElementDescriptor | None:
    """The element to run as a planned follow-up step: the discovered, directly clickable element with this testid,
    or None (the loop then samples normally)."""
    if not testid:
        return None
    return next((e for e in elements if e.testid == testid and not e.needs_expand), None)


def expand_first(el: ElementDescriptor, elements: list[ElementDescriptor]) -> tuple[ElementDescriptor, str | None]:
    """(element to run now, testid to run on the next step). A virtual element (needs_expand) is replaced by the
    expand step it needs, and its own testid becomes the follow-up; any other element is returned unchanged."""
    if not el.needs_expand:
        return el, None
    expand = next((e for e in elements if e.testid == el.needs_expand), None)
    return (expand, el.testid) if expand is not None else (el, None)


def _weighted_sample(
    elements: list[ElementDescriptor], recent_testids: list[str] | None = None,
    focus_mode: str | None = None, focus_view: str | None = None,
    focus_path: list | None = None, focus_age: int = 0,
    pending_create: tuple[str, list] | None = None,
    locality: LocalityConfig | None = None,
) -> ElementDescriptor:
    """Pick the next element: Mode, then View, then action type, then an element within that action type.

    Each level is weighted by the config and normalised over the branches present this step. This equals one
    combined weight per element; three draws are used because they mirror the decision order.

    recent_testids: the last two executed testids. After an undo/redo (chain cap two) or a mutating action, the
    undo/redo weight is boosted ("undo_redo_boosted" instead of "undo_redo_base").
    pending_create: (mode_label, path) of a property created in the previous step. If a fill-like action
    targets exactly that path it is returned directly; otherwise sampling continues normally.
    """
    if pending_create is not None:
        target_mode, target_path = pending_create
        _FILL_LIKE_ACTION_TYPES = {"fill", "input_select_input", "input_select_other", "edit_text"}
        matches = []
        for c in elements:
            key = _focus_key_for_element(c)
            if key is None or key[0] != target_mode or key[2] != target_path:
                continue
            if _element_action_key(c, boost_undo_redo=False) in _FILL_LIKE_ACTION_TYPES:
                matches.append(c)
        if matches:
            picked = random.choice(matches)
            print(f"   create-then-fill: {target_mode} '{'.'.join(str(p) for p in target_path)}' -> {picked.testid}")
            return picked
        else:
            print(f"   create-then-fill: no fill action for {target_mode} "
                  f"'{'.'.join(str(p) for p in target_path)}', sampling normally")

    recent_testids = recent_testids or []
    last = recent_testids[-1] if recent_testids else None
    second_last = recent_testids[-2] if len(recent_testids) >= 2 else None

    last_was_undo_redo = last in _UNDO_REDO_TESTIDS
    already_chained_twice = last_was_undo_redo and second_last in _UNDO_REDO_TESTIDS
    boost_after_undo_redo_chain = last_was_undo_redo and not already_chained_twice

    last_action_key = _element_action_key_by_testid(last) if last else None
    boost_after_mutation = (not last_was_undo_redo) and (last_action_key in _MUTATING_CATEGORIES)

    boost_undo_redo = boost_after_undo_redo_chain or boost_after_mutation

    config = _load_action_weights()

    # Tag every element with its (mode, view, action type)
    tagged = [
        (el, _mode_label(el), el.view, _element_action_key(el, boost_undo_redo))
        for el in elements
    ]

    # Level 1: Mode
    mode_groups: dict[str, list] = {}
    for entry in tagged:
        mode_groups.setdefault(entry[1], []).append(entry)
    mode_weights = {
        m: config.get(m, {}).get("weight", 0)
        for m in mode_groups
        if config.get(m, {}).get("enabled", True)
    }
    mode_weights = {m: w for m, w in mode_weights.items() if w > 0}
    if not mode_weights:
        return random.choice(elements)

    locality = locality or LocalityConfig()
    locality_strength = _locality_strength(focus_age, locality.decay_steps)
    if locality_strength > 0 and focus_mode in mode_weights:
        mode_weights[focus_mode] *= 1.0 + (locality.mode_view_multiplier - 1.0) * locality_strength

    chosen_mode = random.choices(list(mode_weights.keys()), weights=list(mode_weights.values()), k=1)[0]

    # Level 2: View within the chosen Mode
    mode_config = config.get(chosen_mode, {}).get("children", {})
    view_groups: dict[str, list] = {}
    for entry in mode_groups[chosen_mode]:
        view_groups.setdefault(entry[2], []).append(entry)
    view_weights = {
        v: mode_config.get(v, {}).get("weight", 0)
        for v in view_groups
        if mode_config.get(v, {}).get("enabled", True)
    }
    view_weights = {v: w for v, w in view_weights.items() if w > 0}
    if not view_weights:
        # No enabled View has actions (e.g. Schema Diagram): stay within this Mode
        print(f"No enabled View under '{chosen_mode}' has actions this step, choosing uniformly within the Mode")
        return random.choice([entry[0] for entry in mode_groups[chosen_mode]])

    # Only favour the focus View within the focus Mode ("Text View" differs between the two editors)
    if locality_strength > 0 and chosen_mode == focus_mode and focus_view in view_weights:
        view_weights[focus_view] *= 1.0 + (locality.mode_view_multiplier - 1.0) * locality_strength

    chosen_view = random.choices(list(view_weights.keys()), weights=list(view_weights.values()), k=1)[0]

    # Level 3: action type within the chosen Mode and View
    view_config = mode_config.get(chosen_view, {}).get("children", {})
    action_groups: dict[str, list] = {}
    for entry in view_groups[chosen_view]:
        action_groups.setdefault(entry[3], []).append(entry)
    action_weights = {
        a: view_config.get(a, {}).get("weight", 0)
        for a in action_groups
        if view_config.get(a, {}).get("enabled", True)
    }
    action_weights = {a: w for a, w in action_weights.items() if w > 0}
    if not action_weights:
        print(f"No enabled action type under '{chosen_mode} > {chosen_view}' has actions this step, "
              f"choosing uniformly within the View")
        return random.choice([entry[0] for entry in view_groups[chosen_view]])
    chosen_action = random.choices(list(action_weights.keys()), weights=list(action_weights.values()), k=1)[0]

    # Concrete element: uniform among the matches, unless a focus is anchored in this (Mode, View);
    # then each candidate is weighted by the leading path segments it shares with the focus path.
    candidates = [entry[0] for entry in action_groups[chosen_action]]
    if (len(candidates) > 1 and locality_strength > 0
            and chosen_mode == focus_mode and chosen_view == focus_view and focus_path is not None):
        weights = []
        for c in candidates:
            c_key = _focus_key_for_element(c)
            depth = _subtree_proximity(focus_path, c_key[2]) if c_key is not None else 0
            if depth >= locality.min_shared_depth:
                weights.append(1.0 + (locality.element_multiplier - 1.0) * locality_strength * depth)
            else:
                weights.append(1.0)
        return random.choices(candidates, weights=weights, k=1)[0]
    return random.choice(candidates)