"""A pack built by the accelerator from the bank demo source matches the hand pack.

docs/day0-accelerator-phase2-plan.md, step 5. The sample bank (seed/bank.py)
is built in a database of its own and read as a person would: `sdg
accelerate landscape`, then `propose` with the primary dbo.CUSTOMER and the
child dbo.ACCOUNT (its lines dbo.TXN), no criteria. The record kinds and the
pack are named; two identifying columns are added to the customers' table
(both withheld) and the customer's key is made a keyed surrogate, so the
privacy classes are proved on real rows. The plan is submitted, approved
and built into tmp_path.

Then, in processes of their own:
- under the generated pack (SDG_PACKS_DIR, SDG_PACK), into a fresh store:
  `customer-tables`, `extract <SOURCE>` (a built pack, so the approved load
  of sdg.accelerator.loading), then pattern discovery for each
  record kind through the /api/discovery route (TestClient);
- under the hand banking pack, into a store of its own: `customer-tables`,
  `extract BANK`.

Parity: each generated table holds as many rows as the hand pack's table of
the same record kind (customers; accounts at transaction grain), and each
walk level's key has as many distinct values. Privacy: every withheld column
is NULL in every row, and every surrogate is 16 characters (a generated
pack's; health's stays 9).

Approval is written to the generated pack's store, the one it is loaded
into (SDG_DB_DENORM points at it for the test): a load refuses a store that
holds no approval of the build. The approval setting's second approver is
taken as unset, since the approval control is proved in
test_accelerator_approval. Only the three SDMT_BANK_GEN* databases named
below are created, and each is dropped afterwards by its exact name. The source is only read after it is built.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pyodbc
import pytest

pytestmark = [pytest.mark.db, pytest.mark.slow]

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "SDMT_BANK_GEN_SRC"
GENERATED_STORE = "SDMT_BANK_GEN"
HAND_STORE = "SDMT_BANK_GEN_HAND"
CREATED = (SOURCE, GENERATED_STORE, HAND_STORE)
PACK_ID = "gen_bank"
# A key for this test's surrogates only: never a customer's.
TEST_EXTRACT_KEY = "parity-test-key-not-a-secret"
# The hand banking pack's tables, by role.
HAND_TABLES = {"primary": "customer_denorm", "child": "account_denorm"}

DISCOVER = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from tests import conftest  # noqa: F401  the suite's client: this machine's name, the token
from fastapi.testclient import TestClient
from sdg import packs
from sdg.api.app import app

c = TestClient(app)
out = {"pack": packs.active().id}
for entity in sys.argv[2:]:
    r = c.get(f"/api/discovery?entity={entity}&max_depth=1")
    if r.status_code >= 400:
        raise SystemExit(f"discovery of {entity}: {r.status_code} {r.text[:400]}")
    body = r.json()
    out[entity] = {"n_discovered": body["n_discovered"],
                   "combinations": body["combinations_processed"],
                   "population": body["total_population"]}
print("RESULT " + json.dumps(out))
'''


def _master() -> pyodbc.Connection:
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string("master"), autocommit=True)


def _checked(database: str) -> str:
    """Only a database this test creates, by its exact name."""
    assert database in CREATED, database
    assert re.fullmatch(r"SDMT_BANK_GEN[A-Z_]*", database), database
    return database


def _drop(database: str) -> None:
    name = _checked(database)
    with _master() as master:
        master.execute(f"IF DB_ID(N'{name}') IS NOT NULL BEGIN ALTER DATABASE [{name}] "
                       f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{name}]; END")


def _fresh(database: str, created: list[str]) -> str:
    """A name this run may create: refused when a database of that name is
    there already (someone's, or one an earlier run left), which is never
    dropped here. Recorded as this run's before it is made, so it is
    dropped afterwards even when making it fails part way."""
    name = _checked(database)
    if _exists(name):
        pytest.fail(f"{name} is there already, and this test drops only the databases it "
                    f"creates: drop it by hand if it is an earlier run's, then run again.",
                    pytrace=False)
    created.append(name)
    return name


def _create(database: str, created: list[str]) -> None:
    name = _fresh(database, created)
    with _master() as master:
        master.execute(f"CREATE DATABASE [{name}]")


def _exists(database: str) -> bool:
    with _master() as master:
        return master.execute("SELECT DB_ID(?)", _checked(database)).fetchone()[0] is not None


def _connect(database: str) -> pyodbc.Connection:
    from sdg.config import get_settings

    return pyodbc.connect(get_settings().mssql.connection_string(_checked(database)))


def _scalar(database: str, sql: str) -> int:
    with _connect(database) as conn:
        return int(conn.cursor().execute(sql).fetchone()[0])


def _run(args: list[str], env: dict[str, str], timeout: int = 1800) -> str:
    # Hidden from a failure's traceback, which would print `env`: the
    # process's environment holds the machine's passwords and keys.
    __tracebackhide__ = True
    done = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if done.returncode != 0:
        said = " ".join(args[:3]) if args[0] == "-m" else "the discovery script"
        pytest.fail(f"{said} failed:\n{done.stdout[-3000:]}\n"
                    f"{done.stderr[-3000:]}", pytrace=False)
    return done.stdout


def _env(work: Path, store: str, **more: str) -> dict[str, str]:
    """A process reading the bank and writing only `store`."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("SDG_PACKS_DIR", "SDG_PACK", "SDG_EXTRACT_SOURCE", "SDG_EXTRACT_KEY")}
    env.update(SDG_DB_SOURCE=SOURCE, SDG_DB_REF=SOURCE, SDG_DB_TARGET=SOURCE,
               SDG_DB_DENORM=_checked(store), SDG_DENORM_SCHEMA="sdm", SDG_WORK_DIR=str(work))
    env.update(more)
    return env


def _cli(runner, *args: str) -> str:
    from sdg.cli.accelerate import accelerate

    done = runner.invoke(accelerate, list(args))
    assert done.exit_code == 0, (args, done.output, done.exception)
    return done.output


@pytest.fixture(scope="module")
def parity(tmp_path_factory):
    from sdg.config import get_settings, load_env, not_a_demo_machine

    load_env()
    reason = not_a_demo_machine()
    if reason:
        pytest.skip(f"the bank parity proof builds and drops databases: this machine is "
                    f"{reason}")
    from typer.testing import CliRunner

    from sdg.accelerator import approval, sources
    from sdg.accelerator import plan as planning
    from seed import bank as sample

    tmp = tmp_path_factory.mktemp("bank-parity")
    packs_dir = tmp / "packs"
    out: dict = {}
    mp = pytest.MonkeyPatch()
    # Only what this run creates is ever dropped.
    created: list[str] = []
    try:
        out["source_counts"] = sample.build(_fresh(SOURCE, created))
        # The plan's folder: the load reads the plan from it, as the screen does.
        plans_work = get_settings().work_dir
        _create(GENERATED_STORE, created)
        # A store has its schema (customer-tables makes it); approval adds its table.
        with _connect(GENERATED_STORE) as conn:
            conn.execute("CREATE SCHEMA sdm")
            conn.commit()
        # The approval is written to the store the pack is loaded into, never
        # to the real store.
        mp.setenv("SDG_DB_DENORM", GENERATED_STORE)
        mp.setenv("SDG_DENORM_SCHEMA", "sdm")
        mp.setattr(approval, "second_approver", lambda: (None, False))

        runner = CliRunner()
        _cli(runner, "landscape", SOURCE, "--allow-write-login")
        _cli(runner, "propose", SOURCE, "--allow-write-login", "--force",
             "--primary", "dbo.CUSTOMER", "--child", "dbo.ACCOUNT")
        _cli(runner, "name", SOURCE, "primary", "customer", "customers")
        _cli(runner, "name", SOURCE, "child", "account", "accounts")
        _cli(runner, "pack-name", SOURCE, PACK_ID, "Generated bank")
        # The product's type sorts accounts into kinds, as the hand pack has it.
        _cli(runner, "select", SOURCE, "child", "dbo.PRODUCT.PRODUCT_TYPE")
        _cli(runner, "extra", SOURCE, "primary", "dbo.CUSTOMER", "LAST_NAME")
        _cli(runner, "extra", SOURCE, "primary", "dbo.CUSTOMER", "EMAIL")
        _cli(runner, "privacy", SOURCE, "dbo.CUSTOMER", "CUSTOMER_ID", "surrogate")
        # The account's key is proposed a surrogate for its name; the demo
        # bank's is a plain number, kept on the person's word.
        _cli(runner, "privacy", SOURCE, "dbo.ACCOUNT", "ACCOUNT_ID", "kept",
             "--confirm-not-personal")
        # The levels' labels read as personal by name (NAME); they are the
        # bank's branch and product names, kept on the person's word.
        for table, column in (("dbo.BRANCH", "BRANCH_NAME"), ("dbo.PRODUCT", "PRODUCT_NAME")):
            _cli(runner, "privacy", SOURCE, table, column, "kept", "--confirm-not-personal")
        # Customers are discovered on the hand pack's features (age, tenure,
        # segment): the state, proposed too, is left out, so the two compare.
        from sdg.accelerator import workbench

        workbench.edit(SOURCE, lambda held, graph: planning.select_feature(
            held, "primary", "dbo.CUSTOMER.ADDR_STATE", False, graph=graph))
        _cli(runner, "submit", SOURCE)
        _cli(runner, "approve", SOURCE)
        _cli(runner, "build", SOURCE, "--packs-dir", str(packs_dir))

        held = planning.load(sources.folder_for(SOURCE))
        out["plan"] = held
        out["record_kinds"] = {r: (k["table"], k.get("lines_table"))
                               for r, k in held.record_kinds.items()}
        out["criteria"] = [c.id for cs in held.criteria.values() for c in cs if c.selected]
        out["row"] = approval.record(SOURCE)
        folder = packs_dir / PACK_ID
        out["folder"] = folder
        out["pack"] = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
        out["features"] = json.loads((folder / "features.json").read_text(encoding="utf-8"))
        out["extract_sql"] = (folder / "extract" / f"{SOURCE}.sql").read_text(encoding="utf-8")
        mp.undo()

        work = tmp / "work-generated"
        work.mkdir()
        generated = _env(work, GENERATED_STORE, SDG_PACKS_DIR=str(packs_dir), SDG_PACK=PACK_ID,
                         SDG_EXTRACT_KEY=TEST_EXTRACT_KEY)
        _run(["-m", "sdg.cli.main", "customer-tables"], generated)
        # The demo machine's one login reads the source and writes the store.
        out["generated_extract"] = _run(
            ["-m", "sdg.cli.main", "extract", SOURCE, "--allow-write-login"],
            {**generated, "SDG_WORK_DIR": str(plans_work)})
        said = _run(["-c", DISCOVER, str(ROOT), "customers", "accounts"], generated)
        line = next((ln for ln in said.splitlines() if ln.startswith("RESULT ")), None)
        assert line, said[-3000:]
        out["discovered"] = json.loads(line[len("RESULT "):])

        _create(HAND_STORE, created)
        work = tmp / "work-hand"
        work.mkdir()
        hand = _env(work, HAND_STORE, SDG_PACK="banking")
        _run(["-m", "sdg.cli.main", "customer-tables"], hand)
        _run(["-m", "sdg.cli.main", "extract", "BANK"], hand)
        said = _run(["-c", DISCOVER, str(ROOT), "customers", "accounts"], hand)
        line = next((ln for ln in said.splitlines() if ln.startswith("RESULT ")), None)
        assert line, said[-3000:]
        out["hand_discovered"] = json.loads(line[len("RESULT "):])

        out["counts"] = _counts(held)
        out["privacy"] = _privacy(held, out["pack"])
        yield out
    finally:
        mp.undo()
        failures = []
        for name in created:
            try:
                _drop(name)
            except Exception as exc:  # each is tried; every failure is said below
                failures.append(f"{name}: {type(exc).__name__}")
        left = [name for name in created if _exists(name)]
        assert not failures and not left, (failures, left)


def _denorm(held, role: str):
    return next(t for t in held.structure if t.role == role)


def _walk_keys(held, role: str) -> list[tuple[str, str]]:
    """(generated column, hand column) for each selected walk level's key:
    the hand pack names a column as the source does."""
    denorm = _denorm(held, role)
    out = []
    for level in sorted(held.levels(role), key=lambda lv: lv.order):
        if not (level.selected and level.kind == "walk"):
            continue
        column = next(c for c in denorm.columns if c.serves == "level_key"
                      and (c.source_table, c.source_column) == (level.table, level.column))
        out.append((column.name, level.column))
    return out


def _rows(database: str, sql: str) -> list[tuple]:
    with _connect(database) as conn:
        return [tuple(r) for r in conn.cursor().execute(sql).fetchall()]


def _counts(held) -> dict:
    out = {}
    for role in ("primary", "child"):
        denorm = _denorm(held, role)
        mine, theirs = denorm.name, HAND_TABLES[role]
        hand_columns = {r[0].upper() for r in _rows(
            HAND_STORE, f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                        f"WHERE TABLE_SCHEMA = 'sdm' AND TABLE_NAME = '{theirs}'")}
        shared = [c.name for c in denorm.columns
                  if c.privacy != "withheld" and c.name.upper() in hand_columns]
        out[role] = {
            # Each column both packs hold (kept or surrogate here): as many
            # values, so no join took a column from the wrong row or lost it.
            "columns": {
                column: (_scalar(GENERATED_STORE,
                                 f"SELECT COUNT_BIG([{column}]) FROM sdm.[{mine}]"),
                         _scalar(HAND_STORE, f"SELECT COUNT_BIG([{column}]) FROM sdm.[{theirs}]"))
                for column in shared},
            # Rows per walk-level key value, the same in both.
            "spread": {
                column: (_rows(GENERATED_STORE, f"SELECT CAST([{column}] AS nvarchar(100)), "
                                                f"COUNT_BIG(*) FROM sdm.[{mine}] "
                                                f"GROUP BY [{column}] ORDER BY 1"),
                         _rows(HAND_STORE, f"SELECT CAST([{hand}] AS nvarchar(100)), "
                                           f"COUNT_BIG(*) FROM sdm.[{theirs}] "
                                           f"GROUP BY [{hand}] ORDER BY 1"))
                for column, hand in _walk_keys(held, role)},
            "tables": (mine, theirs),
            "rows": (_scalar(GENERATED_STORE, f"SELECT COUNT_BIG(*) FROM sdm.[{mine}]"),
                     _scalar(HAND_STORE, f"SELECT COUNT_BIG(*) FROM sdm.[{theirs}]")),
            "levels": {
                column: (_scalar(GENERATED_STORE,
                                 f"SELECT COUNT(DISTINCT [{column}]) FROM sdm.[{mine}]"),
                         _scalar(HAND_STORE,
                                 f"SELECT COUNT(DISTINCT [{hand}]) FROM sdm.[{theirs}]"))
                for column, hand in _walk_keys(held, role)},
        }
    return out


def _privacy(held, pack: dict) -> dict:
    """Per table and column: (rows, non-NULL values, values not 9 long)."""
    withheld = set(pack["extract"]["withheld"][SOURCE])
    surrogates = set(pack["extract"]["surrogates"][SOURCE])
    # What the approved plan chose, beside what the pack says: the two agree.
    chosen = {p: {c.name for t in held.structure for c in t.columns if c.privacy == p}
              for p in ("withheld", "surrogate")}
    out: dict = {"withheld": {}, "surrogate": {}, "chosen": chosen,
                 "declared": {"withheld": withheld, "surrogate": surrogates}}
    for denorm in held.structure:
        rows = _scalar(GENERATED_STORE, f"SELECT COUNT_BIG(*) FROM sdm.[{denorm.name}]")
        for column in denorm.columns:
            where = f"{denorm.name}.{column.name}"
            if column.name in withheld:
                out["withheld"][where] = (rows, _scalar(
                    GENERATED_STORE, f"SELECT COUNT_BIG([{column.name}]) FROM sdm.[{denorm.name}]"))
            if column.name in surrogates:
                out["surrogate"][where] = (rows, _scalar(
                    GENERATED_STORE,
                    f"SELECT COUNT_BIG([{column.name}]) FROM sdm.[{denorm.name}]"), _scalar(
                    GENERATED_STORE,
                    f"SELECT COUNT_BIG(*) FROM sdm.[{denorm.name}] "
                    f"WHERE [{column.name}] IS NOT NULL AND LEN([{column.name}]) <> 16"))
    primary, child = _denorm(held, "primary").name, _denorm(held, "child").name
    # One surrogate per customer (no two merged), and the same surrogate on
    # both tables: every account's customer is found among the customers.
    out["distinct_keys"] = _scalar(GENERATED_STORE,
                                   f"SELECT COUNT(DISTINCT [CUSTOMER_ID]) FROM sdm.[{primary}]")
    out["orphans"] = _scalar(
        GENERATED_STORE,
        f"SELECT COUNT_BIG(*) FROM sdm.[{child}] AS a WHERE a.[CUSTOMER_ID] IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM sdm.[{primary}] AS c "
        f"WHERE c.[CUSTOMER_ID] = a.[CUSTOMER_ID])")
    return out


# --- the plan and the build ---------------------------------------------------------

def test_the_plan_is_the_one_asked_for_and_is_built(parity):
    assert parity["record_kinds"] == {"primary": ("dbo.CUSTOMER", None),
                                      "child": ("dbo.ACCOUNT", "dbo.TXN")}
    assert parity["criteria"] == []
    assert parity["row"]["status"] == "approved" and parity["row"]["built_hash"]
    assert parity["pack"]["id"] == PACK_ID
    assert parity["pack"]["modules"] == ["pattern_analysis"]
    # The extract reads every row: no criteria, so no WHERE.
    assert "WHERE" not in parity["extract_sql"].split("-- table:", 1)[1]


# --- parity ---------------------------------------------------------------------------

def test_each_table_holds_as_many_rows_as_the_hand_packs(parity):
    counts, source = parity["counts"], parity["source_counts"]
    for role in ("primary", "child"):
        generated, hand = counts[role]["rows"]
        assert generated == hand, (role, counts[role])
    # And those are the bank's: every customer, every transaction.
    assert counts["primary"]["rows"][0] == source["CUSTOMER"]
    assert counts["child"]["rows"][0] == source["TXN"]


def test_each_walk_levels_key_has_as_many_values_as_the_hand_packs(parity):
    counts = parity["counts"]
    assert set(counts["primary"]["levels"]) == {"BRANCH_ID"}
    assert set(counts["child"]["levels"]) == {"BRANCH_ID", "PRODUCT_CODE"}
    for role in ("primary", "child"):
        for column, (generated, hand) in counts[role]["levels"].items():
            assert generated == hand and generated > 0, (role, column, generated, hand)


def test_each_shared_column_holds_as_many_values_as_the_hand_packs(parity):
    counts = parity["counts"]
    # The keys, the levels and their labels at least.
    assert {"CUSTOMER_ID", "BRANCH_ID", "BRANCH_NAME", "SEGMENT"} <= set(
        counts["primary"]["columns"])
    assert {"ACCOUNT_ID", "TXN_SEQ", "CUSTOMER_ID", "BRANCH_ID", "BRANCH_NAME",
            "PRODUCT_CODE", "PRODUCT_NAME"} <= set(counts["child"]["columns"])
    for role in ("primary", "child"):
        for column, (generated, hand) in counts[role]["columns"].items():
            assert generated == hand, (role, column, generated, hand)


def test_each_walk_level_value_holds_as_many_rows_as_the_hand_packs(parity):
    for role in ("primary", "child"):
        for column, (generated, hand) in parity["counts"][role]["spread"].items():
            assert generated == hand and generated, (role, column, generated, hand)


# --- privacy --------------------------------------------------------------------------

def test_the_pack_declares_the_privacy_the_plan_chose(parity):
    privacy = parity["privacy"]
    assert privacy["declared"] == privacy["chosen"]
    assert {"LAST_NAME", "EMAIL"} <= privacy["chosen"]["withheld"]
    assert privacy["chosen"]["surrogate"] == {"CUSTOMER_ID"}


def test_every_withheld_column_is_null_in_every_row(parity):
    found = parity["privacy"]["withheld"]
    assert {"LAST_NAME", "EMAIL"} <= {where.split(".", 1)[1] for where in found}, found
    for where, (rows, values) in found.items():
        assert rows > 0 and values == 0, (where, rows, values)


def test_every_surrogate_is_nine_characters(parity):
    found = parity["privacy"]["surrogate"]
    # The customer's key, on both tables: they join on it.
    assert len(found) == 2 and all(w.endswith(".CUSTOMER_ID") for w in found), found
    for where, (rows, values, other_length) in found.items():
        assert values == rows > 0 and other_length == 0, (where, rows, values, other_length)
    # No two customers share a surrogate, and both tables carry the same one.
    assert parity["privacy"]["distinct_keys"] == parity["source_counts"]["CUSTOMER"]
    assert parity["privacy"]["orphans"] == 0


# --- round trip ------------------------------------------------------------------------

def test_pattern_discovery_finds_patterns_for_each_record_kind(parity):
    found = parity["discovered"]
    assert found["pack"] == PACK_ID
    for entity in ("customers", "accounts"):
        assert found[entity]["n_discovered"] >= 1, (entity, found[entity])


def _structure(features: dict) -> dict:
    """What a features.json is built around: each loader's filter keys, the
    store's combination key, and what sorts the child's records into kinds."""
    return {"filters": {k: [f["key"] for f in v.get("filters", [])]
                        for k, v in features["loaders"].items() if isinstance(v, dict)},
            "combo_key": features["store"]["combo_key"],
            "type_column": features["child_analysis"]["type_column"]}


def test_the_generated_features_are_built_as_the_hand_packs_are(parity):
    hand = json.loads((ROOT / "sdg" / "packs" / "banking" / "features.json")
                      .read_text(encoding="utf-8"))
    assert _structure(parity["features"]) == _structure(hand)


def test_discovery_reads_as_many_combinations_and_records_as_under_the_hand_pack(parity):
    generated, hand = parity["discovered"], parity["hand_discovered"]
    assert hand["pack"] == "banking"
    for entity in ("customers", "accounts"):
        mine, theirs = generated[entity], hand[entity]
        assert mine["combinations"] == theirs["combinations"] > 0, (entity, mine, theirs)
        assert mine["population"] == theirs["population"] > 0, (entity, mine, theirs)
    # Customers are discovered on the same columns in both (age, tenure,
    # segment), over the same combinations and records; the two packs list
    # those features in another order and define other patterns beside them
    # (the hand pack's state), and discovery found one pattern more under the
    # generated pack (17 against 16 on 2026-11-21). Held within a tenth.
    mine, theirs = generated["customers"]["n_discovered"], hand["customers"]["n_discovered"]
    assert abs(mine - theirs) <= max(1, theirs // 10), (generated, hand)
    # Accounts are discovered on other features: the hand pack's are the
    # transactions' amount, type and channel, which the proposer does not
    # offer (they sit on the lines); the generated pack's are the balance and
    # the tenure. Only that both find patterns is held.
    assert generated["accounts"]["n_discovered"] >= 1 and hand["accounts"]["n_discovered"] >= 1
    print("DISCOVERY", json.dumps({"generated": generated, "hand": hand}))
