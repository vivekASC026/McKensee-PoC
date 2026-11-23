"""Reading a DataFrame from a pyodbc connection, without the warning.

pandas warns that it only supports SQLAlchemy connectables and that anything
else is untested. It is right that it is untested by them, and it has worked
here for the life of this project across every query the analyzer makes.

The warning is emitted once per call site per run, which came to forty-five
lines of a test run -- enough that a warning worth reading would be lost in
them. So it is silenced exactly, by message, at the one place every analyzer
query goes through. Any other warning pandas has still arrives.

Moving to SQLAlchemy would be the other answer. It is a larger change than it
sounds: the connection registry, the analyzer's own connection handling and
every `with get_connection()` would move together, and the benefit is a quieter
log rather than a behavior anybody wants. Worth doing deliberately, not as a
side effect of tidying.
"""

from __future__ import annotations

import warnings
from typing import Any

import pandas as pd

# The exact complaint, so that a different one is not swallowed with it.
_MESSAGE = "pandas only supports SQLAlchemy connectable"


def read_sql(sql: str, conn: Any, params: Any = None) -> pd.DataFrame:
    """`pandas.read_sql`, with the pyodbc notice silenced and nothing else."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=_MESSAGE, category=UserWarning)
        if params is None:
            return pd.read_sql(sql, conn)
        return pd.read_sql(sql, conn, params=params)


def literal(value: Any) -> str:
    """A value as a T-SQL string literal, whatever it holds.

    The analyzer's filters write their values into the SQL rather than as
    parameters, because one WHERE clause is reused inside other queries. A
    value is text a request sent, so a quote in it is doubled -- the only
    character that can end a literal -- and it can never become SQL. Not N''
    on purpose: the columns are CHAR, and an NVARCHAR literal would make SQL
    Server convert every row before it could use an index."""
    return "'" + str(value).strip().replace("'", "''") + "'"
