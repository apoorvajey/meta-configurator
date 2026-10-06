"""Screenshots of recorded steps."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from tests.mutation_testing.triage.run_loading import Run


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _shot_folders(run: Run, cfg: dict) -> list[Path]:
    return [run.path.parent, Path(cfg["screenshots"]["root"]) / run.run_id]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def get_shots(run: Run, cfg: dict) -> dict[int, list[tuple[str, Path]]]:
    """Index screenshots by their step counter N, and compute N for every log step. N counts the
    main ('dynamic:') actions; the tool's own undo/retry entries share the N of the action that
    triggered them.
    """
    if run.shots is not None:
        return run.shots
    sc = cfg["screenshots"]
    rx = re.compile(sc["filename_regex"])
    idx: dict[int, list[tuple[str, Path]]] = defaultdict(list)
    seen: set[Path] = set()
    if sc.get("enabled"):
        for folder in _shot_folders(run, cfg):
            if not folder.is_dir():
                continue
            for pattern in ("*", "*/*"):
                for p in sorted(folder.glob(pattern)):
                    if p.suffix.lower() not in IMAGE_EXTS or not p.is_file() or p in seen:
                        continue
                    seen.add(p)
                    m = rx.match(p.stem)
                    if m:
                        idx[int(m.group("n"))].append((m.group("name"), p))
    count, iters = 0, []
    for st in run.steps:
        if str(st.get("action_id", "")).startswith("dynamic:"):
            count += 1
        iters.append(count)
    run.shots, run.shot_iters = dict(idx), iters
    run.shot_image_count = len(seen)
    run.shot_examples = [p.name for p in sorted(seen)[:3]]
    return run.shots


def shots_for_step(run: Run, i: int, cfg: dict) -> list[Path]:
    """Screenshots whose counter and action name both match log step i."""
    get_shots(run, cfg)
    step = run.steps[i]
    cands = (run.shots or {}).get(run.shot_iters[i], [])
    name = re.sub(r"^(dynamic|repair):", "", str(step.get("action_id", ""))).lower()
    kind = run.kinds[i]
    if kind == "auto_undo":
        hits = [p for f, p in cands if f.lower() == f"autoundo_{name}"]
    elif kind == "walkback":
        block_start = i
        while block_start > 0 and run.kinds[block_start - 1] == "walkback":
            block_start -= 1
        ordinal = sum(1 for k in range(block_start, i + 1) if run.steps[k].get("action_id") == step.get("action_id"))
        rep_files = sorted(((int(m.group(1)), p) for f, p in cands
                            for m in [re.fullmatch(r"repair(\d+)_" + re.escape(name), f.lower())] if m))
        hits = [p for _, p in rep_files]
        hits = [hits[ordinal - 1]] if len(hits) >= ordinal else hits[:1]
    else:
        tid = _norm(str(step.get("testid") or name))
        hits = [p for f, p in cands
                if not f.lower().startswith(("autoundo_", "repair")) and _norm(f) and tid.startswith(_norm(f))]
    return hits[:cfg["screenshots"]["max_per_step"]]
