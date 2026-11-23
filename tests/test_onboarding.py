"""Onboarding a database, empty or not, and offboarding it.

An empty database is planned from the pattern store and its own column types:
the store says how many claims a member has and that a claim's total is its
lines added up, and the column's type says whether the store's values can be
stored there at all. Offboarding forgets a database's working state and never
touches patterns.
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdg.discovery.model import Column
from sdg.landscape.project import fits

ROOT = Path(__file__).resolve().parent.parent


def _known(**kw):
    base = dict(values=None, minimum=None, maximum=None, max_length=None)
    return SimpleNamespace(**{**base, **kw})


def _column(type_name, **kw):
    return Column(name="c", ordinal=1, type_name=type_name, **kw)


def test_a_value_the_column_cannot_hold_rules_out_the_store():
    names = _known(values=[{"value": "Christopher"}, {"value": "Ann"}], max_length=11)
    assert fits(_column("varchar", max_length=20), names) is None
    assert "11 characters" in fits(_column("varchar", max_length=4), names)

    codes = _known(values=[{"value": "F"}, {"value": "M"}])
    assert "not a number" in fits(_column("int"), codes)

    big = _known(minimum=0, maximum=70_000)
    assert "smallint" in fits(_column("smallint"), big)
    assert fits(_column("int"), big) is None

    money = _known(minimum=5.0, maximum=25_000.0)
    assert fits(_column("decimal", precision=12, scale=2), money) is None
    assert "digits" in fits(_column("decimal", precision=5, scale=2), money)

    days = _known(minimum="2019-01-01", maximum="2026-06-30")
    assert fits(_column("date"), days) is None
    assert "not a date" in fits(_column("date"), _known(values=[{"value": "A1"}]))


@pytest.mark.db
def test_offboarding_forgets_a_database_and_refuses_while_its_rows_remain(tmp_path):
    from sdg.api.service import Service, ServiceError

    service = Service(work_dir=tmp_path)
    home = service._working_key()
    (tmp_path / "physical-model.json").write_text("{}", encoding="utf-8")
    (tmp_path / "landscape-FACETS_QA.json").write_text("{}", encoding="utf-8")

    service.set_target("MDM_REF")
    (tmp_path / "physical-model.json").write_text("{}", encoding="utf-8")
    other = service._working_key()
    assert {d["key"] for d in service.onboarded()} == {home, other}

    # Rows written into it and not cleaned up: refused.
    (tmp_path / "run-manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ServiceError, match="still holds rows"):
        service.offboard(other)
    (tmp_path / "run-manifest.json").unlink()

    # The one being worked on: work moves back to the configured database.
    result = service.offboard(other)
    assert result["workingOn"]["database"] == service.settings.target_db
    assert not (tmp_path / "databases" / other).exists()
    assert (tmp_path / "physical-model.json").exists(), "the configured database's files did not come back"
    assert (tmp_path / "landscape-FACETS_QA.json").exists(), "the pattern store's description was touched"
    assert [d["key"] for d in service.onboarded()] == [home]


@pytest.mark.db
def test_offboarding_removes_only_a_database_listed_as_onboarded(tmp_path):
    """The key names the folder removed: anything not listed as onboarded --
    nothing, "..", a path -- is refused, and no folder is touched."""
    from sdg.api.service import Service, ServiceError

    service = Service(work_dir=tmp_path)
    (tmp_path / "physical-model.json").write_text("{}", encoding="utf-8")
    shelved = tmp_path / "databases" / "OTHERHOST__OTHERDB"
    shelved.mkdir(parents=True)
    (shelved / "physical-model.json").write_text("{}", encoding="utf-8")
    (shelved / "run-manifest.json").write_text("{}", encoding="utf-8")
    for key in ("", ".", "..", "../..", "databases", "OTHERHOST__OTHERDB/..", str(tmp_path)):
        with pytest.raises(ServiceError):
            service.offboard(key)
    assert (shelved / "run-manifest.json").exists(), "a manifest went with the folder"
    assert (tmp_path / "physical-model.json").exists()

    # Listed, and its rows cleaned up: removed.
    (shelved / "run-manifest.json").unlink()
    service.offboard("OTHERHOST__OTHERDB")
    assert not shelved.exists() and (tmp_path / "databases").exists()


@pytest.mark.db
def test_an_empty_database_is_onboarded_from_the_pattern_store(tmp_path):
    """Facets tables, reference rows only, one column too narrow for the store."""
    import pyodbc

    from sdg.api.service import DEFAULT_SETTINGS, Service
    from sdg.config import get_settings
    from sdg.landscape import derive, save
    from seed.build import run_sql_script

    name = "SDM_TEST_EMPTY_FACETS"
    settings = get_settings()
    master = pyodbc.connect(settings.mssql.connection_string("master"), autocommit=True)

    def drop():
        if master.execute("SELECT DB_ID(?)", name).fetchone()[0]:
            master.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            master.execute(f"DROP DATABASE [{name}]")

    drop()
    master.execute(f"CREATE DATABASE [{name}]")
    try:
        conn = pyodbc.connect(settings.mssql.connection_string(name), autocommit=False)
        run_sql_script(conn, ROOT / "seed" / "ddl_facets.sql")
        cur = conn.cursor()
        order = ["dbo.CMC_IDCD_DIAG_CODE", "dbo.CMC_IPCD_PROC_CODE", "dbo.CMC_PRPR_PROV",
                 "dbo.CMC_NWNW_NETWORK", "dbo.CMC_NWPR_RELATION", "dbo.CMC_NWST_NET_SET",
                 "dbo.CMC_PDPD_PRODUCT", "dbo.CMC_PDDS_PROD_DESC", "dbo.CMC_GRGR_GROUP",
                 "dbo.CMC_SGSG_SUBGROUP", "dbo.CMC_CSPD_CAT_DESC", "dbo.CMC_CSPI_CS_PLAN",
                 "dbo.CMC_PLDS_PLAN_DESC"]
        assert set(order) == {t for t, m in DEFAULT_SETTINGS["modes"].items() if m == "reuse"}
        for table in order:
            cols = [r[0] for r in cur.execute(
                "SELECT name FROM sys.columns WHERE object_id = OBJECT_ID(?) "
                "AND is_computed = 0 ORDER BY column_id", f"{name}.{table}").fetchall()]
            names = ", ".join(f"[{c}]" for c in cols)
            sql = f"INSERT INTO {name}.{table} ({names}) SELECT {names} FROM {settings.source_db}.{table}"
            if cur.execute("SELECT OBJECTPROPERTY(OBJECT_ID(?), 'TableHasIdentity')",
                           f"{name}.{table}").fetchone()[0]:
                sql = f"SET IDENTITY_INSERT {name}.{table} ON; {sql}; SET IDENTITY_INSERT {name}.{table} OFF"
            cur.execute(sql)
        cur.execute("ALTER TABLE dbo.CMC_MEME_MEMBER ALTER COLUMN MEME_FIRST_NAME varchar(4) NULL")
        conn.commit()
        conn.close()

        service = Service(work_dir=tmp_path)
        save(derive(source=settings.source_db, catalog=service.catalog, registry=service.registry),
             tmp_path / f"landscape-{settings.source_db}.json")
        service.write_settings({"seed": "onboarding-suite"})
        service.set_target(name)
        service.discover()
        service.profile()
        mapped = service.map()
        assert "dbo.CMC_CLCL_CLAIM.MEME_CK -> dbo.CMC_MEME_MEMBER" in mapped["keysFromLandscape"]
        service.review()
        plan, _graph, _extra = service.build()
        assert not [w for w in plan.warnings if w.severity == "block"]
        total = plan.table("dbo.CMC_CLCL_CLAIM").field("CLCL_TOT_CHG")
        assert total.generator == "expression", "a claim's total is not its lines added up"

        service.generate()
        assert service.validate()["passed"]
        check = pyodbc.connect(settings.mssql.connection_string(name))
        counts = {t: check.execute(f"SELECT COUNT(*) FROM dbo.{t}").fetchone()[0]
                  for t in ("CMC_SBSB_SUBSC", "CMC_MEME_MEMBER", "CMC_CLCL_CLAIM", "CMC_CDML_CL_LINE")}
        longest = check.execute("SELECT MAX(LEN(MEME_FIRST_NAME)) FROM dbo.CMC_MEME_MEMBER").fetchone()[0]
        check.close()
        assert 2.0 < counts["CMC_MEME_MEMBER"] / counts["CMC_SBSB_SUBSC"] < 2.6
        assert 4.0 < counts["CMC_CLCL_CLAIM"] / counts["CMC_MEME_MEMBER"] < 5.2
        assert 2.8 < counts["CMC_CDML_CL_LINE"] / counts["CMC_CLCL_CLAIM"] < 3.4
        assert longest <= 4
        service.cleanup()
    finally:
        drop()


def test_two_tables_holding_the_same_keys_get_one_key_between_them():
    """MaskBench: parcheck and seqcheck each hold the same 400 member ids, so
    each looked like the other's parent -- a cycle nothing could load."""
    from sdg.discovery.inference import InferredKey, one_way
    from sdg.discovery.model import PhysicalModel, Table

    def table(name, rows):
        return Table(name=name, schema_name="dbo", table_name=name.split(".")[1], row_count=rows,
                     columns=[Column(name="member_id", ordinal=1, type_name="varchar",
                                     is_primary_key=True)])

    model = PhysicalModel(tables=[
        table("dbo.parcheck", 400), table("dbo.seqcheck", 400), table("dbo.member", 100_000)])

    def key(child, parent):
        return InferredKey(child_table=child, child_column="member_id", parent_table=parent,
                           parent_column="member_id", coverage=1.0, distinct_values=400,
                           missing=0, shared_tokens=["member", "id"])

    kept = one_way(model, [
        key("dbo.parcheck", "dbo.seqcheck"), key("dbo.seqcheck", "dbo.parcheck"),
        key("dbo.parcheck", "dbo.member"), key("dbo.member", "dbo.parcheck"),
    ])
    pairs = {(k.child_table, k.parent_table) for k in kept}
    assert pairs == {("dbo.seqcheck", "dbo.parcheck"), ("dbo.parcheck", "dbo.member")}
