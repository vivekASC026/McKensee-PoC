"""The banking pack's sample bank: about 2,000 made-up customers in a database of its own.

    python -m seed.bank SDMT_BANK_SRC        (an SDMT_* database: a test's)
    python -m seed.bank BANK_DEMO --allow    (any other name, on purpose)

Creates the database (dropping it first if it is there), the tables of
sdg/packs/banking/sample/bank_source.sql, and fills them the same way for the
same seed: branches, products and merchant codes; customers with one to four
accounts each, by segment; each account's transactions by product type; a
general ledger and an audit log beside them. Nothing here is a real bank's,
and no database not named on the command line is touched: never a protected
one, a system one, or one this installation is configured to use, whatever
--allow says.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pyodbc

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdg.config import (  # noqa: E402
    ProtectedDatabase, get_settings, not_a_demo_machine, refuse_protected,
)
from seed.build import run_sql_script  # noqa: E402

SCHEMA = ROOT / "sdg" / "packs" / "banking" / "sample" / "bank_source.sql"
DICTIONARIES = ROOT / "examples" / "dictionaries"
AS_OF = date(2026, 6, 30)

BRANCHES = [(1, "Downtown Columbus", "OH", "Columbus"), (2, "Cincinnati Riverfront", "OH", "Cincinnati"),
            (3, "Louisville Main", "KY", "Louisville"), (4, "Lexington Center", "KY", "Lexington"),
            (5, "Indianapolis North", "IN", "Indianapolis"), (6, "Atlanta Midtown", "GA", "Atlanta")]
PRODUCTS = [("CHK01", "Everyday checking", "CHECKING"), ("CHK02", "Premier checking", "CHECKING"),
            ("SAV01", "Savings", "SAVINGS"), ("SAV02", "Money market", "SAVINGS"),
            ("CRD01", "Rewards card", "CARD"), ("CRD02", "Basic card", "CARD"),
            ("LON01", "Auto loan", "LOAN"), ("LON02", "Business line of credit", "LOAN")]
MCCS = [("5411", "Grocery stores"), ("5541", "Service stations"), ("5812", "Restaurants"),
        ("5912", "Drug stores"), ("5942", "Book stores"), ("4111", "Commuter transport"),
        ("4900", "Utilities"), ("5311", "Department stores"), ("7011", "Lodging"),
        ("4511", "Airlines"), ("5732", "Electronics stores"), ("8220", "Colleges")]
# By segment: its share of customers, and the products its customers open.
SEGMENTS = {
    "MASS": (0.62, ["CHK01", "SAV01", "CRD02", "LON01"]),
    "AFFLUENT": (0.22, ["CHK02", "SAV02", "CRD01", "SAV01"]),
    "PRIVATE": (0.06, ["CHK02", "SAV02", "CRD01"]),
    "SMALL_BIZ": (0.10, ["CHK01", "LON02", "CRD02"]),
}


SYSTEM = {"MASTER", "MODEL", "MSDB", "TEMPDB"}


class Refused(ValueError):
    """A database the sample bank may not be built in, or dropped from."""


def check(database: str, allow: bool = False) -> None:
    """Refuse before any database is dropped: the name, then what the database is."""
    reason = not_a_demo_machine()
    if reason:
        raise Refused(f"This machine is {reason}: the sample bank drops and rebuilds "
                      f"{database}. Nothing was changed.")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", database or ""):
        raise Refused(f"{database!r} is not a plain database name (letters, digits, underscores).")
    refuse_protected(database, "drop and rebuild")
    settings = get_settings()
    configured = {settings.source_db, settings.ref_db, settings.denorm_db, settings.target_db}
    if database.upper() in SYSTEM or database.upper() in {d.upper() for d in configured if d}:
        raise Refused(f"{database} is a system database or one this installation is configured "
                      "to use; the sample bank drops and rebuilds its database, so not there.")
    if not database.upper().startswith("SDMT_") and not allow:
        raise Refused(f"{database} is not an SDMT_* database: it is dropped and rebuilt. "
                      "Pass --allow to do that on purpose.")


def _connect(database: str, autocommit: bool = True) -> pyodbc.Connection:
    return pyodbc.connect(get_settings().mssql.connection_string(database), autocommit=autocommit)


def recreate(database: str, allow: bool = False) -> None:
    check(database, allow)
    with _connect("master") as master:
        master.execute(f"IF DB_ID(N'{database}') IS NOT NULL BEGIN ALTER DATABASE [{database}] "
                       f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{database}]; END")
        master.execute(f"CREATE DATABASE [{database}]")


def drop(database: str, allow: bool = False) -> None:
    check(database, allow)
    with _connect("master") as master:
        master.execute(f"IF DB_ID(N'{database}') IS NOT NULL BEGIN ALTER DATABASE [{database}] "
                       f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{database}]; END")


def _words(name: str) -> list[str]:
    return [w.strip() for w in (DICTIONARIES / name).read_text(encoding="utf-8").splitlines() if w.strip()]


def _zips(states: set[str]) -> dict[str, list[tuple[str, str]]]:
    by_state: dict[str, list[tuple[str, str]]] = {s: [] for s in states}
    with (DICTIONARIES / "us_zip.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["state"] in by_state:
                by_state[row["state"]].append((row["zip"], row["city"]))
    return by_state


def rows(customers: int = 2000, seed: str = "bank-2026") -> dict[str, list[tuple]]:
    """Every table's rows, the same for the same seed and size."""
    rng = random.Random(seed)
    first, last = _words("first_names.txt"), _words("last_names.txt")
    zips = _zips({b[2] for b in BRANCHES})
    product_type = {code: kind for code, _, kind in PRODUCTS}
    out: dict[str, list[tuple]] = {"BRANCH": list(BRANCHES), "PRODUCT": list(PRODUCTS),
                                   "MCC": list(MCCS), "CUSTOMER": [], "ACCOUNT": [], "TXN": [],
                                   "GL_ENTRY": [], "AUDIT_LOG": []}
    segments = list(SEGMENTS)
    weights = [SEGMENTS[s][0] for s in segments]
    account_id = 4_000_000_000
    for n in range(1, customers + 1):
        customer_id = 100_000 + n
        segment = rng.choices(segments, weights)[0]
        branch = rng.choice(BRANCHES)
        zip_code, city = rng.choice(zips[branch[2]] or [("00000", branch[3])])
        age = rng.randint(19, 88) if segment != "PRIVATE" else rng.randint(40, 88)
        birth = AS_OF - timedelta(days=age * 365 + rng.randint(0, 364))
        since = AS_OF - timedelta(days=rng.randint(30, min(age - 18, 30) * 365))
        given, family = rng.choice(first), rng.choice(last)
        email = f"{given.lower()}.{family.lower()}{n}@example.com" if rng.random() < 0.8 else None
        out["CUSTOMER"].append((customer_id, given, family, birth, segment, since, city, branch[2],
                                zip_code, email, branch[0]))
        offered = SEGMENTS[segment][1]
        for code in rng.sample(offered, rng.randint(1, min(len(offered), 4))):
            account_id += 1
            kind = product_type[code]
            opened = since + timedelta(days=rng.randint(0, max((AS_OF - since).days, 1)))
            status = rng.choices("ADC", [0.86, 0.09, 0.05])[0]
            scale = {"MASS": 1, "AFFLUENT": 6, "PRIVATE": 40, "SMALL_BIZ": 10}[segment]
            balance = Decimal(round(rng.lognormvariate(7, 1.2) * scale, 2)).quantize(Decimal("0.01"))
            out["ACCOUNT"].append((account_id, customer_id, code, opened, status, balance))
            count = 0 if status == "C" else {"CHECKING": rng.randint(4, 30), "SAVINGS": rng.randint(1, 6),
                                             "CARD": rng.randint(3, 25), "LOAN": rng.randint(1, 12)}[kind]
            for seq in range(1, count + 1):
                day = AS_OF - timedelta(days=rng.randint(0, 729))
                if kind == "CARD":
                    txn = ("DEBIT", rng.choice(["CARD", "CARD", "ONLINE"]), rng.choice(MCCS)[0])
                elif kind == "CHECKING":
                    t = rng.choices(["DEBIT", "CREDIT", "FEE"], [0.6, 0.35, 0.05])[0]
                    txn = (t, rng.choice(["BRANCH", "ATM", "ONLINE"]),
                           rng.choice(MCCS)[0] if t == "DEBIT" and rng.random() < 0.5 else None)
                elif kind == "SAVINGS":
                    txn = (rng.choice(["CREDIT", "INTEREST"]), rng.choice(["BRANCH", "ONLINE"]), None)
                else:
                    txn = ("CREDIT", "ONLINE", None)
                amount = {"FEE": 12.0, "INTEREST": rng.uniform(0.5, 40)}.get(
                    txn[0], rng.lognormvariate(3.8, 1.0) * (scale if kind == "LOAN" else 1))
                out["TXN"].append((account_id, seq, day, txn[0], txn[1], txn[2],
                                   Decimal(round(amount, 2)).quantize(Decimal("0.01"))))
    for n in range(1, 201):
        out["GL_ENTRY"].append((n, f"GL-{rng.randint(1000, 9999)}", AS_OF - timedelta(days=rng.randint(0, 365)),
                                Decimal(round(rng.uniform(-50000, 50000), 2)).quantize(Decimal("0.01"))))
    for n in range(1, 101):
        out["AUDIT_LOG"].append((n, rng.choice(["CUSTOMER", "ACCOUNT", "TXN"]),
                                 f"{AS_OF - timedelta(days=rng.randint(0, 365))} 12:00:00", "ops"))
    return out


def build(database: str, customers: int = 2000, seed: str = "bank-2026",
          allow: bool = False) -> dict[str, int]:
    """The sample bank in `database`, created anew. Returns each table's row count."""
    recreate(database, allow)
    filled = rows(customers, seed)
    conn = _connect(database, autocommit=False)
    try:
        run_sql_script(conn, SCHEMA)
        cur = conn.cursor()
        cur.fast_executemany = True
        for table in ("BRANCH", "PRODUCT", "MCC", "CUSTOMER", "ACCOUNT", "TXN", "GL_ENTRY", "AUDIT_LOG"):
            data = filled[table]
            marks = ", ".join("?" * len(data[0]))
            cur.executemany(f"INSERT INTO dbo.{table} VALUES ({marks})", data)
        conn.commit()
    finally:
        conn.close()
    return {table: len(data) for table, data in filled.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("database")
    parser.add_argument("--customers", type=int, default=2000)
    parser.add_argument("--allow", action="store_true",
                        help="build in a database not named SDMT_* (it is dropped first)")
    args = parser.parse_args(argv)
    try:
        check(args.database, args.allow)
    except (Refused, ProtectedDatabase) as refused:
        parser.error(str(refused))
    for table, count in build(args.database, args.customers, allow=args.allow).items():
        print(f"{table:10} {count:>8,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
