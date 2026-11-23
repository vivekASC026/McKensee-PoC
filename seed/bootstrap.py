"""Create the databases this tool needs, and the analysis store inside one.

Everything else in `seed/` assumes the databases exist and fills them. On the
machine this was written on they did, because somebody made them by hand once.
On any other machine they do not, and the first thing a person sees is a
connection error naming a database nobody told them to create.

So this is the step before the fixture: three databases, and the schema the
analysis store needs. Safe to re-run -- a database that exists is left exactly
as it is, which matters because one of them holds saved patterns and generation
rules that nothing else can recreate.

    python seed/bootstrap.py           create what is missing
    python seed/bootstrap.py --check   say what is missing, create nothing
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build import run_sql_script  # noqa: E402

from sdg.config import get_settings  # noqa: E402

SEED_DIR = Path(__file__).resolve().parent


def log(message: str) -> None:
    print(message, flush=True)


def databases(settings) -> list[tuple[str, str]]:
    """Every database, and what it is for."""
    return [
        (settings.source_db, "a source to read, and the demo fixture"),
        (settings.ref_db, "reference and master data"),
        (settings.denorm_db, "the analysis store, and this tool's own tables"),
        # The target is usually the source. Named separately so generating into
        # a different database is a setting rather than a code change.
        (settings.target_db, "where generated rows are written"),
    ]


def ensure_databases(settings, *, check_only: bool = False) -> list[str]:
    """Create any that are absent. Never touch one that is there."""
    conn = pyodbc.connect(settings.mssql.connection_string("master"), autocommit=True)
    created: list[str] = []
    try:
        cursor = conn.cursor()
        seen: set[str] = set()
        for name, purpose in databases(settings):
            if name in seen:
                continue
            seen.add(name)
            if name == settings.denorm_db and settings.store != settings.mssql:
                # The store has a connection of its own -- a customer's
                # database, which exists and is not this tool's to create.
                try:
                    pyodbc.connect(settings.store.connection_string(name), timeout=15).close()
                    log(f"  {name:<16} reached on its own connection  ({purpose})")
                except pyodbc.Error as exc:
                    raise SystemExit(f"{name} could not be reached on the store's "
                                     f"connection ({settings.store.server}): {exc}") from exc
                continue
            exists = cursor.execute(
                "SELECT 1 FROM sys.databases WHERE name = ?", name
            ).fetchone()
            if exists:
                log(f"  {name:<16} already there  ({purpose})")
                continue
            if check_only:
                log(f"  {name:<16} MISSING        ({purpose})")
                created.append(name)
                continue
            cursor.execute(f"CREATE DATABASE [{name}]")
            log(f"  {name:<16} created        ({purpose})")
            created.append(name)
    finally:
        conn.close()
    return created


def plain_schema(schema: str) -> str:
    """The configured schema name, refused unless it is a plain SQL name.

    It is placed in `CREATE SCHEMA [..]` and substituted into the store's DDL,
    so anything but letters, digits and underscores is refused before use.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", schema or ""):
        raise SystemExit(f"SDG_DENORM_SCHEMA={schema!r} is not a plain schema name. Use "
                         "letters, digits and underscores only, then run this again.")
    return schema


def in_schema(text: str, schema: str) -> str:
    """A store script written for schema `sdm`, rewritten for the configured one."""
    schema = plain_schema(schema)
    if schema == "sdm":
        return text
    text = re.sub(r"\bsdm\.", f"{schema}.", text)
    text = text.replace("'sdm'", f"'{schema}'")
    return re.sub(r"\bSCHEMA sdm\b", f"SCHEMA [{schema}]", text)


def ensure_store(settings, indexes: bool | None = None) -> None:
    """The analysis store's schema: this tool's tables, and the extract tables.

    Split from `ddl_sdm.sql` at the point where that file stops describing the
    store and starts describing the source. The two views at the end read the
    FACETS tables and belong in the source database; everything above them is
    the store's own. Both scripts are written for schema `sdm` and are run in
    the configured one, on the store's own connection.
    """
    from sdg.config import refuse_protected

    # Never in a protected database (SDG_PROTECTED_DATABASES), whoever calls: before anything
    # is created, not only by the guard at the top of each pack script.
    refuse_protected(settings.denorm_db, "create tables in")
    schema = plain_schema(settings.denorm_schema)
    conn = pyodbc.connect(
        settings.store.connection_string(settings.denorm_db), autocommit=False
    )
    try:
        conn.cursor().execute(
            f"IF SCHEMA_ID('{schema}') IS NULL EXEC('CREATE SCHEMA [{schema}]')"
        )
        conn.commit()

        text = (SEED_DIR / "ddl_sdm.sql").read_text(encoding="utf-8")
        marker = "IF OBJECT_ID('sdm.member_denorm', 'V')"
        own = text[: text.index(marker)] if marker in text else text

        scratch = SEED_DIR / "_store_only.sql"
        scratch.write_text(in_schema(own, schema), encoding="utf-8")
        try:
            run_sql_script(conn, scratch)
        finally:
            scratch.unlink(missing_ok=True)

        # The business pack's own tables (health: the two denorm tables, then
        # the provider, plan and claim-detail tables beside them) -- the same
        # scripts a customer runs, add-only.
        from sdg import packs

        pack = packs.active()
        for script in pack.sql("store_tables"):
            scratch.write_text(
                in_schema(script.read_text(encoding="utf-8"), schema), encoding="utf-8")
            try:
                run_sql_script(conn, scratch)
            finally:
                scratch.unlink(missing_ok=True)

        if indexes or (indexes is None and settings.store == settings.mssql):
            # A store of this tool's own is indexed for the reads it serves;
            # a customer's existing store is theirs to index, unless they ask
            # (a new deployment, where the tool creates the denorm itself).
            for script in pack.sql("store_indexes"):
                scratch.write_text(
                    in_schema(script.read_text(encoding="utf-8"), schema), encoding="utf-8")
                try:
                    run_sql_script(conn, scratch)
                finally:
                    scratch.unlink(missing_ok=True)

        tables = [
            row[0] for row in conn.cursor().execute(
                "SELECT o.name FROM sys.objects o JOIN sys.schemas s "
                "ON s.schema_id = o.schema_id WHERE s.name = ? AND o.type = 'U' "
                "ORDER BY o.name", schema
            ).fetchall()
        ]
        log(f"  {schema} schema in {settings.denorm_db}: {len(tables)} tables")
        # The tables the pack's pattern analysis reads (health: member_denorm,
        # claims_denorm); a pack that reads none yet checks none.
        read = [loader["table"] for key, loader in pack.features.get("loaders", {}).items()
                if not key.startswith("_")]
        for name in read:
            if name not in tables:
                raise SystemExit(
                    f"{name} was not created in {settings.denorm_db}; "
                    f"pattern analysis has nothing to read"
                )
    finally:
        conn.close()


def previous_source_scale(settings) -> float | None:
    """The scale a source from before the customer's codes was built at, or None.

    That version's FACETS_QA has two-digit relationship codes, three-letter
    plan categories, SSNs with dashes and no member suffix, none of which the
    customer-shaped store can take. It is the demo fixture, so it is rebuilt
    at the same size rather than altered in place; what people made lives in
    the store and is kept.
    """
    conn = pyodbc.connect(
        settings.mssql.connection_string(settings.source_db), autocommit=True
    )
    try:
        cursor = conn.cursor()
        if cursor.execute("SELECT OBJECT_ID('dbo.CMC_MEME_MEMBER', 'U')").fetchone()[0] is None:
            return None
        if cursor.execute("SELECT COL_LENGTH('dbo.CMC_MEME_MEMBER', 'MEME_SFX')").fetchone()[0]:
            return None
        subscribers = cursor.execute("SELECT COUNT(*) FROM dbo.CMC_SBSB_SUBSC").fetchone()[0]
    finally:
        conn.close()
    from build import FULL_SUBSCRIBERS

    return max(0.0001, round(subscribers / FULL_SUBSCRIBERS, 4))


def refresh_source_views(settings) -> bool:
    """The two extract views in the source, as `ddl_sdm.sql` now describes them.

    An extract selects through them, so a column added to the views (MEME_ID,
    for one) must reach a source built by an earlier version. Only the views
    are replaced; no table and no row is touched. A source with no FACETS
    tables yet -- a new machine before `seed.cmd` -- is left alone.
    """
    from sdg.config import refuse_protected

    # The part of ddl_sdm.sql run here is after its guard: refused here.
    refuse_protected(settings.source_db, "create views in")
    conn = pyodbc.connect(
        settings.mssql.connection_string(settings.source_db), autocommit=False
    )
    try:
        if conn.cursor().execute(
            "SELECT OBJECT_ID('dbo.CMC_MEME_MEMBER', 'U')"
        ).fetchone()[0] is None:
            return False
        conn.cursor().execute("IF SCHEMA_ID('sdm') IS NULL EXEC('CREATE SCHEMA sdm')")
        conn.commit()
        text = (SEED_DIR / "ddl_sdm.sql").read_text(encoding="utf-8")
        marker = "IF OBJECT_ID('sdm.member_denorm', 'V')"
        views = SEED_DIR / "_views_only.sql"
        views.write_text(text[text.index(marker):], encoding="utf-8")
        try:
            run_sql_script(conn, views)
        finally:
            views.unlink(missing_ok=True)
        return True
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report what is missing, create nothing"
    )
    parser.add_argument(
        "--previous-source", action="store_true",
        help="print the scale to rebuild the source at, if it is from the previous version",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.previous_source:
        scale = previous_source_scale(settings)
        if scale is not None:
            print(scale)
        return 0
    log(f"SQL Server at {settings.mssql.server}")

    missing = ensure_databases(settings, check_only=args.check)
    if args.check:
        log(f"\n{len(missing)} missing" if missing else "\nnothing missing")
        return 1 if missing else 0

    log("creating the analysis store")
    ensure_store(settings)
    if previous_source_scale(settings) is not None:
        log(f"  {settings.source_db} is from the previous version; "
            f"scripts\\upgrade.cmd rebuilds it")
    elif refresh_source_views(settings):
        log(f"  extract views in {settings.source_db} brought up to date")
    log("\nready. Next: scripts\\seed.cmd to build the demo fixture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
