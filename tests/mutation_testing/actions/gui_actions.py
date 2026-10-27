"""Actions on the GUI View: fill, select, add, rename and click."""
from __future__ import annotations

import random
from typing import Any
from tests.invariants.app_errors import mark_midstep
from tests.invariants.schema_validity import schema_meta_violations
from tests.mutation_testing.discovery.element import ElementDescriptor
from tests.mutation_testing.discovery.values import roll_valid
from tests.shared.python.test_panel import TestPanel
from tests.shared.python.utils import SessionMode


_FORMAT_OPTIONS = ["json", "yaml"]  # the app registers only these as live formats


class GuiActionsMixin:
    """GUI View actions of InteractionExecutor."""

    def _dismiss_rename_conflict(self, choice: str = "keep", wait_ms: int = 0) -> bool:
        """If the app's 'Rename conflict' dialog is open, click one of its two buttons so it cannot block the next click.
        choice: "keep" = 'Proceed but keep existing data' (the default, non-destructive) or "overwrite" =
        'Proceed and overwrite data'. wait_ms: how long to wait for the dialog to show up. It opens a few hundred
        milliseconds after the name is committed (known bug BUG-14), so the calls right after a commit pass a wait;
        the other calls only look at what is on the screen now. The dialog's text is recorded by the page itself
        (rename_conflict_dialogs in the snapshot, see TEMP-RENAME-05), so dismissing it here loses nothing.
        Returns True if a dialog was dismissed."""
        label = "Proceed and overwrite data" if choice == "overwrite" else "Proceed but keep existing data"
        try:
            btn = self.page.get_by_text(label, exact=True)
            if wait_ms > 0:
                try:
                    btn.first.wait_for(state="visible", timeout=wait_ms)
                except Exception:
                    return False   # no dialog within the wait: the normal case
            if btn.count() and btn.first.is_visible():
                print(f"   [rename-conflict] dialog appeared - choosing '{label}'")
                try:   # the step's screenshot is taken after this click, so keep a picture that still shows the dialog
                    self.page.wait_for_timeout(150)   # let the dialog's opening animation finish
                    self.rename_conflict_shot = self.page.screenshot()
                except Exception:
                    self.rename_conflict_shot = None
                btn.first.click(timeout=2000)
                self.page.wait_for_timeout(300)
                return True
        except Exception as e:
            print(f"   [rename-conflict] could not dismiss: {e}")
        return False

    def _fill(self, el: ElementDescriptor, value: Any) -> None:
        self._dismiss_rename_conflict()
        if el.input_type == "boolean":
            btn_label = str(value).lower() if value is not None else "true"
            container = self.page.get_by_test_id(el.testid)
            btn = container.get_by_role("button", name=btn_label)
            btn.click()
            return

        locator = self.page.get_by_test_id(el.testid)

        # Numeric testids (array items) match several elements, so take the first
        if el.property_name and el.property_name.isdigit():
            locator = locator.first

        try:
            current = locator.input_value(timeout=2000)
            print(f"   field '{el.testid}': '{current}' → '{value}'")
        except Exception:
            print(f"   field '{el.testid}': (unreadable) → '{value}'")

        locator.click()
        locator.fill(str(value) if value is not None else "")
        locator.press("Enter")

    def _rename_property(self, el: ElementDescriptor, forced_name: str | None = None) -> None:
        path = el.testid[len("rename-property-"):]
        label_id = f"_label_{path}"
        parts = path.split(".")
        parent_path = ".".join(parts[:-1]) if len(parts) > 1 else ""
        new_name = forced_name if forced_name else self._distinct_property_name(parent_path)

        pencil = self.page.get_by_test_id(el.testid)
        pencil.scroll_into_view_if_needed()
        pencil.hover()
        self.page.wait_for_timeout(100)
        pencil.click(timeout=5000)
        self.page.wait_for_timeout(200)

        try:
            self.page.wait_for_selector(
                f'[id="{label_id}"][contenteditable="true"]', timeout=2000
            )
            label = self.page.locator(f'[id="{label_id}"][contenteditable="true"]')
        except Exception:
            label = self.page.locator(f'[id="{label_id}"]').first

        label.click()
        self.page.keyboard.press("Control+a")
        self.page.keyboard.type(new_name)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(300)
        self._dismiss_rename_conflict(wait_ms=1000)

        self._last_rename = new_name
        print(f"   renamed '{path}' → '{new_name}'")

    def _mark_midstep(self) -> None:
        """Between the two stages of add-item-required (blank entry added, entry not yet filled): record the meta-schema
        verdict on the schema as it is now and mark the console, so that what the app logged about this intermediate
        state (e.g. a duplicate blank in 'required') is kept apart from the final state. See app_errors.mark_midstep."""
        try:
            schema = TestPanel(self.page, SessionMode.DataEditor).get_schema()
            violations = schema_meta_violations(self.page, schema)
            valid = None if violations is None else not violations
        except Exception:
            valid = None
        mark_midstep(self.page, valid)

    def _click_add_item_required(self) -> None:
        self.page.evaluate("""
            () => {
                const label = document.getElementById('_label_required');
                if (!label) return;
                const row = label.closest('tr');
                if (!row) return;
                const nextRow = row.nextElementSibling;
                const toggle = row.querySelector('button.p-treetable-node-toggle-button');
                if (!toggle) return;
                const currentMargin = parseFloat(toggle.style.marginLeft) || 0;
                const nextToggle = nextRow?.querySelector('button.p-treetable-node-toggle-button');
                const nextMargin = nextToggle ? (parseFloat(nextToggle.style.marginLeft) || 0) : -1;
                const isExpanded = nextMargin > currentMargin;
                if (!isExpanded) toggle.click();
            }
        """)
        self.page.wait_for_timeout(300)

        before_testids = set(self.page.evaluate("""
            () => [...document.querySelectorAll('[data-testid*="required["]')]
                .map(el => el.getAttribute('data-testid'))
        """))

        self._dismiss_rename_conflict()
        self.page.get_by_test_id("add-item-required").first.click(timeout=5000)
        self.page.wait_for_timeout(3000)

        after: list[dict] = self.page.evaluate("""
            () => [...document.querySelectorAll('[data-testid*="required["]')]
                .map(el => ({testid: el.getAttribute('data-testid'), value: el.value}))
        """)
        print(f"   [add-item-required] inputs found: {after}")
        self._mark_midstep()

        new_entries = [i for i in after if i["testid"] not in before_testids]
        target = new_entries[0] if new_entries else next(
            (i for i in after if i["value"] == ""), None
        )

        if target:
            testid = target["testid"]
            if testid.startswith("property-data-") or testid.startswith("property-metadata-"):
                data_testid = testid.replace("property-metadata-", "property-data-")
                inp = self.page.get_by_test_id(data_testid).locator("input, textarea").first
            else:
                inp = self.page.get_by_test_id(testid)

            name = self._generate_property_name("")
            print(f"   [add-item-required] filling '{testid}' (real input located) with '{name}'")
            inp.click()
            inp.fill(name)
            inp.press("Tab")
            self.page.wait_for_timeout(1500)
        else:
            print("   [add-item-required] no new required[N] input found — "
                  "click may have silently failed")

    def _click(self, el: ElementDescriptor, value: Any = None) -> None:
        self._dismiss_rename_conflict()
        if el.js_click:
            self.page.evaluate(el.js_click)
            return

        if el.testid.startswith("add-property-") and el.testid != "add-property-":
            self._click_add_property(el, forced_name=value)
            return
        if el.testid.startswith("remove-property-"):
            path = el.testid[len("remove-property-"):]
            btn = self.page.get_by_test_id(f"property-data-{path}").locator('button[aria-label="Remove"]').first
            try:
                btn.click(timeout=5000)
            except Exception:
                if not self._dismiss_rename_conflict():
                    raise
                btn.click(timeout=5000)
            self.page.wait_for_timeout(300)
            return
        if el.testid.startswith("rename-property-"):
            self._rename_property(el, forced_name=value)
            return

        if el.testid == "add-item-required":
            self._click_add_item_required()
            return

        if el.testid.startswith("edit-text-"):
            mode_str, _, path_str = el.testid[len("edit-text-"):].partition(":")
            self._click_edit_text(mode_str, path_str, forced_valid=value)
            return

        if el.testid.startswith("add-child-text-"):
            mode_and_path = el.testid[len("add-child-text-"):]
            mode_str, _, rest = mode_and_path.partition(":")
            path_str, _, prop_name = rest.partition(":")
            self._click_add_child_text(mode_str, path_str, target_property_name=(prop_name or None))
            return

        if el.testid.startswith("change-type-text-"):
            mode_str, _, path_str = el.testid[len("change-type-text-"):].partition(":")
            self._click_change_type_text(mode_str, path_str)
            return

        if el.testid.startswith("remove-child-text-"):
            mode_str, _, path_str = el.testid[len("remove-child-text-"):].partition(":")
            self._click_remove_child_text(mode_str, path_str)
            return

        if el.testid.startswith("rename-child-text-"):
            mode_str, _, path_str = el.testid[len("rename-child-text-"):].partition(":")
            self._click_rename_child_text(mode_str, path_str)
            return

        if el.testid.startswith("add-array-item-text-"):
            mode_str, _, path_str = el.testid[len("add-array-item-text-"):].partition(":")
            self._click_add_array_item_text(mode_str, path_str)
            return

        if el.testid.startswith("remove-array-item-text-"):
            mode_str, _, path_str = el.testid[len("remove-array-item-text-"):].partition(":")
            self._click_remove_array_item_text(mode_str, path_str)
            return

        if el.testid.startswith("clear-text-"):
            mode_str = el.testid[len("clear-text-"):]
            self._click_clear_text(mode_str)
            return

        self.page.get_by_test_id(el.testid).first.click()

    def _click_add_property(self, el: ElementDescriptor, forced_name: str | None = None) -> None:
        parent_path = el.testid[len("add-property-"):]
        self._last_added_property_parent_path = ""
        self._last_added_property_name = ""
        self._last_added_property_path = ""

        # Mark every editable that exists BEFORE the click. The new name editor is then identified by
        # the absence of the mark, not by DOM position: property labels are contenteditable too, so
        # "the last editable in the table" can be an unrelated existing row. Old marks are cleared first.
        self.page.evaluate("""
            () => {
                document.querySelectorAll('[data-mc-pre]').forEach(e => e.removeAttribute('data-mc-pre'));
                document.querySelectorAll('tr [contenteditable="true"]')
                    .forEach(e => e.setAttribute('data-mc-pre', '1'));
            }
        """)
        clicked = self.page.evaluate("""
            (parentPath) => {
                const all = [...document.querySelectorAll('[data-testid^="add-property-"]')]
                    .filter(e => e.offsetParent !== null);
                // 1) The testid already carries the parent path (what discovery returns).
                const exact = all.find(e => e.getAttribute('data-testid') === 'add-property-' + parentPath);
                if (exact) { exact.click(); return 'exact'; }
                // 2) Empty-suffix control: parent = nearest previous row one aria-level up (same rule as discovery).
                for (const span of all.filter(e => e.getAttribute('data-testid') === 'add-property-')) {
                    const row = span.closest('tr');
                    if (!row) continue;
                    const parentLevel = parseInt(row.getAttribute('aria-level') || '1') - 1;
                    let prev = row.previousElementSibling;
                    while (prev) {
                        const label = prev.querySelector('[id^="_label_"]');
                        if (label && parseInt(prev.getAttribute('aria-level') || '1') === parentLevel) {
                            if (label.id.replace('_label_', '') === parentPath) { span.click(); return 'positional'; }
                            break;
                        }
                        prev = prev.previousElementSibling;
                    }
                }
                return all.map(e => e.getAttribute('data-testid')).slice(0, 12);
            }
        """, parent_path)
        if not isinstance(clicked, str):
            raise RuntimeError(f"add-property: control for '{parent_path}' not found; add-property ids in DOM: {clicked}")

        self.page.wait_for_timeout(400)

        new_name = forced_name if forced_name else self._distinct_property_name(parent_path)
        print(f"   [name-gen] path='{parent_path}' → '{new_name}'")

        try:
            self.page.wait_for_function(
                """() => [...document.querySelectorAll('tr [contenteditable="true"]')]
                         .some(e => !e.hasAttribute('data-mc-pre'))""",
                timeout=3000)
        except Exception:
            raise RuntimeError(f"add-property: no new name editor appeared under '{parent_path}'")

        editor = self.page.evaluate("""
            () => {
                const fresh = [...document.querySelectorAll('tr [contenteditable="true"]')]
                    .filter(e => !e.hasAttribute('data-mc-pre'));
                if (!fresh.length) return {n: 0, id: ''};
                const target = fresh[0];
                target.focus();
                document.execCommand('selectAll', false, null);
                return {n: fresh.length, id: target.id || ''};
            }
        """)
        if not editor or editor.get("n", 0) == 0:
            raise RuntimeError(f"add-property: the new name editor under '{parent_path}' disappeared before typing")
        if editor["n"] > 1:
            print(f"   [add-property] {editor['n']} new editables appeared; using the first (id={editor['id']!r})")
        self.page.wait_for_timeout(200)
        self.page.keyboard.type(new_name)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(300)
        self._dismiss_rename_conflict(wait_ms=1000)
        # Where the key really landed, read back from the tree's row labels. The control id names the schema that
        # receives the property (e.g. definitions.Complex); in the Schema Editor the key itself lands one level
        # deeper (definitions.Complex.properties.meadow). The shortest match is the new row, not a deeper namesake.
        landed = ""
        try:
            landed = self.page.wait_for_function(
                """([parent, name]) => [...document.querySelectorAll('[id^="_label_"]')]
                       .map(e => e.id.replace('_label_', ''))
                       .filter(p => p.startsWith(parent + '.') && p.endsWith('.' + name))
                       .sort((a, b) => a.length - b.length)[0] || ''""",
                arg=[parent_path, new_name], timeout=2000).json_value()
        except Exception:
            pass  # row label not rendered in time: log the name only
        print(f"   new property '{new_name}' added at {landed}" if landed else f"   new property named '{new_name}'")
        self._last_added_property_parent_path = parent_path
        self._last_added_property_name = new_name
        self._last_added_property_path = landed

    def _select(self, el: ElementDescriptor, value: Any = None) -> None:
        self._dismiss_rename_conflict()
        if el.testid == "format-selector":
            self._select_format(value)
        elif el.testid.startswith("input-select-"):
            self._select_dropdown(el, value)
        else:
            self.page.get_by_test_id(el.testid).first.click()

    def _select_dropdown(self, el: ElementDescriptor, value: Any = None) -> None:
        container   = self.page.get_by_test_id(el.testid).first
        spinbutton  = container.get_by_role("spinbutton")
        selectbutton= container.locator(".p-selectbutton")
        combobox    = container.get_by_role("combobox")

        if spinbutton.count() > 0:
            prop_schema = self._get_prop_schema(el.testid)
            self._fill_number(el, spinbutton, prop_schema)
        elif selectbutton.count() > 0:
            self._fill_boolean(selectbutton)
        elif combobox.count() > 0:
            self._fill_enum(combobox)
        else:
            tag = self.page.get_by_test_id(el.testid).first.evaluate("el => el.tagName.toLowerCase()")
            if tag == "input":
                self._fill_string(el, self.page.get_by_test_id(el.testid).first, value)
            else:
                textbox = container.get_by_role("textbox")
                if textbox.count() > 0:
                    self._fill_string(el, textbox, value)
                else:
                    self.page.keyboard.press("Escape")

    def _fill_number(self, el: ElementDescriptor, spinbutton, prop_schema: dict = None) -> None:
        prop_schema = prop_schema or {}
        minimum = prop_schema.get("minimum", 0)
        maximum = prop_schema.get("maximum", 100)
        multiple_of = prop_schema.get("multipleOf")
        is_integer = prop_schema.get("type") == "integer"
        use_valid = roll_valid()

        if not use_valid:
            invalid_choices = [
                maximum + 100,
                minimum - 100,
                round(random.uniform(maximum, maximum * 2), 2),
            ]
            if multiple_of:
                invalid_choices.append(multiple_of * random.randint(1, 10) + 0.5)
            value = random.choice(invalid_choices)
        elif multiple_of:
            min_mult = int(minimum / multiple_of) + (1 if minimum % multiple_of else 0)
            max_mult = int(maximum / multiple_of)
            value = random.randint(min_mult, max_mult) * multiple_of if min_mult <= max_mult else multiple_of
        elif is_integer:
            value = random.randint(int(minimum), int(maximum))
        else:
            value = round(random.uniform(minimum, maximum), 2)

        try:
            current = spinbutton.input_value(timeout=2000)
            print(f"   field '{el.testid}': '{current}' → '{value}'")
        except Exception:
            print(f"   field '{el.testid}': (unreadable) → '{value}'")
        spinbutton.click()
        spinbutton.fill(str(value))
        spinbutton.press("Enter")

    def _fill_boolean(self, selectbutton) -> None:
        choice = random.choice(["true", "false"])
        print(f"   boolean → '{choice}'")
        btn = selectbutton.get_by_role("button", name=choice)
        if btn.count() > 0:
            btn.first.click()

    def _fill_enum(self, combobox) -> None:
        combobox.first.click()
        self.page.wait_for_timeout(300)
        options = self.page.locator(".p-select-list .p-select-option")
        count = options.count()
        if count > 0:
            options.nth(random.randint(0, count - 1)).click()
        else:
            self.page.keyboard.press("Escape")

    def _fill_string(self, el: ElementDescriptor, textbox, value: Any) -> None:
        try:
            current = textbox.input_value(timeout=2000)
            print(f"   field '{el.testid}': '{current}' → '{value}'")
        except Exception:
            print(f"   field '{el.testid}': (unreadable) → '{value}'")

        textbox.click()
        textbox.press("Control+a")
        textbox.fill(str(value) if value is not None else "")
        textbox.press("Tab")
        self.page.wait_for_timeout(300)

        try:
            after = textbox.input_value(timeout=1000)
            if after != str(value):
                print("   [fill-retry] Tab didn't commit — trying Enter")
                textbox.click()
                textbox.press("Control+a")
                textbox.fill(str(value) if value is not None else "")
                textbox.press("Enter")
                self.page.wait_for_timeout(300)
        except Exception:
            pass

    def _select_format(self, format_value: Any = None) -> None:
        selector = self.page.get_by_test_id("format-selector")
        try:
            current = selector.inner_text().lower().strip()
            for fmt in _FORMAT_OPTIONS:
                if fmt in current:
                    current = fmt
                    break
            else:
                current = "json"
        except Exception:
            current = "json"

        if format_value is None:
            choices = [f for f in _FORMAT_OPTIONS if f != current]
            format_value = random.choice(choices)

        if format_value == current:
            return

        selector.get_by_role("combobox").click()
        self.page.get_by_role("option", name=format_value).click()
        self.page.wait_for_timeout(300)