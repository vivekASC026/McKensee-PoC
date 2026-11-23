"""Features that only occur in certain combinations, drawn as those combinations.

A plan used to draw every coded column on its own. That is right for features
that vary independently and wrong for the ones that do not: a plan belongs to
one product, a provider has one specialty, a child is not married, some claim
types only carry some diagnoses. Drawn apart, those make records the book has
never held -- valid values in an invalid combination.

So the pattern store is measured. For every pair of coded features in one of
its tables, the question is how much of a population drawn independently would
land on pairs that never occur, counting only pairs common enough that their
absence means something. Pairs past a threshold depend on each other; pairs
that depend on each other are joined into groups. A diagnosis and a procedure
are always a group, because clinically they always are, whatever one store
shows.

A plan then draws each group's columns as one of the combinations the store
holds, weighted by how often each occurs, wherever the columns sit together --
one table, or a table and the parent its rows belong to. The parent's values
are drawn first and the child's are drawn from the combinations that agree
with them. Validation checks every group the plan draws.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from sdg.packs import held

# The store's tables and the element each coded column is. The store is this
# tool's own schema, so the names are fixed here rather than discovered.
STORE_ELEMENTS: dict[str, dict[str, str]] = held("pack:plan.store_elements")

def _schema() -> str:
    """The pattern store's schema, as configured."""
    from sdg.config import get_settings

    return get_settings().denorm_schema


# Always drawn together, whatever the store shows.
ALWAYS = {t: [tuple(p) for p in pairs] for t, pairs in held("pack:plan.always_together").items()}
# Columns whose emptiness is a value: an out-of-network claim names no network.
# Kept in a combination as empty rather than leaving the combination out.
EMPTY_IS_A_VALUE = set(held("pack:plan.empty_is_a_value"))

# A pair depends on each other when more than this share of an independent
# draw would land on pairs that never occur.
THRESHOLD = 0.05
# Only a pair this common under independence counts as missing: rarer ones
# are absent by chance as often as by rule.
MIN_EXPECTED = 5.0
# More values than this and a column is an identifier, not a code.
MAX_DISTINCT = 5_000
# The most common combinations a group keeps, with how often each occurs.
MAX_COMBINATIONS = 2_000


@dataclass
class Group:
    """Elements that occur only in certain combinations, and those combinations."""

    store: str
    elements: list[str]
    columns: list[str]
    combinations: list[tuple[tuple[str, ...], int]] = field(default_factory=list)
    # How strongly each dependent pair depends: the share of an independent
    # draw that would land on a pair the store never holds.
    evidence: list[tuple[str, str, float]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return "combo:" + "+".join(sorted(self.elements))

    def as_record(self) -> dict[str, Any]:
        return {
            "store": self.store, "elements": self.elements,
            "combinations": len(self.combinations),
            "evidence": [{"pair": [a, b], "unseen_if_independent": round(v, 4)}
                         for a, b, v in self.evidence],
        }


def _unseen(rows: list[dict[str, Any]]) -> float:
    """Share of an independent draw landing on pairs the rows never hold."""
    left: dict[Any, int] = {}
    right: dict[Any, int] = {}
    held: set[tuple[Any, Any]] = set()
    for row in rows:
        left[row["a"]] = left.get(row["a"], 0) + row["n"]
        right[row["b"]] = right.get(row["b"], 0) + row["n"]
        held.add((row["a"], row["b"]))
    total = float(sum(left.values())) or 1.0
    missing = 0.0
    for a, na in left.items():
        for b, nb in right.items():
            expected = na * nb / total
            if expected >= MIN_EXPECTED and (a, b) not in held:
                missing += expected
    return missing / total


# Measured once per store and size: a few seconds of aggregate queries, which
# every plan build and every pattern analysis would otherwise repeat.
_MEASURED: dict[tuple, list[Group]] = {}


def discover(registry) -> list[Group]:
    """The dependent groups the pattern store holds, with their combinations.

    None at all where there is no store, or it cannot be read: a plan then
    draws as it always did.
    """
    try:
        key = (str(registry.database_for("denorm")),) + tuple(
            int(registry.scalar("denorm", f"SELECT COUNT_BIG(*) FROM {_schema()}.{store}") or 0)
            for store in STORE_ELEMENTS)
        if key not in _MEASURED:
            _MEASURED[key] = _discover(registry)
        LAST_ERROR.pop("discover", None)
        return _MEASURED[key]
    except Exception as exc:
        # Drawn as before, without combinations -- but noted, so a file made
        # meanwhile records that the store could not be read.
        LAST_ERROR["discover"] = f"{type(exc).__name__}: {exc}"
        return []


def _discover(registry) -> list[Group]:
    groups: list[Group] = []
    for store, named in STORE_ELEMENTS.items():
        present = {r["COLUMN_NAME"] for r in registry.query(
            "denorm",
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", [_schema(), store])}
        columns = [c for c in named if c in present]
        distinct = {c: int(registry.scalar(
            "denorm", f"SELECT COUNT(DISTINCT {c}) FROM {_schema()}.{store}") or 0) for c in columns}
        columns = [c for c in columns if 1 < distinct[c] <= MAX_DISTINCT]
        element_of = {c: named[c] for c in columns}
        column_of = {e: c for c, e in element_of.items()}

        # Union-find over the dependent pairs.
        parent = {c: c for c in columns}

        def root(c: str) -> str:
            while parent[c] != c:
                parent[c] = parent[parent[c]]
                c = parent[c]
            return c

        found: list[tuple[str, str, float]] = []
        for a, b in itertools.combinations(columns, 2):
            rows = registry.query(
                "denorm",
                f"SELECT {_text(a)} AS a, {_text(b)} AS b, COUNT(*) AS n "
                f"FROM {_schema()}.{store} WHERE {_present(a)} AND {_present(b)} "
                f"GROUP BY {a}, {b}")
            share = _unseen(rows)
            if share > THRESHOLD:
                found.append((element_of[a], element_of[b], share))
                parent[root(a)] = root(b)
        for a, b in ALWAYS.get(store, []):
            if a in column_of and b in column_of:
                parent[root(column_of[a])] = root(column_of[b])

        members: dict[str, list[str]] = {}
        for c in columns:
            members.setdefault(root(c), []).append(c)
        for cols in members.values():
            if len(cols) < 2:
                continue
            cols = sorted(cols)
            elements = [element_of[c] for c in cols]
            listed = ", ".join(cols)
            stripped = ", ".join(f"{_text(c)} AS v{i}" for i, c in enumerate(cols))
            rows = registry.query(
                "denorm",
                f"SELECT {stripped}, COUNT(*) AS n "
                f"FROM {_schema()}.{store} WHERE "
                + " AND ".join(_present(c) for c in cols)
                + f" GROUP BY {listed} ORDER BY n DESC, {listed}")
            # An empty value that means something -- no network -- is kept as
            # None, so a generated row writes NULL there, not an empty string.
            every = [(tuple(None if c in EMPTY_IS_A_VALUE and r[f"v{i}"] == "" else str(r[f"v{i}"])
                            for i, c in enumerate(cols)), int(r["n"]))
                     for r in rows]
            combos = _covering(every, len(cols))
            groups.append(Group(
                store=store, elements=elements, columns=cols, combinations=combos,
                evidence=[e for e in found if e[0] in elements and e[1] in elements],
            ))
    return groups


def _text(column: str) -> str:
    inner = f"LTRIM(RTRIM(CAST({column} AS NVARCHAR(100))))"
    return f"ISNULL({inner}, '')" if column in EMPTY_IS_A_VALUE else inner


def _present(column: str) -> str:
    return "1 = 1" if column in EMPTY_IS_A_VALUE else f"{column} IS NOT NULL"


def _covering(every: list[tuple[tuple[str, ...], int]], width: int) -> list[tuple[tuple[str, ...], int]]:
    """The most common combinations, and the most common one holding each value.

    Keeping only the most common would drop rare values altogether -- a
    diagnosis that never makes the top two thousand could never be generated,
    or narrowed to. So every value of every feature keeps at least the
    commonest real combination it occurs in. `every` is most common first.
    """
    kept: dict[tuple[str, ...], int] = dict(every[:MAX_COMBINATIONS])
    covered: list[set[str]] = [set() for _ in range(width)]
    for values, _n in kept.items():
        for i, v in enumerate(values):
            covered[i].add(v)
    for values, n in every:
        if any(v not in covered[i] for i, v in enumerate(values)):
            kept[values] = n
            for i, v in enumerate(values):
                covered[i].add(v)
    weights = _raked(kept, every, width)
    return sorted(weights.items(),
                  key=lambda kv: (-kv[1], tuple("" if v is None else v for v in kv[0])))


# Rounds of re-weighting; each brings every feature's share closer to the store's.
RAKE_ROUNDS = 30


def _raked(kept: dict[tuple, int], every: list[tuple[tuple, int]],
           width: int) -> dict[tuple, int]:
    """The kept combinations, weighted so each feature's mix is the store's.

    Keeping the commonest combinations keeps the head of a long tail, and the
    tail is not spread evenly: out-of-network claims scatter over many rare
    combinations, so the kept ones held 1% of them where the store holds 13%.
    Re-weighting the kept combinations (iterative proportional fitting) until
    every feature's shares match the store's puts that back, without drawing
    a single combination the store does not hold.
    """
    total = float(sum(n for _, n in every)) or 1.0
    target: list[dict] = [{} for _ in range(width)]
    for values, n in every:
        for i, v in enumerate(values):
            target[i][v] = target[i].get(v, 0.0) + n / total
    weight = {values: float(n) for values, n in kept.items()}
    # Where not every share can be met at once, the feature fitted last is
    # met exactly. That is the one with fewest values -- claim type, in or out
    # of network -- whose mix a person reads first.
    order = sorted(range(width), key=lambda i: -len(target[i]))
    for _ in range(RAKE_ROUNDS):
        for i in order:
            held: dict = {}
            for values, w in weight.items():
                held[values[i]] = held.get(values[i], 0.0) + w
            whole = sum(held.values()) or 1.0
            for values in weight:
                share = held[values[i]] / whole
                if share > 0:
                    weight[values] *= target[i].get(values[i], share) / share
    scale = 1_000_000 / (sum(weight.values()) or 1.0)
    return {values: max(1, int(round(w * scale))) for values, w in weight.items()}


def held_combinations(registry, store: str, columns: list[str]) -> set[tuple[str, ...]]:
    """Every combination of these store columns the store holds, for checking."""
    # An empty network (out of network) is held as an empty string.
    stripped = ", ".join(f"{_text(c)} AS v{i}" for i, c in enumerate(columns))
    rows = registry.query(
        "denorm",
        f"SELECT DISTINCT {stripped} FROM {_schema()}.{store} WHERE "
        + " AND ".join(_present(c) for c in columns))
    return {tuple(str(r[f"v{i}"]) for i in range(len(columns))) for r in rows}


# The hierarchy a pattern is found in: its scope, not one of its combinations.
HIERARCHY = {"GROUP_KEY", "SUBGROUP_KEY", "COVERAGE_PLAN_ID", "PRODUCT_CODE"}

# The combinations a pattern keeps of each group, at most.
PATTERN_COMBINATIONS = 1000


def of_rows(frame, store: str, groups: list[Group]) -> list[dict[str, Any]]:
    """A pattern's own combinations of each group, from the rows it was found in.

    What a file or a load made from the pattern draws: the combinations its
    rows hold, with how often each occurs among them.
    """
    out: list[dict[str, Any]] = []
    if frame is None or len(frame) == 0:
        return out
    for group in groups:
        if group.store != store:
            continue
        # A pattern's group, plan and product are its scope, carried as such;
        # its combinations are of what varies inside that scope.
        pairs = [(c, e) for c, e in zip(group.columns, group.elements)
                 if c in frame.columns and e not in HIERARCHY]
        if len(pairs) < 2:
            continue
        columns = [c for c, _ in pairs]
        # An empty value is part of the combination, not a reason to drop the
        # row: an out-of-network line has no network, and is still a line.
        part = frame[columns].apply(
            lambda s: s.map(lambda v: "" if v is None or v != v else str(v).strip()))
        part = part[(part != "").any(axis=1)]
        if part.empty:
            continue
        counts = _spread(part, PATTERN_COMBINATIONS)
        total = float(counts.sum()) or 1.0
        out.append({
            "elements": [e for _, e in pairs],
            "records": [
                {**{e: (v or None) for (_, e), v in
                    zip(pairs, key if isinstance(key, tuple) else (key,))},
                 "share": round(float(n) / total, 6)}
                for key, n in counts.items()
            ],
        })
    return out


def _spread(part, cap: int):
    """How often each combination occurs, in at most `cap` combinations.

    All of them when there are few. When there are more, the most common alone
    would misstate the pattern -- the top few hundred diagnosis and procedure
    pairs of a big pattern are mostly one claim type -- so the rows are
    sampled evenly instead, every `n`th in combination order, and each value
    keeps about its share.
    """
    counts = part.value_counts()
    if len(counts) <= cap:
        return counts
    ordered = part.sort_values(list(part.columns)).reset_index(drop=True)
    step = len(ordered) / cap
    picked = ordered.iloc[[int((i + 0.5) * step) for i in range(cap)]]
    return picked.value_counts()


def for_patterns() -> list[Group]:
    """The store's groups, as the running service measured them."""
    try:
        from sdg.api.app import service

        found = service.combinations()
        LAST_ERROR.pop("for_patterns", None)
        return found
    except Exception as exc:
        LAST_ERROR["for_patterns"] = f"{type(exc).__name__}: {exc}"
        return []


# Why the last read of the store's combinations failed, while it does.
LAST_ERROR: dict[str, str] = {}


def signature() -> dict[str, Any]:
    """The combinations a file is drawn with, in plain form, and any reason the
    store could not be read: kept by digest, so a file written again that
    differs can say the combinations changed."""
    return {
        "groups": [[list(g.elements), [[list(v), n] for v, n in g.combinations]]
                   for g in for_patterns()],
        "unreadable": dict(sorted(LAST_ERROR.items())),
    }
