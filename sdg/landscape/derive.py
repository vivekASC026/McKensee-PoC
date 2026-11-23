"""Derive a landscape shape from the central store.

The store is a database like any other, so it is read the same way everything
else is: discover its tables, profile its columns, match them to elements. What
is new is the last step -- the profile is then pivoted from columns onto the
elements they carry, which is what makes it sayable about a target that shares
no column names with it.

Nothing here reads a target. A target supplies a schema and may hold no rows at
all; being told what the landscape looks like is the entire point.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.run import discover as run_discovery
from sdg.landscape.model import ElementShape, LandscapeShape, RelationshipShape
from sdg.mapping.scorer import map_columns
from sdg.profiling.run import profile as run_profiling

# Below this many distinct values a column is read as a code set and its values
# are carried. Above it the values are data rather than vocabulary, and a
# target has no business being given somebody's names.
CODED_AT_MOST = 60

def _q(name: str) -> str:
    """A column name as SQL Server reads it, whatever characters it holds."""
    return "[" + name.replace("]", "]]") + "]"


def _t(name: str) -> str:
    """A schema.table name, each part quoted."""
    schema, dot, table = name.partition(".")
    return f"{_q(schema)}.{_q(table)}" if dot else _q(name)


# Relationships that could not be measured, and why. Read by the caller
# rather than printed: a missing ratio changes what a plan can do.
_unmeasurable: list[str] = []


def unmeasurable() -> list[str]:
    return list(_unmeasurable)


def derive(
    *,
    source: str,
    catalog,
    registry: ConnectionRegistry | None = None,
    connection: str = "denorm",
    schema: str | None = None,
) -> LandscapeShape:
    """Read the store and describe what it holds, per element."""
    registry = registry or ConnectionRegistry()
    schema = schema or registry.settings.denorm_schema
    _unmeasurable.clear()

    # Asked for by name: discovery leaves the analyzer's schema out by default,
    # which is right when it is reading somebody's database and wrong when that
    # schema is the thing being read.
    model = run_discovery(
        registry, connection, include_schemas=(schema,), exclude_schemas=(),
        infer_keys=True,
    ).model
    # The store holds the tool's own tables beside the extracts. Only the
    # extracts describe a landscape; a table of saved patterns does not.
    model.tables = [
        t for t in model.tables
        if t.schema_name == schema and t.table_name.endswith("_denorm")
    ]
    if not model.tables:
        raise ValueError(
            f"no denormalized tables in {registry.database_for(connection)}. "
            f"Run an extract first."
        )

    report = run_profiling(registry, model, connection=connection, catalog=catalog)
    mappings = map_columns(model, catalog, report)

    element_of = {
        f"{m.table}.{m.column}": m.element
        for m in mappings.mappings if m.element
    }

    shape = LandscapeShape(
        source=source,
        derived_from=registry.database_for(connection),
        rows_seen=max((c.row_count for c in report.columns), default=0),
    )

    by_element: dict[str, list] = {}
    for column in report.columns:
        if column.row_count and column.null_count >= column.row_count:
            shape.empty.append(column.qualified)
            continue
        element = element_of.get(column.qualified)
        if element is None:
            shape.unmatched.append(column.qualified)
            continue
        by_element.setdefault(element, []).append(column)

    for element, columns in sorted(by_element.items()):
        merged = _merge(element, columns)
        known = catalog.element(element)
        if known is not None and getattr(known, "code_set", False):
            # The whole vocabulary, counted exactly -- not the profiler's top
            # twenty, which kept 20 of 30 diagnoses and none of 400 procedures.
            whole = _code_set(registry, connection, columns)
            if whole:
                merged.values = whole
        shape.elements.append(merged)

    shape.relationships = _relationships(report, element_of)
    shape.relationships.extend(
        _measured(registry, connection, report, element_of, shape.relationships)
    )
    shape.relationships.extend(
        _nested(registry, connection, report, element_of, shape.relationships)
    )
    shape.sums = _sums(registry, connection, model, report, element_of)
    shape.line_elements = _line_elements(registry, connection, report, element_of)
    return shape


def _line_elements(registry, connection: str, report, element_of: dict[str, str]) -> list[str]:
    """Elements whose value changes between the lines of one child record.

    Read in the extract that holds the lines: grouped by the child record's
    id (the pack's anchor: health's CLAIM_ID, a bank's ACCOUNT_ID), an
    element whose column takes more than one value within a record belongs
    to the line. Everything else a record carries once.
    """
    from sdg import packs

    record_id = packs.active().child_anchors()[0] or "CLAIM_ID"
    found: set[str] = set()
    by_table: dict[str, list] = {}
    for column in report.columns:
        if element_of.get(column.qualified):
            by_table.setdefault(column.table, []).append(column)
    for table, columns in by_table.items():
        claim_keys = [c for c in columns if element_of[c.qualified] == record_id]
        if not claim_keys:
            continue
        key = max(claim_keys, key=lambda c: c.distinct_count).column
        for column in columns:
            element = element_of[column.qualified]
            if column.column == key or element in found:
                continue
            sql = (f"SELECT TOP 1 1 FROM {_t(table)} WHERE {_q(key)} IS NOT NULL "
                   f"GROUP BY {_q(key)} HAVING COUNT(DISTINCT {_q(column.column)}) > 1")
            try:
                with registry.connect(connection) as conn:
                    if conn.cursor().execute(sql).fetchone() is not None:
                        found.add(element)
            except Exception as exc:
                _unmeasurable.append(f"{column.qualified} within a claim: {type(exc).__name__}")
    return sorted(found)


# A total is a sum when it equals its parts for at least this share of groups.
SUM_MATCH = 0.99
_AMOUNT_TYPES = {"decimal", "numeric", "money", "smallmoney", "float", "real"}


def _sums(registry, connection: str, model, report, element_of: dict[str, str]):
    """Totals that are their parts added up, inside each flat extract.

    A claim's total and each of its lines' charges sit on every line row. Where
    the total is the same on every row of a key and equals the sum of the part
    over those rows, the total is a sum -- which an empty target cannot show
    and must not generate as a number of its own.
    """
    from sdg.landscape.model import SumShape

    found = []
    for table in model.tables:
        amounts = [c.name for c in table.columns
                   if c.type_name in _AMOUNT_TYPES and element_of.get(f"{table.name}.{c.name}")]
        if len(amounts) < 2:
            continue
        keys = [c for c in report.columns
                if c.table == table.name and c.distinct_count >= KEY_DISTINCT
                and _is_key(element_of.get(c.qualified) or "")]
        for key in keys:
            for total in amounts:
                for part in amounts:
                    if total == part:
                        continue
                    # Counted over every group. Checking only the groups where
                    # the total happens not to vary finds "sums" in single-line
                    # claims, where anything equals itself.
                    sql = f"""
                        SELECT COUNT(*) AS groups,
                               SUM(CASE WHEN t_hi = t_lo AND ABS(t_hi - s) < 0.005
                                        THEN 1 ELSE 0 END) AS matched
                        FROM (
                            SELECT {_q(key.column)} AS k, MAX({_q(total)}) AS t_hi,
                                   MIN({_q(total)}) AS t_lo, SUM({_q(part)}) AS s
                            FROM {_t(table.name)}
                            WHERE {_q(key.column)} IS NOT NULL
                            GROUP BY {_q(key.column)}
                        ) AS g
                    """
                    try:
                        with registry.connect(connection) as conn:
                            row = conn.cursor().execute(sql).fetchone()
                    except Exception as exc:
                        _unmeasurable.append(
                            f"{table.name} {total} as sum of {part}: {type(exc).__name__}")
                        continue
                    groups, matched = int(row.groups or 0), int(row.matched or 0)
                    if not groups or matched / groups < SUM_MATCH:
                        continue
                    found.append(SumShape(
                        total_element=element_of[f"{table.name}.{total}"],
                        part_element=element_of[f"{table.name}.{part}"],
                        key_element=element_of[key.qualified],
                        observed_in=[f"{table.name}: {total} = sum({part}) per {key.column}"],
                        groups=groups, matched=matched,
                    ))
    return found


def _merge(element: str, columns: list) -> ElementShape:
    """One element, from every column that carries it.

    The widest reading wins. Two columns holding the same element describe one
    thing seen twice, and a target asking what it looks like should be told
    about the whole of it rather than whichever column came first.
    """
    rows = sum(c.row_count for c in columns)
    weighted = rows or 1

    merged = ElementShape(
        element=element,
        observed_in=sorted(c.qualified for c in columns),
        rows=rows,
        null_rate=round(
            sum(c.null_rate * max(c.row_count, 1) for c in columns) / weighted, 6
        ),
        distinct_ratio=round(max((c.distinct_ratio for c in columns), default=0.0), 6),
        cardinality=_widest([c.cardinality for c in columns]),
        minimum=_extreme([c.minimum for c in columns], low=True),
        maximum=_extreme([c.maximum for c in columns], low=False),
        min_length=_extreme([c.min_length for c in columns], low=True),
        max_length=_extreme([c.max_length for c in columns], low=False),
        has_time_component=any(c.has_time_component for c in columns) or None,
    )

    means = [(c.mean, c.row_count) for c in columns if c.mean is not None]
    if means:
        total = sum(n for _, n in means) or 1
        merged.mean = round(sum(m * n for m, n in means) / total, 6)
    spreads = [c.stddev for c in columns if c.stddev is not None]
    if spreads:
        merged.stddev = round(max(spreads), 6)

    # A format every column carrying this element agrees on is a property of
    # the element. One that only some agree on is a property of a column.
    formats = {c.format_regex() for c in columns}
    if len(formats) == 1 and None not in formats:
        merged.format_regex = formats.pop()

    distributions = [c.distribution for c in columns if c.distribution]
    if distributions:
        merged.distribution = distributions[0]

    values = _values(columns)
    if values:
        merged.values = values
    return merged


# Above this many distinct values an element is not read as a vocabulary even
# when the catalog marks it one; that is a column mapped to the wrong element.
CODE_SET_AT_MOST = 20_000


def _code_set(registry, connection: str, columns: list) -> list[dict[str, Any]] | None:
    """Every value of a code set across the columns carrying it, with its share."""
    pooled: dict[str, int] = {}
    for column in columns:
        sql = (f"SELECT {_q(column.column)} AS v, COUNT(*) AS n FROM {_t(column.table)} "
               f"WHERE {_q(column.column)} IS NOT NULL GROUP BY {_q(column.column)}")
        try:
            with registry.connect(connection) as conn:
                rows = conn.cursor().execute(sql).fetchall()
        except Exception as exc:
            _unmeasurable.append(f"{column.qualified} code set: {type(exc).__name__}: {str(exc)[:160]}")
            return None
        for value, count in rows:
            text = str(value).strip()
            if text:
                pooled[text] = pooled.get(text, 0) + int(count)
        if len(pooled) > CODE_SET_AT_MOST:
            return None
    if not pooled:
        return None
    total = sum(pooled.values())
    return [
        {"value": value, "weight": round(count / total, 8)}
        for value, count in sorted(pooled.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _values(columns: list) -> list[dict[str, Any]] | None:
    """The value set, where the element is a vocabulary rather than data."""
    pooled: dict[str, int] = {}
    for column in columns:
        if column.distinct_count > CODED_AT_MOST or not column.top_values:
            return None
        for top in column.top_values:
            if top.value is None:
                continue
            pooled[str(top.value)] = pooled.get(str(top.value), 0) + int(top.count or 0)
    if not pooled:
        return None
    total = sum(pooled.values()) or 1
    return [
        {"value": value, "weight": round(count / total, 6)}
        for value, count in sorted(pooled.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _relationships(report, element_of: dict[str, str]) -> list[RelationshipShape]:
    """Children per parent, between the elements the two keys carry."""
    out: list[RelationshipShape] = []
    for ratio in report.ratios:
        child = element_of.get(f"{ratio.child_table}.{ratio.child_column}")
        parent = element_of.get(f"{ratio.parent_table}.{ratio.parent_column}")
        if not child or not parent:
            # A relationship whose keys carry no element cannot be said about
            # any other database, so it is left out rather than guessed at.
            continue
        out.append(RelationshipShape(
            child_element=child,
            parent_element=parent,
            child_table=ratio.child_table,
            parent_table=ratio.parent_table,
            observed_in=[f"{ratio.child_table} -> {ratio.parent_table}"],
            parents=ratio.parents,
            children=ratio.children,
            minimum=ratio.minimum,
            maximum=ratio.maximum,
            mean=ratio.mean,
            stddev=ratio.stddev,
            median=ratio.median,
            p90=ratio.p90,
            p99=ratio.p99,
            histogram=_plain_bins(getattr(ratio, "histogram", None)),
        ))
    return out


def _plain_bins(bins) -> list[dict] | None:
    """A ratio's histogram as plain records, however the profile held them."""
    if not bins:
        return None
    return [b.model_dump() if hasattr(b, "model_dump") else dict(b) for b in bins]


def _measured(
    registry, connection: str, report, element_of: dict[str, str],
    already: list[RelationshipShape],
) -> list[RelationshipShape]:
    """Count children per parent where the keys say there is a relationship.

    Extracts are flat and carry no keys between them, so nothing can be
    inferred from the schema. The relationship is in the data: a column unique
    in one table and repeating in another is a parent key seen from both ends,
    and the repeats are the ratio. This is how "claim lines per member" survives
    being denormalized into two tables that declare nothing about each other.

    Both ends must carry an element, because a ratio between two columns is a
    fact about one database and a ratio between two elements is a fact about
    the landscape.
    """
    seen = {(r.child_element, r.parent_element) for r in already}
    by_column: dict[str, list] = {}
    for column in report.columns:
        if element_of.get(column.qualified):
            by_column.setdefault(column.column, []).append(column)

    out: list[RelationshipShape] = []
    for name, columns in sorted(by_column.items()):
        if len(columns) < 2:
            continue
        parents = [c for c in columns if c.cardinality == "unique"]
        children = [c for c in columns if c.cardinality != "unique"]
        if not parents or not children:
            continue

        parent = parents[0]
        for child in children:
            element = element_of[parent.qualified]
            if (element, element) in seen:
                continue
            measured = _count_children(
                registry, connection, child.table, name, parent.table
            )
            if measured is None:
                continue
            out.append(RelationshipShape(
                child_element=element,
                parent_element=element,
                child_table=child.table,
                parent_table=parent.table,
                observed_in=[f"{child.table} -> {parent.table} on {name}"],
                **measured,
            ))
            seen.add((element, element))
    return out


# A column is a key of something when its element names an identity -- the
# catalog's MEMBER_KEY, CLAIM_ID, SUBSCRIBER_KEY -- and it has at least this
# many values. Below it a column is a code: a group of eight is a lookup, not a
# population. Dates, places and amounts repeat too, but "members per date of
# birth" is not a relationship any database has.
KEY_DISTINCT = 100
KEY_SUFFIXES = ("_KEY", "_ID")


def _is_key(element: str) -> bool:
    return element.upper().endswith(KEY_SUFFIXES)


def _nested(
    registry, connection: str, report, element_of: dict[str, str],
    already: list[RelationshipShape],
) -> list[RelationshipShape]:
    """Which thing per which parent, read from inside each flat extract.

    An extract is flat: a member's subscriber, a claim's member and a line's
    claim all sit on one row. The hierarchy is still there -- each claim id
    has exactly one member, each member exactly one subscriber -- and the
    number of distinct children under each parent is the ratio a target's key
    needs: claims per member, members per subscriber.

    Lines are the other case. A line number is not a key of anything on its
    own -- every claim has a line 1 -- but with its claim it names one row.
    Rows per claim is lines per claim.

    Each relationship is keyed by the child thing and the parent key: CLAIM_ID
    per MEMBER_KEY, LINE_NUMBER per CLAIM_ID. Where one is measured in two
    extracts the reading with more children wins, because members per
    subscriber counted among members who have claims undercounts.
    """
    seen = {(r.child_element, r.parent_element) for r in already}
    found: dict[tuple[str, str], RelationshipShape] = {}

    by_table: dict[str, list] = {}
    for column in report.columns:
        if element_of.get(column.qualified):
            by_table.setdefault(column.table, []).append(column)

    for table, columns in sorted(by_table.items()):
        keys = [c for c in columns
                if c.distinct_count >= KEY_DISTINCT and _is_key(element_of[c.qualified])]
        rows = max((c.row_count for c in columns), default=0)

        for child in keys:
            for parent in keys:
                if parent is child or child.distinct_count <= parent.distinct_count:
                    continue
                pair = (element_of[child.qualified], element_of[parent.qualified])
                if pair[0] == pair[1] or pair in seen:
                    continue
                if not _nests(registry, connection, table, child.column, parent.column):
                    continue
                measured = _count_children(
                    registry, connection, table, parent.column, table,
                    child_key=child.column,
                )
                _keep(found, pair, table, child.column, parent.column, measured)

        # Sequences within a parent: small integers starting at 1 that, with a
        # repeating key, name one row.
        for sequence in columns:
            if sequence.distinct_count >= KEY_DISTINCT or str(sequence.minimum) not in ("1", "1.0"):
                continue
            for parent in keys:
                if parent.distinct_count >= rows:
                    continue
                pair = (element_of[sequence.qualified], element_of[parent.qualified])
                if pair[0] == pair[1] or pair in seen:
                    continue
                if not _names_a_row(registry, connection, table, parent.column, sequence.column):
                    continue
                measured = _count_children(registry, connection, table, parent.column, table)
                _keep(found, pair, table, sequence.column, parent.column, measured)

    # A parent with no children appears in no row of the extract that holds
    # the children: a member with no claims is not in the claims extract. Where
    # another extract holds every parent, the ones missing are zeros, and a
    # book where three in ten members claim nothing must say so.
    # Which extract holds the most of each key -- read from the profile, whose
    # distinct counts are estimates -- and then counted exactly there. An
    # estimate is fine for choosing where to look and wrong for saying how
    # many parents have nothing: 5,000 too many claims read as 2% with no
    # lines.
    home: dict[str, Any] = {}
    for column in report.columns:
        element = element_of.get(column.qualified)
        if element and (element not in home or column.distinct_count > home[element].distinct_count):
            home[element] = column
    for shape in found.values():
        holder = home.get(shape.parent_element)
        if holder is None or holder.table == shape.child_table:
            continue
        exact = _distinct(registry, connection, holder.table, holder.column)
        if exact:
            _with_zeros(shape, exact)

    return list(found.values())


def _distinct(registry, connection: str, table: str, column: str) -> int:
    try:
        with registry.connect(connection) as conn:
            return int(conn.cursor().execute(
                f"SELECT COUNT(DISTINCT {_q(column)}) FROM {_t(table)}").fetchone()[0])
    except Exception as exc:
        _unmeasurable.append(f"{table}.{column} count: {type(exc).__name__}: {str(exc)[:160]}")
        return 0


def _with_zeros(shape: RelationshipShape, parents: int) -> None:
    """Add the parents with no children, measured from the extract holding all."""
    missing = parents - shape.parents
    if missing <= 0 or not shape.histogram:
        return
    weight = missing / parents
    bins = [{"children": b["children"], "weight": b["weight"] * (1 - weight)}
            for b in shape.histogram]
    shape.histogram = [{"children": 0, "weight": round(weight, 8)}] + [
        {"children": b["children"], "weight": round(b["weight"], 8)} for b in bins
    ]
    shape.parents = parents
    shape.minimum = 0
    shape.mean = round(shape.children / parents, 4)
    variance = sum(b["weight"] * (b["children"] - shape.mean) ** 2 for b in shape.histogram)
    shape.stddev = round(variance ** 0.5, 4)

    def at(q: float) -> float:
        running = 0.0
        for b in shape.histogram:
            running += b["weight"]
            if running >= q:
                return float(b["children"])
        return float(shape.histogram[-1]["children"])

    shape.median, shape.p90, shape.p99 = at(0.5), at(0.9), at(0.99)
    shape.observed_in.append(f"{missing:,} parents with none, from the extract holding every one")


def _keep(found, pair, table, child_column, parent_column, measured) -> None:
    if measured is None:
        return
    held = found.get(pair)
    if held is not None and held.children >= measured["children"]:
        return
    found[pair] = RelationshipShape(
        child_element=pair[0],
        parent_element=pair[1],
        child_table=table,
        parent_table=table,
        observed_in=[f"{table}: {child_column} per {parent_column}"],
        **measured,
    )


def _nests(registry, connection: str, table: str, child: str, parent: str) -> bool:
    """Whether every value of `child` has exactly one value of `parent`."""
    sql = f"""
        SELECT TOP 1 1 FROM {_t(table)}
        WHERE {_q(child)} IS NOT NULL AND {_q(parent)} IS NOT NULL
        GROUP BY {_q(child)} HAVING COUNT(DISTINCT {_q(parent)}) > 1
    """
    try:
        with registry.connect(connection) as conn:
            return conn.cursor().execute(sql).fetchone() is None
    except Exception as exc:
        _unmeasurable.append(f"{table} {child} in {parent}: {type(exc).__name__}: {str(exc)[:160]}")
        return False


def _names_a_row(registry, connection: str, table: str, parent: str, sequence: str) -> bool:
    """Whether a parent key and a sequence together pick out one row each."""
    sql = f"""
        SELECT TOP 1 1 FROM {_t(table)}
        GROUP BY {_q(parent)}, {_q(sequence)} HAVING COUNT(*) > 1
    """
    try:
        with registry.connect(connection) as conn:
            return conn.cursor().execute(sql).fetchone() is None
    except Exception as exc:
        _unmeasurable.append(f"{table} {sequence} under {parent}: {type(exc).__name__}: {str(exc)[:160]}")
        return False


def _count_children(
    registry, connection: str, child_table: str, key: str, parent_table: str,
    *, child_key: str | None = None,
) -> dict[str, Any] | None:
    """One grouped count, read back as the spread of children per parent.

    With `child_key` the children are that column's distinct values under each
    parent -- claims per member in a table of lines -- rather than rows.
    """
    counted = f"COUNT(DISTINCT {_q(child_key)})" if child_key else "COUNT(*)"
    # Two steps on purpose. PERCENTILE_CONT is a window function and cannot
    # sit in the same SELECT as the aggregates, which SQL Server reports as a
    # GROUP BY error several lines away from the actual cause.
    sql = f"""
        WITH per_parent AS (
            SELECT {_q(key)} AS k, {counted} AS n
            FROM {_t(child_table)}
            WHERE {_q(key)} IS NOT NULL
            GROUP BY {_q(key)}
        ),
        totals AS (
            SELECT COUNT(*) AS parents, SUM(n) AS children,
                   MIN(n) AS lo, MAX(n) AS hi,
                   AVG(CAST(n AS FLOAT)) AS mean,
                   STDEV(CAST(n AS FLOAT)) AS spread
            FROM per_parent
        ),
        spread AS (
            SELECT DISTINCT
                   PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY n) OVER () AS p50,
                   PERCENTILE_CONT(0.9)  WITHIN GROUP (ORDER BY n) OVER () AS p90,
                   PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY n) OVER () AS p99
            FROM per_parent
        )
        SELECT * FROM totals CROSS JOIN spread
    """
    # How many parents have how many children. The engine draws from this, so a
    # mean alone would turn a book where half have two and half have forty into
    # one where everybody has twenty-one.
    spread_sql = f"""
        SELECT n AS children, COUNT(*) AS parents
        FROM (
            SELECT {counted} AS n FROM {_t(child_table)}
            WHERE {_q(key)} IS NOT NULL GROUP BY {_q(key)}
        ) AS per_parent
        GROUP BY n
        ORDER BY n
    """
    try:
        with registry.connect(connection) as conn:
            row = conn.cursor().execute(sql).fetchone()
            bins = conn.cursor().execute(spread_sql).fetchall()
    except Exception as exc:
        # Reported, never swallowed. A bare except here hid a SQL error that
        # made every relationship silently absent, which reads exactly like a
        # landscape that has none.
        _unmeasurable.append(
            f"{child_table} on {key}: {type(exc).__name__}: {str(exc)[:160]}"
        )
        return None
    if not row or not row.parents:
        return None
    total = sum(int(b.parents) for b in bins) or 1
    histogram = [
        {"children": int(b.children), "weight": round(int(b.parents) / total, 8)}
        for b in bins
    ]
    return {
        "histogram": histogram,
        "parents": int(row.parents),
        "children": int(row.children or 0),
        "minimum": int(row.lo or 0),
        "maximum": int(row.hi or 0),
        "mean": round(float(row.mean or 0), 4),
        "stddev": round(float(row.spread or 0), 4),
        "median": round(float(row.p50 or 0), 4),
        "p90": round(float(row.p90 or 0), 4),
        "p99": round(float(row.p99 or 0), 4),
    }


def _widest(values: list[str]) -> str:
    order = ["empty", "constant", "low", "high", "unique", "unknown"]
    known = [v for v in values if v in order]
    return max(known, key=order.index) if known else "unknown"


def _extreme(values: list, *, low: bool):
    present = [v for v in values if v is not None]
    if not present:
        return None
    try:
        return min(present) if low else max(present)
    except TypeError:
        # Mixed types across columns carrying one element is a mapping problem,
        # not something to resolve by comparing a date to a string.
        return None


# ── storage ─────────────────────────────────────────────────────────────────

def save(shape: LandscapeShape, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(shape.model_dump(exclude_none=True), indent=2, default=str),
        encoding="utf-8",
    )
    return path


def load(path: Path) -> LandscapeShape:
    return LandscapeShape.model_validate_json(Path(path).read_text(encoding="utf-8"))
