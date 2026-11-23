"""A large table, read through a fixed sample of its rows.

The measures a database load needs beyond the column measures -- how many
children each parent has, which columns decide others, which dates come first
-- are shapes and shares, and a sample of rows shows them as well as the whole
table does. Reading the whole table for each of them is what made a read of a
large database slow: one pass per measure per table, on every table.

So a table over twice the sample size is read through a page sample with a
fixed seed, capped at the sample size. A re-read sees the same rows, so two
profiles of an unchanged database agree. A smaller table is read whole: it is
cheap, and its measures stay exact.
"""

from __future__ import annotations

import re

from sdg.discovery.model import Column, Table

# The rows a sampled measure reads, and the seed that makes them the same rows
# every time. The same figures the column profile's own sample uses.
SAMPLE_ROWS = 100_000
SAMPLE_SEED = 20260915
# Above a set number of rows (SDG_WHOLE_READ_MAX_ROWS, 50 million unless the
# .env file says otherwise) nothing reads a table whole: a pass over a billion
# rows takes tens of minutes, and a read of the source makes many passes. What
# is measured on such a table comes from a sample of it, or from a seek on an
# index; what can be had neither way is said to be estimated, not read.


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def qualified(table: Table) -> str:
    return f"{_quote(table.schema_name)}.{_quote(table.table_name)}"


def is_sampled(table: Table, rows: int = SAMPLE_ROWS) -> bool:
    """Whether this table is read through a sample rather than whole."""
    return bool(rows) and table.row_count > rows * 2


def is_huge(table: Table, rows: int | None = None) -> bool:
    """Whether this table is too large to read whole even once. `rows` is the
    limit; by default the installation's own, and 0 means no table is."""
    if rows is None:
        from sdg.config import whole_read_max_rows

        rows = whole_read_max_rows()
    return bool(rows) and table.row_count > rows


def seek_hint(table: Table, column: str, rows: int | None = None) -> str:
    """For a query that looks up rows of a huge table by an indexed column:
    one seek per value looked up, never a scan of the table. Left to itself
    the database may prefer a scan, which on a billion rows is the slow read
    the sample was there to avoid."""
    return " OPTION (LOOP JOIN)" if is_huge(table, rows) and table.leads_index(column) else ""


def collate_as(column: Column | None, value: Column | None) -> str:
    """What to put after a value from `value` compared with `column`, when
    the two are text of different collations: the value is read in
    `column`'s, so the comparison runs and an index on `column` still serves."""
    if column is None or value is None or not column.collation or not value.collation:
        return ""
    if column.collation == value.collation:
        return ""
    # A collation is a bare name in the SQL, so only a name is written: one
    # that is not plain letters, digits and underscores is left out.
    if not _COLLATION.match(column.collation):
        return ""
    return f" COLLATE {column.collation}"


_COLLATION = re.compile(r"^[A-Za-z0-9_]{1,128}$")


# A date at or past this stands for "no end", at or before EARLY_DATE for "no
# date": Facets writes 9999-12-31 and 1753-01-01.
LATE_DATE = "9999-01-01"
EARLY_DATE = "1900-01-01"


def late_placeholder(expression: str, column: Column) -> str:
    # A smalldatetime ends in 2079 and cannot even be compared with 9999.
    if column.type_name == "smalldatetime":
        return "1 = 0"
    return f"{expression} >= '{LATE_DATE}'"


def early_placeholder(expression: str) -> str:
    return f"{expression} <= '{EARLY_DATE}'"


def real_date(expression: str, column: Column) -> str:
    """A condition true of a real date in `expression`, not a placeholder."""
    late = "" if column.type_name == "smalldatetime" else f" AND {expression} < '{LATE_DATE}'"
    return f"{expression} > '{EARLY_DATE}'{late}"


def source(table: Table, alias: str | None = None, *, rows: int = SAMPLE_ROWS,
           seed: int = SAMPLE_SEED) -> str:
    """What to put after FROM: the table itself, or a fixed sample of it."""
    name = qualified(table)
    if not is_sampled(table, rows):
        return f"{name} AS {alias}" if alias else name
    # Pages are sampled, so a share somewhat over the rows wanted is asked for
    # and the cap trims it; a table with few rows per page still fills it.
    percent = max(0.01, min(100.0, rows * 2.5 / table.row_count * 100))
    sampled = (f"(SELECT TOP ({rows}) * FROM {name} "
               f"TABLESAMPLE SYSTEM ({percent:.4f} PERCENT) REPEATABLE ({seed}))")
    return f"{sampled} AS {alias or _quote('sampled')}"
