"""Find foreign keys nobody declared.

Facets-style schemas carry relationships that exist in the data but not in the
catalog. A column is proposed as a foreign key when its values are contained in
another table's key column.

Candidates are narrowed first, because an inclusion check costs a query. A pair
is only worth testing when the types line up and the names share a token that
is not a generic key suffix. Without that second rule every `*_ID` column in the
schema would be compared against every other one.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable

import pyodbc

from sdg import logsafe
from sdg.discovery.classify import tokenize
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import ForeignKey, PhysicalModel, Table
from sdg.profiling import sample

# Shared by nearly every key column, so a match on one of these alone says
# nothing about whether two columns are related.
GENERIC_TOKENS = {"ID", "CK", "CD", "NO", "NUM", "KEY", "CODE", "SK", "NBR", "SEQ"}

INCLUSION_THRESHOLD = 0.995
MIN_DISTINCT_VALUES = 2
MIN_PARENT_ROWS = 2
# A database of thousands of tables has thousands of pairs to check, so each
# check is kept short: at most this many of the child's distinct values, each
# looked up through the parent key's own index, several checks at a time.
CHECKED_VALUES = 5_000
CHECK_WORKERS = 8

log = logging.getLogger(__name__)

# The child's distinct values, each looked up in the parent. A large child is
# read through its fixed sample: a real key still finds nearly every value, a
# false one still misses most, and the parent side is a seek on its key.
INCLUSION_SQL = """
SELECT COUNT(*) AS distinct_values,
       SUM(CASE WHEN p.{parent_column} IS NULL THEN 1 ELSE 0 END) AS missing
FROM (
    SELECT DISTINCT TOP ({checked}) {child_column} AS candidate_value
    FROM {child_source}
    WHERE {child_column} IS NOT NULL
) AS c
LEFT JOIN {parent_table} AS p ON p.{parent_column} = c.candidate_value{collate}{hint}
"""


@dataclass
class Candidate:
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    shared_tokens: list[str] = field(default_factory=list)


@dataclass
class InferredKey:
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    coverage: float
    distinct_values: int
    missing: int
    shared_tokens: list[str]
    sampled: bool = False

    def evidence(self) -> list[str]:
        read = f", read from a sample of {self.child_table}" if self.sampled else ""
        return [
            f"{self.coverage:.1%} of {self.distinct_values:,} distinct values exist in "
            f"{self.parent_table}.{self.parent_column}{read}",
            f"names share {', '.join(self.shared_tokens)}",
        ]


def _comparable(a, b) -> bool:
    """Do two columns hold the same kind of value?"""
    if a.type_name != b.type_name:
        return False
    if a.is_character and a.character_length() != b.character_length():
        return False
    if a.type_name in {"decimal", "numeric"} and (a.precision, a.scale) != (b.precision, b.scale):
        return False
    return True


def _key_columns(table: Table) -> list[str]:
    """Single-column keys another table could point at."""
    keys = set(table.unique_single_columns())
    if len(table.primary_key) == 1:
        keys.add(table.primary_key[0])
    return sorted(keys)


def candidates(model: PhysicalModel) -> list[Candidate]:
    """Pairs worth spending a query on.

    Each table's key columns are filed under the tokens of their names, so a
    column is only set beside the keys it shares a token with -- not beside
    every key of every other table, which on thousands of tables is hundreds of
    millions of comparisons before a single query runs."""
    found: list[Candidate] = []
    declared: set[tuple[str, str, str]] = set()
    for table in model.tables:
        for foreign_key in table.foreign_keys:
            for column in foreign_key.columns:
                declared.add((table.name, column, foreign_key.parent_table))

    keys_by_token: dict[str, list[tuple]] = {}
    for order, parent in enumerate(model.tables):
        if parent.row_count < MIN_PARENT_ROWS:
            continue
        for parent_column_name in _key_columns(parent):
            parent_column = parent.column(parent_column_name)
            if parent_column is None:
                continue
            tokens = set(tokenize(parent_column_name)) - GENERIC_TOKENS
            for token in tokens:
                keys_by_token.setdefault(token, []).append(
                    (order, parent_column_name, parent, parent_column, tokens))

    for child in model.tables:
        for child_column in child.columns:
            if child_column.is_computed or child_column.is_identity:
                continue
            child_tokens = set(tokenize(child_column.name))
            near: dict[tuple[int, str], tuple] = {}
            for token in child_tokens - GENERIC_TOKENS:
                for entry in keys_by_token.get(token, ()):
                    near[(entry[0], entry[1])] = entry
            # In the model's table order, then by key name: the order the
            # pairs were always listed in.
            for _, parent_column_name, parent, parent_column, tokens in (
                near[k] for k in sorted(near)
            ):
                if parent.name == child.name:
                    continue
                if (child.name, child_column.name, parent.name) in declared:
                    continue
                if not _comparable(child_column, parent_column):
                    continue
                found.append(
                    Candidate(
                        child_table=child.name,
                        child_column=child_column.name,
                        parent_table=parent.name,
                        parent_column=parent_column_name,
                        shared_tokens=sorted(child_tokens & tokens),
                    )
                )
    return found


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _bracketed(table: Table | None, name: str) -> str:
    """A table's name as SQL reads it whatever it holds: a backup table named
    20140208_BKUP_..., say, is not a name SQL takes without brackets."""
    if table is not None:
        return sample.qualified(table)
    schema, _, rest = name.partition(".")
    return f"{_quote(schema)}.{_quote(rest)}" if rest else _quote(schema)


def test_candidate(
    registry: ConnectionRegistry, connection: str, candidate: Candidate,
    model: PhysicalModel | None = None,
) -> InferredKey | None:
    """One query: how many of the child's distinct values exist in the parent?"""
    child = model.table(candidate.child_table) if model is not None else None
    parent = model.table(candidate.parent_table) if model is not None else None
    sampled = child is not None and sample.is_sampled(child)
    seek = parent is not None and parent.leads_index(candidate.parent_column)
    sql = INCLUSION_SQL.format(
        checked=CHECKED_VALUES,
        child_source=sample.source(child) if sampled else _bracketed(child, candidate.child_table),
        child_column=_quote(candidate.child_column),
        parent_table=_bracketed(parent, candidate.parent_table),
        parent_column=_quote(candidate.parent_column),
        collate=sample.collate_as(
            parent.column(candidate.parent_column) if parent is not None else None,
            child.column(candidate.child_column) if child is not None else None,
        ),
        hint=" OPTION (LOOP JOIN)" if seek else "",
    )
    rows = registry.query(connection, sql)
    if not rows:
        return None
    distinct_values = int(rows[0]["distinct_values"] or 0)
    missing = int(rows[0]["missing"] or 0)
    if distinct_values < MIN_DISTINCT_VALUES:
        return None
    coverage = (distinct_values - missing) / distinct_values
    return InferredKey(
        child_table=candidate.child_table,
        child_column=candidate.child_column,
        parent_table=candidate.parent_table,
        parent_column=candidate.parent_column,
        coverage=coverage,
        distinct_values=distinct_values,
        missing=missing,
        shared_tokens=candidate.shared_tokens,
        sampled=sampled or distinct_values >= CHECKED_VALUES,
    )


def infer(
    registry: ConnectionRegistry,
    model: PhysicalModel,
    connection: str = "source",
    *,
    threshold: float = INCLUSION_THRESHOLD,
    untested: list[tuple[Candidate, str]] | None = None,
) -> list[InferredKey]:
    """Test every candidate and keep the ones above the inclusion threshold.

    A pair the database will not check -- a comparison it refuses, a table
    gone since the structure was read -- is left out and listed in
    `untested`, rather than ending the read of every other pair."""
    def check(candidate: Candidate) -> InferredKey | None:
        try:
            return test_candidate(registry, connection, candidate, model)
        except pyodbc.Error as exc:
            # The driver's words can quote a compared value: kind and code only.
            said = logsafe.summary(exc)
            log.warning("could not check %s.%s against %s.%s: %s",
                        candidate.child_table, candidate.child_column,
                        candidate.parent_table, candidate.parent_column, said)
            if untested is not None:
                untested.append((candidate, said))
            return None

    pairs = candidates(model)
    if len(pairs) > 1:
        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            results = list(pool.map(check, pairs))
    else:
        results = [check(c) for c in pairs]
    accepted = [r for r in results if r is not None and r.coverage >= threshold]
    return one_way(model, accepted)


def one_way(model: PhysicalModel, accepted: list[InferredKey]) -> list[InferredKey]:
    """Keep one direction of a key the data shows both ways.

    Two tables holding the same set of keys -- two copies of one member list,
    say -- each contain all of the other's values, so each looks like the
    other's parent. That is the same kind of thing twice, not a parent and a
    child, and taking both directions is a cycle no load can satisfy: neither
    table's rows can go in first. The larger table is kept as the parent, and
    between equals the one whose name sorts first, so a schema read twice reads
    the same way. A key the data suggests in the reverse of one the schema
    declares is dropped: the declaration says which way it runs.
    """
    def rows(name: str) -> int:
        table = model.table(name)
        return table.row_count if table is not None else 0

    declared = {
        (fk.parent_table, fk.parent_columns[0], table.name, fk.columns[0])
        for table in model.tables for fk in table.foreign_keys
        if fk.kind == "declared" and len(fk.columns) == 1 and fk.parent_columns
    }
    found = {(k.child_table, k.child_column, k.parent_table, k.parent_column) for k in accepted}
    kept: list[InferredKey] = []
    for key in accepted:
        # `declared` holds (parent, its column, child, its column); a declared
        # key running the other way has this key's child as its parent.
        if (key.child_table, key.child_column, key.parent_table, key.parent_column) in declared:
            continue
        reverse = (key.parent_table, key.parent_column, key.child_table, key.child_column)
        if reverse in found:
            # The parent should be the larger table; between equals, the first
            # by name. Drop this direction if it points the other way.
            parent_first = (-rows(key.parent_table), key.parent_table)
            child_first = (-rows(key.child_table), key.child_table)
            if child_first < parent_first:
                continue
        kept.append(key)
    return kept


def attach(model: PhysicalModel, inferred: Iterable[InferredKey]) -> PhysicalModel:
    """Add the accepted keys to the model as inferred relationships."""
    for key in inferred:
        table = model.table(key.child_table)
        if table is None:
            continue
        already = any(
            fk.columns == [key.child_column] and fk.parent_table == key.parent_table
            for fk in table.foreign_keys
        )
        if already:
            continue
        table.foreign_keys.append(
            ForeignKey(
                name=None,
                columns=[key.child_column],
                parent_table=key.parent_table,
                parent_columns=[key.parent_column],
                kind="inferred",
                coverage=key.coverage,
                evidence=key.evidence(),
            )
        )
    return model
