"""Create (or drop and recreate) the two PoC databases on the configured server.

Usage:
    python seed/create_databases.py            # create if missing
    python seed/create_databases.py --drop     # drop and recreate
"""

from __future__ import annotations

import argparse
import sys

import pyodbc

from sdg.config import get_settings, not_a_demo_machine, refuse_protected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the FACETS_QA and MDM_REF databases.")
    parser.add_argument("--drop", action="store_true", help="drop the databases first")
    args = parser.parse_args(argv)

    settings = get_settings()
    databases = [settings.source_db, settings.ref_db]
    # Refused before connecting: --drop destroys both databases outright.
    reason = not_a_demo_machine() if args.drop else None
    if reason:
        raise SystemExit(f"This machine is {reason}: --drop would destroy "
                         f"{', '.join(databases)}. Nothing was dropped.")
    for db in databases:
        refuse_protected(db, "drop and recreate" if args.drop else "create")

    conn = pyodbc.connect(settings.mssql.connection_string("master"), autocommit=True)
    cur = conn.cursor()
    try:
        for db in databases:
            exists = cur.execute(
                "SELECT 1 FROM sys.databases WHERE name = ?", db
            ).fetchone()
            if exists and args.drop:
                print(f"dropping {db}")
                cur.execute(f"ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
                cur.execute(f"DROP DATABASE [{db}]")
                exists = None
            if not exists:
                print(f"creating {db}")
                cur.execute(f"CREATE DATABASE [{db}]")
            else:
                print(f"{db} already exists")
            # Recovery model SIMPLE keeps the log small during bulk seeding.
            cur.execute(f"ALTER DATABASE [{db}] SET RECOVERY SIMPLE")
    finally:
        conn.close()

    print(f"server  : {settings.mssql.server}")
    print(f"databases: {', '.join(databases)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
