"""Interactive action selection and pausing."""
from __future__ import annotations

import os
from tests.mutation_testing.discovery.element import ElementDescriptor, CATEGORY_LABELS
from tests.mutation_testing.runs.step_result import StepResult


class ActionChoiceMixin:
    """Menus for choosing or skipping an action by hand."""

    PAGE_SIZE = 20          # rows shown per page
    GROUP_OVER = 25         # a category with more actions than this is split by section and location first

    def _interactive_action_select(
            self, elements: list[ElementDescriptor], suggested: ElementDescriptor
    ) -> ElementDescriptor | None:
        """Pick an action in at most three short steps instead of one list of thousands.

        Category -> (section and location, only for big categories) -> action.
        At every list: [Enter] accept the suggestion, [number] pick, [n]/[p] next/previous page,
        [/text] keep only rows containing all the words typed ([/] clears), [b] back, [s] skip, [q] quit.
        At the category list, [/text] searches all actions at once.
        """
        categories: dict[str, list[ElementDescriptor]] = {}
        for e in elements:
            categories.setdefault(e.category, []).append(e)
        keys = sorted(categories, key=lambda k: (int(k.split("_")[0]) if k.split("_")[0].isdigit() else 99, k))
        category_rows = [
            (f"{CATEGORY_LABELS.get(k, k)} ({len(categories[k])})" + (" *" if suggested in categories[k] else ""), k)
            for k in keys
        ]

        print(f"\n   Suggested: {suggested.human_label}  ({CATEGORY_LABELS.get(suggested.category, suggested.category)})")
        while True:
            picked = self._browse("Categories", category_rows, suggested, allow_back=False, search_all=True)
            if picked == "SEARCH":
                picked = self._browse(f"All {len(elements)} actions",
                                      [(f"{e.human_label}", e) for e in elements], suggested,
                                      query=self._last_query)
            elif isinstance(picked, str) and picked in categories:
                picked = self._select_from_category(categories[picked], suggested, picked)
            if picked == "BACK":
                continue
            if picked == "QUIT":
                raise StopIteration
            return picked  # None (skip) or an ElementDescriptor

    def _select_from_category(self, items: list[ElementDescriptor], suggested: ElementDescriptor, cat_key: str):
        """Pick within one category; big categories are split by section and location first.
        Returns an ElementDescriptor, None (skip), 'BACK' or 'QUIT'."""
        title = CATEGORY_LABELS.get(cat_key, cat_key)
        if len(items) <= self.GROUP_OVER:
            return self._browse(f"{title} ({len(items)})", [(e.human_label, e) for e in items], suggested)

        groups: dict[tuple[str, str], list[ElementDescriptor]] = {}
        for e in items:
            groups.setdefault((e.section, e.location), []).append(e)
        coarse = len(groups) > len(items) / 3       # mostly one action per place: group by section only
        if coarse:
            groups = {}
            for e in items:
                groups.setdefault((e.section, ""), []).append(e)
        group_keys = sorted(groups, key=lambda k: k[0])      # stable: keeps discovery order inside a section
        label_of = (lambda e: e.human_label) if coarse else (lambda e: e.choice_label)
        if len(group_keys) == 1:
            only = groups[group_keys[0]]
            return self._browse(f"{title} ({len(only)})", [(label_of(e), e) for e in only], suggested)
        group_rows = [
            (f"{section}{': ' + location if location else ''} ({len(groups[(section, location)])})"
             + (" *" if suggested in groups[(section, location)] else ""), (section, location))
            for section, location in group_keys
        ]
        while True:
            where = "places" if not coarse else "sections"
            picked = self._browse(f"{title}: {len(items)} actions across {len(group_keys)} {where}", group_rows, suggested)
            if not isinstance(picked, tuple):
                return picked
            section, location = picked
            members = groups[picked]
            crumb = f"{title} › {section}" + (f" › {location}" if location else "")
            result = self._browse(f"{crumb} ({len(members)})", [(label_of(e), e) for e in members], suggested)
            if result != "BACK":
                return result

    _last_query = ""

    def _browse(self, title: str, rows: list[tuple[str, object]], suggested: ElementDescriptor,
                allow_back: bool = True, search_all: bool = False, query: str = ""):
        """Paged, filterable list. Returns the payload of the chosen row, None (skip), 'BACK', 'QUIT',
        'SEARCH' (only with search_all; the typed text is left in self._last_query) or `suggested` on Enter."""
        page = 0
        while True:
            words = query.lower().split()       # every word must appear, in any order
            shown = [r for r in rows if all(w in r[0].lower() for w in words)] if words else rows
            pages = max(1, -(-len(shown) // self.PAGE_SIZE))
            page = min(page, pages - 1)
            print(f"\n   {title}:" if not query else f'\n   {title} — {len(shown)} matching "{query}":')
            if not shown:
                print("     (nothing matches)")
            for i in range(page * self.PAGE_SIZE, min(len(shown), (page + 1) * self.PAGE_SIZE)):
                text, payload = shown[i]
                marker = " *" if payload is suggested and not text.endswith(" *") else ""
                print(f"     [{i + 1:3}] {text}{marker}")
            nav = ["[Enter] Accept suggested"]
            if pages > 1:
                nav.append(f"[n]/[p] page {page + 1}/{pages}")
            nav.append("[/text] Search" if search_all else "[/text] Filter")
            if allow_back:
                nav.append("[b] Back")
            nav += ["[s] Skip", "[q] Quit"]
            print("   " + "   ".join(nav))

            try:
                choice = input("   > ").strip()
            except EOFError:
                choice = ""
            low = choice.lower()

            if low == "q":
                return "QUIT"
            if low == "b" and allow_back:
                return "BACK"
            if low == "s":
                print("   Step skipped.")
                return None
            if low == "":
                return suggested
            if low == "n":
                page = min(page + 1, pages - 1)
            elif low == "p":
                page = max(page - 1, 0)
            elif choice.startswith("/"):
                if search_all and choice[1:].strip():
                    self._last_query = choice[1:].strip()
                    return "SEARCH"
                query, page = choice[1:].strip(), 0
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(shown):
                    payload = shown[idx][1]
                    if isinstance(payload, ElementDescriptor):
                        print(f"   Selected: {payload.human_label}")
                    return payload
                print(f"   Please enter a number between 1 and {len(shown)}.")
            else:
                print("   Please enter a number, Enter, [n], [p], [/text], [b], [s], or [q].")

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