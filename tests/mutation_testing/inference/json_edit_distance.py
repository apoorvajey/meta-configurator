"""Edit distance between two JSON values, in the spirit of JEDI (Hütter et al., SIGMOD 2022, https://arxiv.org/abs/2201.08099).

NOT the authors' algorithm. JEDI is the minimum number of node inserts, deletes and renames over all mappings that keep
the document structure; its algorithm (QuickJEDI) is not reimplemented here. This module computes a TOP-DOWN variant, in which
a node can only be matched to a node at the same position of the tree (the roots are matched, the children of matched nodes are
matched, and so on). Every top-down mapping is an allowed JEDI mapping, so the result is an UPPER BOUND on the JEDI distance;
the two agree on the edits this project cares about (one value changed, a key added, removed or renamed, an item appended).

Tree model: an object is a node with one child per key; the key node carries the key and has the value as its only child.
An array is a node with its items as ordered children; a scalar is a leaf. Object children are unordered, array children are
ordered. Costs, all 1 per node: delete a node, insert a node, rename a node (a key, or a scalar to another value or type).

    {"a": 1} -> {"a": 2}                       1   (the value is renamed)
    {"a": 1} -> {"b": 1}                       1   (the key is renamed)
    {"a": 1} -> {"a": 1, "b": 2}               2   (a key node and its value are inserted)
    [1, 2]   -> [1, 2, 3]                      1   (an item is inserted)
    "x"      -> [1, 2]                         3   (the root is renamed, two nodes are inserted)

edit_distance() returns None when the work limit is exceeded, so a very different pair of large documents costs a bounded time and
simply gives no observation. The limit is a parameter (inference_config.json: edit_distance_work_limit), not a constant here.
Subtrees that are equal are skipped without looking inside, so the cost follows the part that changed.
"""
from __future__ import annotations


class _OverBudget(Exception):
    pass


class _Budget:
    def __init__(self, limit: int) -> None:
        self.left = limit

    def spend(self, units: int = 1) -> None:
        self.left -= units
        if self.left < 0:
            raise _OverBudget


def json_type(value) -> str:
    """The JSON type of a Python value: null, boolean, number (int and float alike), string, array or object."""
    if value is None:
        return "null"
    if isinstance(value, bool):  # before int: True is an int in Python
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def tree_size(value) -> int:
    """Number of nodes of the tree model above."""
    if isinstance(value, dict):
        return 1 + sum(1 + tree_size(v) for v in value.values())
    if isinstance(value, list):
        return 1 + sum(tree_size(v) for v in value)
    return 1


def _strict_equal(a, b) -> bool:
    """Equal, and with the same JSON type everywhere (Python's == says 1 == True)."""
    if a != b:
        return False
    ta = json_type(a)
    if ta != json_type(b):
        return False
    if ta == "object":
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    if ta == "array":
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    return True


def _assign(cost: list[list[int]]) -> int:
    """Cost of the cheapest assignment of rows to columns of a square integer matrix (Hungarian method, O(n^3))."""
    n = len(cost)
    inf = float("inf")
    u, v, match, way = [0] * (n + 1), [0] * (n + 1), [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        match[0] = i
        j0 = 0
        min_to = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = match[j0], inf, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < min_to[j]:
                        min_to[j], way[j] = cur, j0
                    if min_to[j] < delta:
                        delta, j1 = min_to[j], j
            for j in range(n + 1):
                if used[j]:
                    u[match[j]] += delta
                    v[j] -= delta
                else:
                    min_to[j] -= delta
            j0 = j1
            if match[j0] == 0:
                break
        while True:
            j1 = way[j0]
            match[j0] = match[j1]
            j0 = j1
            if j0 == 0:
                break
    return int(-v[0])


def _dist(a, b, budget: _Budget) -> int:
    budget.spend()
    if _strict_equal(a, b):
        return 0
    ta, tb = json_type(a), json_type(b)
    container_a, container_b = ta in ("object", "array"), tb in ("object", "array")
    if not container_a and not container_b:
        return 1  # a scalar renamed to another value or type
    if ta != tb:
        return 1 + (tree_size(a) - 1) + (tree_size(b) - 1)  # the root is renamed, nothing below it can be kept
    return _dist_object(a, b, budget) if ta == "object" else _dist_array(a, b, budget)


def _dist_object(a: dict, b: dict, budget: _Budget) -> int:
    total = sum(_dist(a[k], b[k], budget) for k in a if k in b)
    only_a = [k for k in a if k not in b]
    only_b = [k for k in b if k not in a]
    if not only_a:
        return total + sum(1 + tree_size(b[k]) for k in only_b)
    if not only_b:
        return total + sum(1 + tree_size(a[k]) for k in only_a)
    # Keys on both sides that match no key of the other: rename one into the other (1 + the distance of the values),
    # or delete / insert them. The cheapest combination is an assignment problem; dummy rows and columns stand for
    # "deleted" and "inserted".
    n, m = len(only_a), len(only_b)
    size = n + m
    budget.spend(size * size)
    cost = [[0] * size for _ in range(size)]
    for i in range(size):
        for j in range(size):
            if i < n and j < m:
                cost[i][j] = 1 + _dist(a[only_a[i]], b[only_b[j]], budget)
            elif i < n:
                cost[i][j] = 1 + tree_size(a[only_a[i]])
            elif j < m:
                cost[i][j] = 1 + tree_size(b[only_b[j]])
    return total + _assign(cost)


def _dist_array(a: list, b: list, budget: _Budget) -> int:
    lo = 0
    while lo < len(a) and lo < len(b) and _strict_equal(a[lo], b[lo]):
        lo += 1
    ha, hb = len(a), len(b)
    while ha > lo and hb > lo and _strict_equal(a[ha - 1], b[hb - 1]):
        ha, hb = ha - 1, hb - 1
    x, y = a[lo:ha], b[lo:hb]
    size_x, size_y = [tree_size(v) for v in x], [tree_size(v) for v in y]
    if not x or not y:
        return sum(size_x) + sum(size_y)
    budget.spend(len(x) * len(y))
    previous = [0]
    for s in size_y:
        previous.append(previous[-1] + s)
    for i, xi in enumerate(x):
        current = [previous[0] + size_x[i]]
        for j, yj in enumerate(y, 1):
            current.append(min(previous[j] + size_x[i],
                               current[j - 1] + size_y[j - 1],
                               previous[j - 1] + _dist(xi, yj, budget)))
        previous = current
    return previous[-1]


def edit_distance(a, b, work_limit: int) -> int | None:
    """Top-down JSON edit distance of a and b (see the module docstring), or None if more than work_limit units of work were needed."""
    try:
        return _dist(a, b, _Budget(work_limit))
    except _OverBudget:
        return None
