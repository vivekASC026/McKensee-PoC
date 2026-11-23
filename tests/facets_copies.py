"""Databases with Facets' tables and no members or claims, for onboarding tests.

Each holds the reference rows a run reuses -- providers, groups, plans, code
sets -- copied from the source, and nothing else: a new environment waiting to
be filled. Built and dropped by name; nothing here touches the source.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pyodbc

from sdg.config import get_settings

ROOT = Path(__file__).resolve().parent.parent

# Parents before children, so the copied rows satisfy the declared keys.
REFERENCE_TABLES = [
    "dbo.CMC_IDCD_DIAG_CODE", "dbo.CMC_IPCD_PROC_CODE", "dbo.CMC_PRPR_PROV",
    "dbo.CMC_NWNW_NETWORK", "dbo.CMC_NWPR_RELATION", "dbo.CMC_NWST_NET_SET",
    "dbo.CMC_PDPD_PRODUCT", "dbo.CMC_PDDS_PROD_DESC", "dbo.CMC_GRGR_GROUP",
    "dbo.CMC_SGSG_SUBGROUP", "dbo.CMC_CSPD_CAT_DESC", "dbo.CMC_CSPI_CS_PLAN",
    "dbo.CMC_PLDS_PLAN_DESC",
]
GENERATED_TABLES = ("CMC_SBSB_SUBSC", "CMC_MEME_MEMBER", "CMC_CLCL_CLAIM", "CMC_CDML_CL_LINE")


def _master():
    settings = get_settings()
    return pyodbc.connect(settings.mssql.connection_string("master"), autocommit=True)


def drop(name: str) -> None:
    with _master() as master:
        if master.execute("SELECT DB_ID(?)", name).fetchone()[0]:
            master.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            master.execute(f"DROP DATABASE [{name}]")


def build(name: str, *alterations: str) -> None:
    """Create `name` with Facets' schema and reference rows, then alter it."""
    from seed.build import run_sql_script

    settings = get_settings()
    drop(name)
    with _master() as master:
        master.execute(f"CREATE DATABASE [{name}]")
    conn = pyodbc.connect(settings.mssql.connection_string(name), autocommit=False)
    try:
        run_sql_script(conn, ROOT / "seed" / "ddl_facets.sql")
        cur = conn.cursor()
        for table in REFERENCE_TABLES:
            cols = [r[0] for r in cur.execute(
                "SELECT name FROM sys.columns WHERE object_id = OBJECT_ID(?) "
                "AND is_computed = 0 ORDER BY column_id", f"{name}.{table}").fetchall()]
            names = ", ".join(f"[{c}]" for c in cols)
            sql = (f"INSERT INTO {name}.{table} ({names}) "
                   f"SELECT {names} FROM {settings.source_db}.{table}")
            if cur.execute("SELECT OBJECTPROPERTY(OBJECT_ID(?), 'TableHasIdentity')",
                           f"{name}.{table}").fetchone()[0]:
                sql = (f"SET IDENTITY_INSERT {name}.{table} ON; {sql}; "
                       f"SET IDENTITY_INSERT {name}.{table} OFF")
            cur.execute(sql)
        for statement in alterations:
            cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


def counts(name: str) -> dict[str, int]:
    settings = get_settings()
    conn = pyodbc.connect(settings.mssql.connection_string(name))
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM dbo.{t}").fetchone()[0]
                for t in GENERATED_TABLES}
    finally:
        conn.close()


@contextmanager
def copies(*specs: tuple[str, tuple[str, ...]]):
    """Build each (name, alterations), yield, and always drop them all."""
    try:
        for name, alterations in specs:
            build(name, *alterations)
        yield
    finally:
        for name, _ in specs:
            drop(name)
