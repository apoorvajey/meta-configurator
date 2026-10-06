"""DOMDiscoverer: lists the actions available on the current page from its data-testid attributes."""
from __future__ import annotations

from playwright.sync_api import Page
from tests.shared.python.utils import SessionMode
from tests.shared.python.test_panel import TestPanel
from tests.mutation_testing.schema_path_enumerator import (
    enumerate_addable_properties,
    additional_properties_allowed,
    resolve_subschema_at_path,
)
from tests.mutation_testing.discovery.element import (
    ElementDescriptor,
    _classify,
    _infer_element_mode,
)
from tests.mutation_testing.discovery.paths import (
    _collect_container_paths,
    _collect_leaf_paths,
    humanize_value,
    path_to_testid_string,
)
from tests.mutation_testing.discovery.page_capture import _get_current_active_mode, js_click_for_testid
from tests.mutation_testing.discovery.schema_resolution import META_SCHEMA_SIMPLIFIED


class DOMDiscoverer:

    def __init__(self, page: Page):
        self.page = page

    def discover(self) -> list[ElementDescriptor]:
        # Text View actions only work in JSON; discovering them in YAML would waste steps on silent no-ops
        current_format = "json"
        try:
            current_format = self.page.get_by_test_id("format-selector").inner_text().lower()
        except Exception:
            pass
        is_json_format = "json" in current_format

        raw: list[dict] = self.page.evaluate("""
            () => {
                const skip = [
                    'current-path', 'submit-', 'current-selected', 'test-component',
                    'data-input', 'schema-input', 'current-path-input',
                    'current-selected-element-input', 'property-metadata-',
                    'required-star', 'validation-error-icon',
                ];

                function resolveAddPropertyLabel(el) {
                    const row = el.closest('tr');
                    if (!row) return '';
                    const currentLevel = parseInt(row.getAttribute('aria-level') || '1');
                    const parentLevel = currentLevel - 1;
                    let prev = row.previousElementSibling;
                    while (prev) {
                        const prevLevel = parseInt(prev.getAttribute('aria-level') || '1');
                        const label = prev.querySelector('[id^="_label_"]');
                        if (prevLevel === parentLevel && label) {
                            return label.id.replace('_label_', '');
                        }
                        prev = prev.previousElementSibling;
                    }
                    return '';
                }

                return [...document.querySelectorAll('[data-testid]')]
                    .filter(el => {
                        if (el.offsetParent === null) return false;
                        const testid = el.getAttribute('data-testid') || '';
                        for (const s of skip) {
                            if (testid.startsWith(s) || testid === s.replace(/-$/, '')) {
                                return false;
                            }
                        }
                        if (testid.startsWith('input-select-')) {
                            if (testid.startsWith('input-select-guiEditor.')) return false;
                            if (testid.startsWith('input-select-textEditor.')) return false;
                            if (testid.startsWith('input-select-schemaDiagram.')) return false;
                            const isInputItself = el.tagName === 'INPUT' && (el.type === 'text' || !el.type);
                            return isInputItself || el.querySelector('[role="combobox"], [role="spinbutton"], .p-selectbutton, [role="textbox"], input[type="text"], input:not([type])') !== null;
                        }
                        // Skip rename/remove/add with empty path suffix
                        if ((testid === 'rename-property-') ||
                            (testid === 'remove-property-') ||
                            (testid === 'add-property-')) return false;
                        return true;
                    })
                    .map(el => {
                        let testid = el.getAttribute('data-testid');
                        if (testid === 'add-property-') {
                            const parentPath = resolveAddPropertyLabel(el);
                            testid = 'add-property-' + parentPath;
                        }
                        return {
                            testid: el.getAttribute('data-testid'),  // ← use getAttribute directly, not the variable
                            tag:    el.tagName.toLowerCase(),
                            role:   el.getAttribute('role') || '',
                            value:  el.value || el.innerText?.slice(0, 40) || '',
                        };
                    });
            }
        """)

        expand_raw: list[dict] = self.page.evaluate("""
            () => {
                return [...document.querySelectorAll('button.p-treetable-node-toggle-button')]
                    .filter(btn => btn.style.visibility !== 'hidden' && btn.offsetParent !== null)
                    .map(btn => {
                        const row = btn.closest('tr');
                        const label = row?.querySelector('[id^="_label_"]');
                        const path = label?.id?.replace('_label_', '');
                        if (!path || path.includes('..') || path.endsWith('.')) return null;

                        const nextRow = row?.nextElementSibling;
                        const nextToggler = nextRow?.querySelector('button.p-treetable-node-toggle-button');
                        const currentMargin = parseFloat(btn.style.marginLeft) || 0;
                        const nextMargin = nextToggler ? parseFloat(nextToggler.style.marginLeft) || 0 : -1;
                        const isExpanded = nextMargin > currentMargin;

                        return {
                            testid: (isExpanded ? 'collapse-' : 'expand-') + path,
                            tag:    'button',
                            role:   '',
                            value:  '',
                        };
                    })
                    .filter(x => x !== null);
            }
        """)

        # Undo/redo use plain ids (undo, redo, schema_undo, schema_redo), not data-testid, so they are
        # clicked via js_click. Settings undo/redo are excluded: Settings is out of scope for invariants.
        undo_redo_raw: list[dict] = self.page.evaluate("""
            () => {
                return ['undo', 'redo', 'schema_undo', 'schema_redo']
                    .map(id => document.getElementById(id))
                    .filter(el => el && !el.disabled && el.offsetParent !== null)
                    .map(el => ({
                        testid: el.id,
                        tag:    el.tagName.toLowerCase(),
                        role:   el.getAttribute('role') || '',
                        value:  '',
                    }));
            }
        """)

        (
            edit_text_raw,
            add_child_text_raw,
            change_type_text_raw,
            remove_child_text_raw,
            rename_child_text_raw,
            add_array_item_text_raw,
            remove_array_item_text_raw,
            clear_text_raw,
        ) = self._discover_text_view_actions(is_json_format)

        current_mode = _get_current_active_mode(self.page)

        result: list[ElementDescriptor] = []
        for r in (raw + expand_raw + undo_redo_raw + edit_text_raw + add_child_text_raw
                  + change_type_text_raw + remove_child_text_raw + rename_child_text_raw
                  + add_array_item_text_raw + remove_array_item_text_raw + clear_text_raw):
            interaction = _classify(r["testid"])
            if interaction is None:
                continue
            result.append(ElementDescriptor(
                testid=r["testid"],
                tag=r["tag"],
                role=r["role"],
                interaction=interaction,
                label=r["value"] if r["testid"].startswith("edit-text-") else r["testid"],
                js_click=js_click_for_testid(r["testid"]),
                mode=_infer_element_mode(r["testid"], current_mode),
            ))

        result = [el for el in result if el.testid != "mode-active-true"]

        seen = set()
        deduped = []
        for el in result:
            if el.testid.startswith("mode-active-"):
                if el.testid in seen:
                    continue
                seen.add(el.testid)
            deduped.append(el)
        return deduped

    def _discover_text_view_actions(self, is_json_format) -> tuple[list[dict], ...]:
        """Text View actions of both editors, one list per action type: edit, add child,
        change type, remove child, rename child, add array item, remove array item, clear."""
        # Text View actions are enumerated per node (one per editable leaf, one add-child per missing property,
        # ...) so they appear in the action list like every other action. One pass per mode.
        edit_text_raw: list[dict] = []
        add_child_text_raw: list[dict] = []
        change_type_text_raw: list[dict] = []
        remove_child_text_raw: list[dict] = []
        rename_child_text_raw: list[dict] = []
        add_array_item_text_raw: list[dict] = []
        remove_array_item_text_raw: list[dict] = []
        clear_text_raw: list[dict] = []

        for mode_str, session_mode in (
            ("dataEditor", SessionMode.DataEditor),
            ("schemaEditor", SessionMode.SchemaEditor),
        ):
            has_editor = self.page.evaluate(
                f'() => document.querySelector(\'[id^="code-editor-{mode_str}"]\') !== null'
            )
            if not has_editor or not is_json_format:
                continue
            try:
                tp = TestPanel(self.page, session_mode)
                target = tp.get_data()  # the document being edited (the "data" slot on both tabs); see the governing_schema note just below for why this must NOT be tp.get_schema() here
            except Exception:
                continue

            # edit-text (operation: change a value)
            try:
                leaves = _collect_leaf_paths(target)
            except Exception:
                leaves = []
            for leaf_path, leaf_value in leaves:
                path_str = path_to_testid_string(leaf_path)
                if not path_str:
                    continue  # root-level primitive: no property-data- row to click
                edit_text_raw.append({
                    "testid": f"edit-text-{mode_str}:{path_str}",
                    "tag": "div",
                    "role": "",
                    "value": humanize_value(leaf_value),
                })

            # Object-node operations (add-child, change-type, remove-child, rename-child) share one container walk
            try:
                objects = _collect_container_paths(target, want_type=dict)
            except Exception:
                objects = []

            # Governing schema for the add-child targets: the user's schema in the Data Editor, the meta-schema in
            # the Schema Editor. In the Schema Editor TestPanel.get_schema() already returns the meta-schema, which
            # is why the walked document (`target`) is read with get_data() on both tabs.
            if mode_str == "dataEditor":
                try:
                    governing_schema = TestPanel(self.page, SessionMode.DataEditor).get_schema()
                except Exception:
                    governing_schema = None
            else:
                governing_schema = META_SCHEMA_SIMPLIFIED

            for obj_path, obj_value in objects:
                path_str = path_to_testid_string(obj_path)

                node_schema = None
                if governing_schema is not None:
                    try:
                        node_schema = resolve_subschema_at_path(governing_schema, obj_path, target)
                    except Exception:
                        node_schema = None

                if node_schema is not None:
                    try:
                        addable = enumerate_addable_properties(governing_schema, node_schema, obj_value)
                        allow_generic = additional_properties_allowed(governing_schema, node_schema, obj_value)
                    except Exception:
                        addable, allow_generic = [], True
                    for prop_name, _child_schema in addable:
                        add_child_text_raw.append({
                            "testid": f"add-child-text-{mode_str}:{path_str}:{prop_name}",
                            "tag": "div",
                            "role": "",
                            "value": f"{len(obj_value)} existing propert{'y' if len(obj_value) == 1 else 'ies'}",
                        })
                    if allow_generic:
                        add_child_text_raw.append({
                            "testid": f"add-child-text-{mode_str}:{path_str}:*",
                            "tag": "div",
                            "role": "",
                            "value": f"{len(obj_value)} existing propert{'y' if len(obj_value) == 1 else 'ies'}",
                        })
                else:
                    # No schema loaded, or the node is not covered by it: name and type are
                    # chosen at execution time.
                    add_child_text_raw.append({
                        "testid": f"add-child-text-{mode_str}:{path_str}",
                        "tag": "div",
                        "role": "",
                        "value": f"{len(obj_value)} existing propert{'y' if len(obj_value) == 1 else 'ies'}",
                    })

                # Root-level change-type in the Schema Editor is excluded: it can turn the whole schema into a
                # scalar, which breaks everything that assumes the schema is a dict (value generation,
                # invariant checkers, this discoverer). In the Data Editor it only makes the data
                # questionable, which the invariants can check, so it stays.
                is_schema_root = (mode_str == "schemaEditor" and not obj_path)
                if not is_schema_root:
                    change_type_text_raw.append({
                        "testid": f"change-type-text-{mode_str}:{path_str}",
                        "tag": "div",
                        "role": "",
                        "value": f"currently an object with {len(obj_value)} propert"
                                 f"{'y' if len(obj_value) == 1 else 'ies'}",
                    })
                for key in obj_value.keys():
                    child_path_str = path_to_testid_string(obj_path + [key])
                    remove_child_text_raw.append({
                        "testid": f"remove-child-text-{mode_str}:{child_path_str}",
                        "tag": "div",
                        "role": "",
                        "value": humanize_value(obj_value[key]),
                    })
                    rename_child_text_raw.append({
                        "testid": f"rename-child-text-{mode_str}:{child_path_str}",
                        "tag": "div",
                        "role": "",
                        "value": f'"{key}"',
                    })

            # Array operations (add-array-item, remove-array-item). The schema-driven item type is chosen in
            # the executor, so nothing extra is needed here.
            try:
                arrays = _collect_container_paths(target, want_type=list)
            except Exception:
                arrays = []
            for arr_path, arr_value in arrays:
                path_str = path_to_testid_string(arr_path)
                add_array_item_text_raw.append({
                    "testid": f"add-array-item-text-{mode_str}:{path_str}",
                    "tag": "div",
                    "role": "",
                    "value": f"{len(arr_value)} existing item{'' if len(arr_value) == 1 else 's'}",
                })
                for i, item_value in enumerate(arr_value):
                    item_path_str = path_to_testid_string(arr_path + [i])
                    remove_array_item_text_raw.append({
                        "testid": f"remove-array-item-text-{mode_str}:{item_path_str}",
                        "tag": "div",
                        "role": "",
                        "value": humanize_value(item_value),
                    })

            # Whole-document clear: one entry per mode, only if there is content
            has_content = bool(target) if isinstance(target, (dict, list)) else target is not None
            if has_content:
                clear_text_raw.append({
                    "testid": f"clear-text-{mode_str}",
                    "tag": "div",
                    "role": "",
                    "value": f"{len(target)} top-level key(s)" if isinstance(target, dict) else "content",
                })

        return (
            edit_text_raw,
            add_child_text_raw,
            change_type_text_raw,
            remove_child_text_raw,
            rename_child_text_raw,
            add_array_item_text_raw,
            remove_array_item_text_raw,
            clear_text_raw,
        )
