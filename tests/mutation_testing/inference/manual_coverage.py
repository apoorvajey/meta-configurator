"""Which of the hand-written invariants did the engine find on its own?

config/manual_invariants.json lists every hand-written invariant (tests/invariants) and, where a template can say the
same, the inferred rule that would. evaluate() looks each one up in the engine's candidates:

    recovered   every category of the invariant has a promotable rule that says it
    partial     only some categories do
    seen        a matching rule exists, but it is not promotable (too little data or below the threshold)
    missing     the traces have enough steps of that action, yet no matching rule exists
    no_evidence the traces have too few steps of that action in some dataset to test it at all
    not_expressible   no template can say it (the reason is in the file)

Recall = recovered / testable: of the hand-written invariants that a template can express and the traces can test, how
many the engine found. Precision is a question about the reviewed rules (how many were kept) and is computed on the review page.
"""
from __future__ import annotations

import json
from pathlib import Path

MANUAL_PATH = Path(__file__).parent.parent / "config" / "manual_invariants.json"
STATUSES = ("recovered", "partial", "seen", "missing", "no_evidence", "not_expressible")


def load_manual(path: Path = MANUAL_PATH) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["invariants"]


def _says(spec: dict, p) -> bool:
    """The candidate has the template and the parameters of the spec and is stated in the positive direction.
    A guarded candidate (params['guard']) says less than the hand-written invariant, so it never counts as recovering it."""
    return (p.template == spec["template"] and p.direction == "positive" and "guard" not in p.params
            and all(p.params.get(k) == v for k, v in spec["params"].items()))


def _in_category(wanted: str, category: str) -> bool:
    if wanted == "*" or "@" in wanted:
        return category == wanted
    return category.split("@")[0] == wanted


def _matching(entry: dict, promotions: list, category: str) -> list:
    specs = [{"template": entry["template"], "params": entry["params"]}, *entry.get("implied_by", [])]
    return [p for p in promotions if _in_category(category, p.category) and any(_says(s, p) for s in specs)]


def _has_evidence(category: str, sizes: dict[str, dict[str, int]], datasets: list[str], min_observations: int) -> bool:
    """True if every dataset has at least min_observations steps in a category that matches."""
    return all(any(n.get(ds, 0) >= min_observations for cat, n in sizes.items() if _in_category(category, cat))
               for ds in datasets)


def evaluate(promotions: list, sizes: dict[str, dict[str, int]], datasets: list[str], min_observations: int,
             entries: list[dict] | None = None) -> dict:
    """{'entries': [...], 'summary': {...}, 'tags': {rule id: [hand-written ids]}} for the engine's PromotionCandidates.
    sizes maps an action category to the number of steps it has per dataset (to tell 'missing' from 'no_evidence').
    """
    entries = entries if entries is not None else load_manual()
    tags: dict[str, list[str]] = {}
    out = []
    for e in entries:
        row = {k: e[k] for k in ("id", "name", "kind", "source")}
        if not e["expressible"]:
            out.append({**row, "status": "not_expressible", "reason": e["reason"], "rules": [], "categories": []})
            continue
        per_category, rules = [], []
        for category in e["categories"]:
            found = _matching(e, promotions, category)
            for p in found:
                tags.setdefault(p.rule["id"], [])
                if e["id"] not in tags[p.rule["id"]]:
                    tags[p.rule["id"]].append(e["id"])
            promotable = [p for p in found if p.verdict == "PROMOTABLE"]
            rules += [p.rule["id"] for p in promotable]
            if promotable:
                state = "recovered"
            elif found:
                state = "seen"
            else:
                state = "missing" if _has_evidence(category, sizes, datasets, min_observations) else "no_evidence"
            per_category.append({"category": category, "status": state, "verdicts": sorted({p.verdict for p in found})})
        states = [c["status"] for c in per_category]
        status = ("recovered" if all(s == "recovered" for s in states)
                  else "partial" if "recovered" in states
                  else "seen" if "seen" in states
                  else "no_evidence" if all(s == "no_evidence" for s in states) else "missing")
        out.append({**row, "status": status, "note": e.get("note", ""), "rules": sorted(set(rules)),
                    "categories": per_category})
    counts = {s: sum(1 for r in out if r["status"] == s) for s in STATUSES}
    testable = len(out) - counts["not_expressible"] - counts["no_evidence"]
    summary = {**counts, "total": len(out), "testable": testable,
               "recall": counts["recovered"] / testable if testable else 0.0,
               "recall_with_partial": (counts["recovered"] + counts["partial"]) / testable if testable else 0.0}
    return {"entries": out, "summary": summary, "tags": tags}