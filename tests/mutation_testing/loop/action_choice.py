"""Interactive action selection and pausing."""
from __future__ import annotations

import os
from tests.mutation_testing.discovery.element import ElementDescriptor, CATEGORY_LABELS
from tests.mutation_testing.runs.step_result import StepResult


class ActionChoiceMixin:
    """Menus for choosing or skipping an action by hand."""

    def _interactive_action_select(
            self, elements: list[ElementDescriptor], suggested: ElementDescriptor
    ) -> ElementDescriptor | None:
        """Pick a category, then an action within it.

        Step 1: [Enter] accept the suggestion, [1-N] pick a category, [a] flat list of all actions, [s]/[q] skip/quit.
        Step 2: [Enter] accept the suggestion if it is in this category, [1-N] pick an action, [b] back, [s]/[q] skip/quit.
        """
        while True:
            categories: dict[str, list[ElementDescriptor]] = {}
            for e in elements:
                categories.setdefault(e.category, []).append(e)
            sorted_category_keys = sorted(categories.keys())

            print(f"\n   Suggested: {suggested.human_label}  ({CATEGORY_LABELS.get(suggested.category, suggested.category)})")
            print(f"   [Enter] Accept suggested   [a] Show all {len(elements)} actions   [s] Skip   [q] Quit")
            print("   Categories:")
            for i, cat_key in enumerate(sorted_category_keys):
                label = CATEGORY_LABELS.get(cat_key, cat_key)
                count = len(categories[cat_key])
                marker = " *" if cat_key == suggested.category else ""
                print(f"     [{i+1:2}] {label} ({count}){marker}")

            try:
                choice = input("   > ").strip().lower()
            except EOFError:
                choice = ""

            if choice == "q":
                raise StopIteration
            elif choice == "s":
                print("   Step skipped.")
                return None
            elif choice == "":
                return suggested
            elif choice == "a":
                picked = self._select_from_flat_list(elements, suggested)
                if picked == "QUIT":
                    raise StopIteration
                if picked == "BACK":
                    continue  # re-show categories
                return picked  # None (skip) or an ElementDescriptor
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(sorted_category_keys):
                    cat_key = sorted_category_keys[idx]
                    picked = self._select_from_category(categories[cat_key], suggested, cat_key)
                    if picked == "BACK":
                        continue
                    if picked == "QUIT":
                        raise StopIteration
                    return picked  # None (skip) or an ElementDescriptor
                else:
                    print(f"   Please enter a number between 1 and {len(sorted_category_keys)}.")
            else:
                print("   Please enter a number, Enter, [a], [s], or [q].")

    def _select_from_category(
            self, category_elements: list[ElementDescriptor], suggested: ElementDescriptor, cat_key: str
    ) -> ElementDescriptor | None | str:
        """Step 2 of interactive selection — pick within one category.
        Returns an ElementDescriptor, None (skip), 'BACK', or 'QUIT'."""
        label = CATEGORY_LABELS.get(cat_key, cat_key)
        while True:
            print(f"\n   {label} ({len(category_elements)}):")
            for j, e in enumerate(category_elements):
                marker = " *" if e is suggested else ""
                print(f"     [{j+1:3}] {e.human_label}{marker}")
            print("   [Enter] Accept suggested   [b] Back to categories   [s] Skip   [q] Quit")

            try:
                choice = input("   > ").strip().lower()
            except EOFError:
                choice = ""

            if choice == "q":
                return "QUIT"
            elif choice == "b":
                return "BACK"
            elif choice == "s":
                print("   Step skipped.")
                return None
            elif choice == "":
                return suggested
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(category_elements):
                    chosen = category_elements[idx]
                    print(f"   Selected: {chosen.human_label}")
                    return chosen
                else:
                    print(f"   Please enter a number between 1 and {len(category_elements)}.")
            else:
                print("   Please enter a number, Enter, [b], [s], or [q].")

    def _select_from_flat_list(
            self, elements: list[ElementDescriptor], suggested: ElementDescriptor
    ) -> ElementDescriptor | None | str:
        """Flat list of all actions ([a] in the category menu)."""
        sorted_elements = sorted(elements, key=lambda e: e.category)
        print(f"\n   All {len(sorted_elements)} actions:")
        current_category = None
        for j, e in enumerate(sorted_elements):
            if e.category != current_category:
                print(f"\n   ── {CATEGORY_LABELS.get(e.category, e.category)}")
                current_category = e.category
            marker = " *" if e is suggested else ""
            print(f"     [{j+1:3}] {e.human_label}{marker}")
        print("\n   [Enter] Accept suggested   [b] Back to categories   [s] Skip   [q] Quit")

        while True:
            try:
                choice = input("   > ").strip().lower()
            except EOFError:
                choice = ""

            if choice == "q":
                return "QUIT"
            elif choice == "b":
                return "BACK"
            elif choice == "s":
                print("   Step skipped.")
                return None
            elif choice == "":
                return suggested
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(sorted_elements):
                    chosen = sorted_elements[idx]
                    print(f"   Selected: {chosen.human_label}")
                    return chosen
                else:
                    print(f"   Please enter a number between 1 and {len(sorted_elements)}.")
            else:
                print("   Please enter a number, Enter, [b], [s], or [q].")

    def _interactive_pause(self, step: int, total: int, result: StepResult):
        print(f"\n   ── Paused after step {step}/{total} ──")
        if result.report:
            print(f"   Mode: {result.report.mode}  |  {result.report.summary()}")
        if result.screenshot_path:
            print(f"   Screenshot: {os.path.abspath(result.screenshot_path)}")
        print("   Browser is open — inspect the app now.")
        print("   [Enter] Continue   [s] Skip next step   [q] Quit loop")
        while True:
            try:
                choice = input("   > ").strip().lower()
            except EOFError:
                choice = ""
            if choice == "q":
                print("   Stopping loop.")
                raise StopIteration
            elif choice == "s":
                self._skip_next = True
                print("   Next step will be skipped.")
                break
            else:
                break

    def _should_skip(self) -> bool:
        skip = self._skip_next
        self._skip_next = False
        return skip
