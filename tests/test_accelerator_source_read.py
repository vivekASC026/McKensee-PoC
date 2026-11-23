"""The accelerator only ever reads a source.

A source is a customer's database, often production. Every statement the
accelerator sends -- its own, and every one discovery and profiling send for
it -- passes source_read's check, which lets through one statement that starts
with SELECT or WITH and names nothing that writes. These tests read a small
made-up source through a registry that answers from memory and records every
statement it was sent. No database is needed.
"""

from __future__ import annotations

import pytest

from sdg.accelerator import landscape, source_read, sources
from sdg.accelerator.source_read import NotARead, refuse_unless_read
from sdg.discovery import metadata
from sdg.discovery.connections import ConnectionRegistry
from sdg.discovery.model import Column, Table


def _tables():
    return [{"schema_name": "dbo", "table_name": t, "row_count": n}
            for t, n in (("KIND", 4), ("PERSON", 300), ("EVENT", 1200))]


def _columns():
    spec = {
        "KIND": [("KIND_CD", "char", 2), ("KIND_DESC", "varchar", 40)],
        "PERSON": [("PERSON_ID", "int", 4), ("FIRST_NAME", "varchar", 30),
                   ("BIRTH_DT", "date", 3), ("KIND_CD", "char", 2)],
        "EVENT": [("EVENT_ID", "int", 4), ("PERSON_ID", "int", 4), ("EFF_DT", "date", 3),
                  ("TERM_DT", "date", 3), ("AMOUNT", "decimal", 9)],
    }
    rows = []
    for table, columns in spec.items():
        for ordinal, (name, type_name, length) in enumerate(columns, start=1):
            rows.append({
                "schema_name": "dbo", "table_name": table, "column_name": name,
                "column_id": ordinal, "type_name": type_name, "max_length": length,
                "precision": 12 if type_name == "decimal" else 0,
                "scale": 2 if type_name == "decimal" else 0, "is_nullable": False,
                "is_identity": False, "is_computed": False, "computed_definition": None,
                "default_definition": None, "collation_name": None,
            })
    return rows


def _indexes():
    return [{"schema_name": "dbo", "table_name": t, "index_name": f"PK_{t}",
             "is_primary_key": True, "key_ordinal": 1, "column_name": c}
            for t, c in (("KIND", "KIND_CD"), ("PERSON", "PERSON_ID"), ("EVENT", "EVENT_ID"))]


def _foreign_keys():
    return [{"constraint_name": "FK_PERSON_KIND", "child_schema": "dbo", "child_table": "PERSON",
             "child_column": "KIND_CD", "parent_schema": "dbo", "parent_table": "KIND",
             "parent_column": "KIND_CD", "constraint_column_id": 1}]


class _Answering(ConnectionRegistry):
    """Answers the structure queries for three tables; every other statement
    is recorded and answered with no rows."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: list[str] = []

    def query(self, name, sql, params=(), *, timeout=0):
        self.sent.append(sql)
        answers = {
            metadata.TABLES_SQL: _tables, metadata.COLUMNS_SQL: _columns,
            metadata.INDEXES_SQL: _indexes, metadata.FOREIGN_KEYS_SQL: _foreign_keys,
        }
        return answers[sql]() if sql in answers else []


class _FakeSource(sources.SourceRegistry, _Answering):
    """A SourceRegistry whose statements, once checked, reach _Answering
    instead of a database."""


def _fake() -> _FakeSource:
    registry = _FakeSource(reads_production=True)
    registry.register_source(*sources.source_settings("SDMT_FAKE_SRC"))
    return registry


def test_reading_a_source_sends_only_reads():
    from sdg.config import get_settings

    fake = _fake()
    read = landscape.read("SDMT_FAKE_SRC", registry=fake)
    assert read.summary["tables"] == 3
    assert read.summary["declared_keys"] == 1
    # The structure queries and the profile's own were all sent.
    assert len(fake.sent) > 7
    assert any("COUNT" in sql for sql in fake.sent), "the profile measured the tables"
    for sql in fake.sent:
        refuse_unless_read(sql)
        assert source_read._skeleton(sql).lstrip().upper().startswith(("SELECT", "WITH"))
    # Nothing names the store: the source registry cannot even open it.
    schema = get_settings().denorm_schema.lower()
    assert not [sql for sql in fake.sent
                if f"[{schema}]" in sql.lower() or f"{schema}." in sql.lower()]


def test_discovery_and_profiling_are_checked_too(monkeypatch):
    """A writing statement from inside discovery is refused before it is sent."""
    fake = _fake()
    monkeypatch.setattr(metadata, "TRIGGERS_SQL", "DELETE FROM dbo.EVENT")
    with pytest.raises(NotARead):
        landscape.read("SDMT_FAKE_SRC", registry=fake)
    assert "DELETE FROM dbo.EVENT" not in fake.sent


def test_a_plain_registry_is_not_taken():
    with pytest.raises(TypeError):
        landscape.read("SDMT_FAKE_SRC", registry=ConnectionRegistry())


@pytest.mark.parametrize("role", ["denorm", "target", "ref", "master", "landscape"])
def test_a_source_registry_opens_the_source_only(role):
    fake = _fake()
    with pytest.raises(sources.SourceError):
        fake.query(role, "SELECT 1")
    with pytest.raises(sources.SourceError):
        fake.connect(role)
    assert fake.sent == []


@pytest.mark.parametrize("sql", [
    "INSERT INTO dbo.T VALUES (1)",
    "insert dbo.T select 1",
    "CREATE TABLE dbo.T (A int)",
    "EXEC sp_who",
    "EXECUTE ('SELECT 1')",
    "UPDATE dbo.T SET A = 1",
    "DELETE dbo.T",
    "DROP TABLE dbo.T",
    "TRUNCATE TABLE dbo.T",
    "MERGE dbo.T AS t USING dbo.U AS u ON 1 = 1 WHEN MATCHED THEN DELETE;",
    "SELECT 1; DROP TABLE dbo.T",
    "SELECT 1;SELECT 2",
    "SELECT 1 DROP TABLE dbo.T",
    "SELECT * INTO dbo.COPY FROM dbo.T",
    "WITH x AS (SELECT 1 AS a) DELETE FROM dbo.T",
    "WITH x AS (SELECT 1 AS a) UPDATE dbo.T SET A = 1",
    "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')",
    "-- SELECT\nDROP TABLE dbo.T",
    "/* SELECT */ DROP TABLE dbo.T",
    "/* nested /* SELECT */ still a comment */ DROP TABLE dbo.T",
    "SELECT 1 /* never closed",
    "SELECT 'never closed",
    "SELECT [never closed",
    "",
    "   ",
    "SELECTION",
    "SELECT 1 WAITFOR DELAY '00:00:05'",
    "SELECT 1 SET NOCOUNT ON",
    "SELECT 1 DECLARE @x int",
    "SELECT 1 BEGIN TRAN",
    "SELECT 1 COMMIT",
    "SELECT * FROM dbo.T WITH (UPDLOCK)",
    "SELECT * FROM dbo.T WITH (XLOCK, HOLDLOCK)",
    "SELECT * FROM dbo.T WITH (TABLOCKX)",
    "SELECT * FROM dbo.T WITH (PAGLOCK)",
    "SELECT NEXT VALUE FOR dbo.S",
    "SELECT next  value\nfor dbo.S",
    "SELECT 1 RAISERROR('x', 16, 1)",
    "SELECT 1 THROW 50000, 'x', 1",
    "SELECT 1 PRINT 'x'",
    "SELECT 1 -- a note\rDROP TABLE dbo.T",
    "SELECT 1 -- a note\x85DROP TABLE dbo.T",
    "SELECT 1 -- a note\u2028DROP TABLE dbo.T",
    "SELECT 1 -- a note\u2029DROP TABLE dbo.T",
    # SQL Server reads @, # and $ inside a name: a command glued to one.
    "SELECT 1 EXEC@r",
    "SELECT 1 DROP#x",
    "SELECT 1 SET@x = 1",
    "SELECT 1 EXEC$x",
    "SELECT 1 IF 1 = 1 SELECT 2",
    "SELECT 1 WHILE 1 = 1 SELECT 2",
    "SELECT 1 GOTO done",
])
def test_anything_but_one_read_is_refused(sql):
    fake = _fake()
    with pytest.raises(NotARead):
        source_read.query(fake, sql)
    with pytest.raises(NotARead):
        fake.query(sources.SOURCE, sql)
    assert fake.sent == []


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "  select 1;",
    "-- what this reads\nSELECT [UPDATE], [DROP TABLE] FROM dbo.T WHERE A = 'DELETE; DROP'",
    "/* a note */ WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT COUNT(*) AS n FROM dbo.T WHERE NAME = 'it''s; INSERT'",
    'SELECT "INSERT" FROM dbo.T',
    "SELECT UPDATED_AT, DELETED_FLAG, INTO_COUNT FROM dbo.T",
])
def test_a_read_goes_through(sql):
    fake = _fake()
    assert source_read.query(fake, sql) == []
    assert fake.sent == [sql]


def test_pair_counts_names_only_the_tables_own_columns():
    table = Table(name="dbo.T", schema_name="dbo", table_name="T", row_count=10,
                  columns=[Column(name="A", ordinal=1, type_name="int"),
                           Column(name="B]x", ordinal=2, type_name="int")])
    fake = _fake()
    assert source_read.pair_counts(fake, table, "A", "B]x") == (0, 0, 0)
    (sent,) = fake.sent
    assert "[B]]x]" in sent and "[dbo].[T]" in sent
    # One pass: the rows holding both, and the pair keyed as the profile keys it.
    assert "COUNT_BIG(*) AS rows_both" in sent
    assert "CONCAT([A], CHAR(1), [B]]x])" in sent
    with pytest.raises(ValueError):
        source_read.pair_counts(fake, table, "A", "NOT_THERE")


@pytest.mark.parametrize("name", ["", "1ABC", "A-B", "A B", "A" * 33, "A;DROP", "..\\X"])
def test_a_source_name_is_a_plain_name(name):
    with pytest.raises(sources.SourceError):
        sources.source_settings(name)


def test_source_settings_fall_back_to_the_configured_server(monkeypatch):
    from sdg.config import get_settings

    base = get_settings().mssql
    settings, database = sources.source_settings("sdmt_fake_src")
    assert database == "SDMT_FAKE_SRC"
    assert (settings.server, settings.port, settings.driver) == (base.server, base.port,
                                                                 base.driver)
    monkeypatch.setenv("SDG_SOURCE_SDMT_FAKE_SRC_DATABASE", "OTHER_DB")
    monkeypatch.setenv("SDG_SOURCE_SDMT_FAKE_SRC_SERVER", "elsewhere")
    monkeypatch.setenv("SDG_SOURCE_SDMT_FAKE_SRC_PORT", "1500")
    monkeypatch.setenv("SDG_SOURCE_SDMT_FAKE_SRC_TRUSTED", "yes")
    settings, database = sources.source_settings("SDMT_FAKE_SRC")
    assert (database, settings.server, settings.port, settings.trusted) == (
        "OTHER_DB", "elsewhere", 1500, True)
    monkeypatch.setenv("SDG_SOURCE_SDMT_FAKE_SRC_PORT", "not a port")
    with pytest.raises(sources.SourceError):
        sources.source_settings("SDMT_FAKE_SRC")


def test_the_shortlist_takes_each_chosen_tables_parents():
    from sdg.discovery.model import ForeignKey, PhysicalModel

    def table(name, rows, parents=()):
        return Table(name=f"dbo.{name}", schema_name="dbo", table_name=name, row_count=rows,
                     foreign_keys=[ForeignKey(columns=["K"], parent_table=f"dbo.{p}",
                                              parent_columns=["K"]) for p in parents])

    model = PhysicalModel(tables=[table("BIG", 1000, ["SMALL"]), table("SMALL", 3),
                                  table("MID", 500), table("EMPTY", 0)])
    assert landscape.shortlist(model, 1) == ["dbo.BIG", "dbo.SMALL"]
    assert "dbo.EMPTY" not in landscape.shortlist(model, 10)


def test_personal_values_are_withheld_against_the_base_catalog():
    from sdg.catalog.loader import load_catalog
    from sdg.profiling.model import ColumnProfile, ProfileReport, TopValue
    from sdg.profiling.personal import withhold_personal_values

    catalog = load_catalog(landscape.BASE_CATALOG)
    names = ["CUST_FIRST_NAME", "LAST_NAME", "MID_INIT", "BIRTH_DT", "SSN", "HOME_PHONE",
             "EMAIL_ADDR", "ADDR1", "ADDR_CITY", "ZIP_CODE"]
    kept = ["STATUS", "BRANCH_STATE", "EFF_DT", "AMOUNT"]
    report = ProfileReport(columns=[
        ColumnProfile(table="dbo.T", column=c, minimum="x", maximum="y",
                      top_values=[TopValue(value="v", count=1, share=1.0)])
        for c in names + kept])
    cleared = withhold_personal_values(report, catalog)
    assert sorted(cleared) == sorted(f"dbo.T.{c}" for c in names)
    assert all(report.column("dbo.T", c).top_values for c in kept)


# --- the registry, the login, the folder ---------------------------------

def test_a_source_registry_hands_out_no_connection_and_takes_no_other_role():
    fake = _fake()
    with pytest.raises(sources.SourceError):
        fake.connect(sources.SOURCE)
    with pytest.raises(sources.SourceError):
        fake.register("denorm", "ANY_DB")
    with pytest.raises(sources.SourceError):
        fake.register_connection(sources.SOURCE, *sources.source_settings("SDMT_FAKE_SRC"))
    assert fake.sent == []


class _Timed(sources.SourceRegistry, _Answering):
    pass


def test_every_statement_has_a_time_limit():
    timed = _Timed(reads_production=True)
    timed.register_source(*sources.source_settings("SDMT_FAKE_SRC"))
    seen = []
    original = _Answering.query

    def recording(self, name, sql, params=(), *, timeout=0):
        seen.append(timeout)
        return original(self, name, sql, params, timeout=timeout)

    _Answering.query = recording
    try:
        timed.query(sources.SOURCE, "SELECT 1")
        timed.query(sources.SOURCE, "SELECT 1", timeout=5)
    finally:
        _Answering.query = original
    assert seen == [sources.DEFAULT_TIMEOUT, 5]


def test_a_customer_machine_needs_the_sources_own_login(monkeypatch):
    from sdg.config import get_settings

    monkeypatch.setattr(sources, "not_a_demo_machine", lambda: "set up as a customer's (x)")
    for part in ("USER", "PASSWORD", "TRUSTED"):
        monkeypatch.delenv(f"SDG_SOURCE_SDMT_FAKE_SRC_{part}", raising=False)
    monkeypatch.delenv("SDG_SOURCE_SDMT_FAKE_SRC_SERVER", raising=False)
    with pytest.raises(sources.SourceError, match="SDG_SOURCE_SDMT_FAKE_SRC_USER"):
        sources.source_settings("SDMT_FAKE_SRC")
    monkeypatch.setenv("SDG_SOURCE_SDMT_FAKE_SRC_TRUSTED", "no")
    with pytest.raises(sources.SourceError, match="SDG_SOURCE_SDMT_FAKE_SRC_USER"):
        sources.source_settings("SDMT_FAKE_SRC")
    monkeypatch.delenv("SDG_SOURCE_SDMT_FAKE_SRC_TRUSTED")
    monkeypatch.setenv("SDG_SOURCE_SDMT_FAKE_SRC_USER", "reader_login")
    # The source's own server too: its login is never sent to the store's.
    with pytest.raises(sources.SourceError, match="SDG_SOURCE_SDMT_FAKE_SRC_SERVER"):
        sources.source_settings("SDMT_FAKE_SRC")
    monkeypatch.setenv("SDG_SOURCE_SDMT_FAKE_SRC_SERVER", "source-host")
    for part in ("TRUST_CERT", "ENCRYPT"):
        monkeypatch.delenv(f"SDG_SOURCE_SDMT_FAKE_SRC_{part}", raising=False)
    settings, _ = sources.source_settings("SDMT_FAKE_SRC")
    assert settings.user == "reader_login" and not settings.trusted
    assert settings.server == "source-host"
    assert settings.encrypt and not settings.trust_cert
    # The store's password is never borrowed for the source's login.
    base = get_settings().mssql
    assert settings.password == "" or settings.password != base.password
    monkeypatch.delenv("SDG_SOURCE_SDMT_FAKE_SRC_USER")
    monkeypatch.setenv("SDG_SOURCE_SDMT_FAKE_SRC_TRUSTED", "yes")
    settings, _ = sources.source_settings("SDMT_FAKE_SRC")
    assert settings.trusted and settings.user == ""


class _Login(sources.SourceRegistry, _Answering):
    """Answers the login question with the permissions it was given."""

    def __init__(self, can, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.can = can

    def query(self, name, sql, params=(), *, timeout=0):
        rows = super().query(name, sql, params, timeout=timeout)
        if sql == sources.LOGIN_SQL:
            return [{sources.permission_alias(p): int(p in self.can)
                     for p in sources.WRITE_PERMISSIONS}]
        return rows


def _login(can) -> _Login:
    registry = _Login(can, reads_production=True)
    registry.register_source(*sources.source_settings("SDMT_FAKE_SRC"))
    return registry


def test_a_login_that_can_write_is_refused_unless_allowed():
    reader = _login(set())
    assert sources.check_login(reader) == {
        "permissions": {p: False for p in sources.WRITE_PERMISSIONS},
        "can_write": False, "allowed_write_login": False}
    (sent,) = reader.sent
    refuse_unless_read(sent)
    writer = _login({"INSERT", "ALTER"})
    with pytest.raises(sources.SourceError, match="--allow-write-login"):
        sources.check_login(writer)
    held = sources.check_login(writer, allow_write_login=True)
    assert held["can_write"] and held["allowed_write_login"]
    assert held["permissions"]["INSERT"] and not held["permissions"]["DELETE"]
    with pytest.raises(sources.SourceError):
        sources.check_login(_fake())  # no answer: not read


@pytest.mark.parametrize("granted", ["CREATE TABLE", "EXECUTE", "CONTROL", sources.ON_A_TABLE])
def test_a_login_that_can_write_anywhere_is_refused(granted):
    """A grant on one table, or to create, run or control, is as much a write
    as a grant on the database."""
    with pytest.raises(sources.SourceError, match="--allow-write-login"):
        sources.check_login(_login({granted}))


def test_the_login_question_is_one_read_asking_every_table():
    sql = sources.LOGIN_SQL
    refuse_unless_read(sql)
    assert sql.count("SELECT") == 2 and "EXISTS (SELECT 1 FROM sys.tables" in sql
    for permission in sources.TABLE_PERMISSIONS:
        assert f"'OBJECT', '{permission}'" in sql
    for permission in sources.DATABASE_PERMISSIONS:
        assert f"'DATABASE', '{permission}') AS {sources.permission_alias(permission)}" in sql
    assert set(sources.DATABASE_PERMISSIONS) >= {"INSERT", "UPDATE", "DELETE", "ALTER",
                                                 "CREATE TABLE", "EXECUTE", "CONTROL"}


def test_a_folder_that_is_a_link_is_refused(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    link = work / "accelerator"
    try:
        os.symlink(elsewhere, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("this account cannot make a link here")
        import _winapi

        _winapi.CreateJunction(str(elsewhere), str(link))
    monkeypatch.setattr(sources, "get_settings", lambda: SimpleNamespace(work_dir=work))
    with pytest.raises(sources.SourceError, match="link or a junction"):
        sources.folder_for("SDMT_FAKE_SRC")
    assert list(elsewhere.iterdir()) == []


def _work(tmp_path, monkeypatch):
    from types import SimpleNamespace

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(sources, "get_settings", lambda: SimpleNamespace(work_dir=work))
    return work / "accelerator" / "SDMT_FAKE_SRC"


def test_a_fresh_folder_is_made_owned_and_limited_before_it_is_handed_back(tmp_path, monkeypatch):
    import os

    folder = _work(tmp_path, monkeypatch)
    limited = []
    real_limit = sources._owner_only
    monkeypatch.setattr(sources, "_owner_only",
                        lambda path: (limited.append(path), real_limit(path)))
    if os.name != "nt":
        monkeypatch.setattr(sources.os, "chmod",
                            lambda path, mode, real=os.chmod: (limited.append(path),
                                                               real(path, mode)))
    # Made fresh: no ownership question, and empty when its access is set.
    assert sources.folder_for("SDMT_FAKE_SRC") == folder
    assert limited == [folder] and list(folder.iterdir()) == []
    assert sources._owned_by_this_account(folder)
    # There already and owned by this account: taken, and limited again.
    (folder / "proposal.json").write_text("{}", encoding="utf-8")
    assert sources.folder_for("SDMT_FAKE_SRC") == folder
    assert limited == [folder, folder]


def test_a_folder_another_account_owns_is_refused(tmp_path, monkeypatch):
    import os

    folder = _work(tmp_path, monkeypatch)
    folder.mkdir(parents=True)
    if os.name == "nt":
        # The Administrators group, not the signed-in account.
        monkeypatch.setattr(sources, "_owner_sid", lambda path: "S-1-5-32-544")
    else:
        monkeypatch.setattr(sources.os, "getuid", lambda: os.lstat(folder).st_uid + 1)
    monkeypatch.setattr(sources, "_owner_only",
                        lambda path: pytest.fail("access was set on a folder not owned"))
    with pytest.raises(sources.SourceError, match="does not own it"):
        sources.folder_for("SDMT_FAKE_SRC")
    assert list(folder.iterdir()) == []


def test_a_folder_turned_into_a_link_before_its_access_is_set_is_refused(tmp_path, monkeypatch):
    """The last check runs right before the access is set: a junction made
    after the folder was found is caught there, and nothing is set on
    wherever it points."""
    import os

    folder = _work(tmp_path, monkeypatch)
    folder.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def swapped(path):
        path.rmdir()
        try:
            os.symlink(elsewhere, path, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                pytest.skip("this account cannot make a link here")
            import _winapi

            _winapi.CreateJunction(str(elsewhere), str(path))
        return True

    monkeypatch.setattr(sources, "_owned_by_this_account", swapped)
    monkeypatch.setattr(sources, "_owner_only",
                        lambda path: pytest.fail("access was set through a link"))
    with pytest.raises(sources.SourceError, match="link or a junction"):
        sources.folder_for("SDMT_FAKE_SRC")
    assert list(elsewhere.iterdir()) == []


def test_the_saved_profile_keeps_no_value_outside_the_allowlist(tmp_path, monkeypatch):
    """A login, a full name and an account number: none of their values is
    saved, though the catalog's names alone would miss the account number.
    A short status code and a span's start date keep theirs."""
    from sdg.discovery.model import PhysicalModel
    from sdg.profiling.model import ColumnProfile, ProfileReport, TopValue

    def column(name, type_name, length=None, ordinal=1):
        return Column(name=name, ordinal=ordinal, type_name=type_name, max_length=length)

    model = PhysicalModel(tables=[Table(
        name="dbo.AUDIT_LOG", schema_name="dbo", table_name="AUDIT_LOG", row_count=500,
        columns=[column("CHANGED_BY", "varchar", 8), column("FULL_NAME", "varchar", 60, 2),
                 column("ACCOUNT_NO", "varchar", 12, 3), column("STATUS_CD", "char", 1, 4),
                 column("EFF_DT", "date", None, 5), column("BIRTH_DT", "date", None, 6)])])
    secrets = {"CHANGED_BY": ("jdoe1234", "low", 3), "FULL_NAME": ("Pat Q Example", "high", 480),
               "ACCOUNT_NO": ("000123456789", "unique", 500),
               "BIRTH_DT": ("1961-02-03", "low", 40),
               # One token each, so only a match inside the name finds them.
               "LASTNAME": ("Examplesson", "low", 4), "CREATEDBY": ("ops77", "low", 2)}
    kept = {"STATUS_CD": ("A", "low", 2), "EFF_DT": ("2020-01-01", "low", 12)}
    # Each value held by many rows, so only the names and kinds above decide
    # what is cleared (the count floor has its own test).
    report = ProfileReport(columns=[
        ColumnProfile(table="dbo.AUDIT_LOG", column=name, row_count=500, distinct_count=n,
                      cardinality=cardinality, minimum=value, maximum=value,
                      max_length=len(value), sequence={"min": value, "max": value, "step": 1},
                      fitted_format={"kind": "literal", "regex": value},
                      top_values=[TopValue(value=value, count=300, share=0.6)])
        for name, (value, cardinality, n) in {**secrets, **kept}.items()])
    model.tables[0].columns += [column("LASTNAME", "varchar", 10, 7),
                                column("CREATEDBY", "varchar", 8, 8),
                                column("PIN_NO", "int", None, 9)]
    # A birth date's oldest value, saved as a placeholder, and a mean and
    # spread: values too, unless the range is kept.
    birth = report.column("dbo.AUDIT_LOG", "BIRTH_DT")
    birth.placeholders = [{"value": "1888-08-08", "count": 1, "share": 0.002},
                          {"value": "9999-12-31", "count": 3, "share": 0.006}]
    report.columns.append(ColumnProfile(
        table="dbo.AUDIT_LOG", column="PIN_NO", row_count=1, distinct_count=1,
        cardinality="constant", minimum=482913, maximum=482913, mean=482913.0, stddev=0.0,
        top_values=[TopValue(value=482913, count=1, share=1.0)]))
    secrets["PIN_NO"] = ("482913", "constant", 1)
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    folder = landscape.save(landscape.Landscape("SDMT_FAKE_SRC", model, report, {}))
    text = (folder / landscape.PROFILE_FILE).read_text(encoding="utf-8")
    for name, (value, _, _) in secrets.items():
        assert value not in text, f"{name} kept {value}"
    assert "1888-08-08" not in text and "9999-12-31" in text
    saved = ProfileReport.load(folder / landscape.PROFILE_FILE)
    status = saved.column("dbo.AUDIT_LOG", "STATUS_CD")
    assert status.top_values and status.top_values[0].value == "A"
    start = saved.column("dbo.AUDIT_LOG", "EFF_DT")
    assert str(start.minimum) == "2020-01-01"


# --- every write word, by hand ---------------------------------------------

# Written out, not read from source_read.WRITING_WORDS: a word dropped from
# the code's list still fails here.
WRITE_WORDS = [
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER", "CREATE", "EXEC",
    "EXECUTE", "INTO", "GRANT", "REVOKE", "DENY", "BACKUP", "RESTORE", "DBCC", "BULK",
    "OPENROWSET", "OPENQUERY", "OPENDATASOURCE", "SHUTDOWN", "KILL", "RECONFIGURE",
    "WRITETEXT", "UPDATETEXT", "WAITFOR", "DISABLE", "ENABLE", "RECEIVE", "SEND",
    "CONVERSATION", "SET", "USE", "DECLARE", "BEGIN", "COMMIT", "ROLLBACK", "SAVE", "SETUSER",
    "REVERT", "CHECKPOINT", "READTEXT", "LOAD", "OPEN", "RENAME", "COPY", "ADD", "PRINT",
    "RAISERROR", "THROW", "UPDLOCK", "XLOCK", "TABLOCKX", "HOLDLOCK", "PAGLOCK", "IF", "WHILE",
    "GOTO",
]


@pytest.mark.parametrize("word", WRITE_WORDS)
def test_every_write_word_is_refused(word):
    for sql in (f"SELECT 1 {word} x", f"select 1 {word.lower()} x", f"SELECT 1 {word}@x",
                f"WITH c AS (SELECT 1 AS a) SELECT a FROM c {word} x"):
        with pytest.raises(NotARead):
            refuse_unless_read(sql)


@pytest.mark.parametrize("separator", ["\t", "\x0b", "\x0c", "\xa0", "\r\n", "\n"])
def test_any_separator_before_a_write_word_is_refused(separator):
    with pytest.raises(NotARead):
        refuse_unless_read(f"SELECT 1{separator}DROP TABLE dbo.T")
    with pytest.raises(NotARead):
        refuse_unless_read(f"SELECT 1{separator}INSERT dbo.T SELECT 2")


def test_a_write_word_against_a_bracket_is_refused():
    with pytest.raises(NotARead):
        refuse_unless_read("SELECT 1 DROP[T]")
    with pytest.raises(NotARead):
        refuse_unless_read("SELECT 1 DELETE[dbo].[T]")


def test_no_word_glued_to_a_variable_is_missed_but_variables_still_read():
    # A system function and a parameter marker are reads.
    refuse_unless_read("SELECT @@SERVERNAME AS s, DB_NAME() AS d WHERE 1 = ?")


# --- what is saved: no value planted anywhere survives --------------------

def _planted():
    """A small source with a value planted in every place one could hide: a
    check constraint, a default, a profile's check digit, a histogram's
    buckets, rare common values, a thin column, a range with no code name."""
    from sdg.discovery.model import CheckConstraint, ForeignKey, PhysicalModel
    from sdg.profiling.model import ColumnProfile, ProfileReport, TopValue

    def column(name, type_name, ordinal, length=None, **more):
        return Column(name=name, ordinal=ordinal, type_name=type_name, max_length=length,
                      **more)

    kind = Table(name="dbo.KIND", schema_name="dbo", table_name="KIND", row_count=4,
                 primary_key=["KIND_CD"],
                 columns=[column("KIND_CD", "char", 1, 2, is_primary_key=True,
                                 primary_key_ordinal=1),
                          column("KIND_DESC", "varchar", 2, 40)])
    person = Table(
        name="dbo.PERSON", schema_name="dbo", table_name="PERSON", row_count=500,
        primary_key=["PERSON_ID"],
        columns=[column("PERSON_ID", "int", 1, is_primary_key=True, primary_key_ordinal=1),
                 column("KIND_CD", "char", 2, 2,
                        check_definition="([KIND_CD] IN ('SNTLCHK1', 'B'))",
                        check_values=["SNTLCHK1", "B"]),
                 column("GRADE_CD", "char", 3, 2, default_definition="('SNTLDEF2')"),
                 column("TIER_CD", "char", 4, 2),
                 column("VISIT_DT", "date", 5),
                 column("SCORE", "int", 6),
                 column("CARD_NO", "varchar", 7, 16)],
        check_constraints=[CheckConstraint(name="CK_PERSON_KIND",
                                           definition="([KIND_CD] IN ('SNTLCHK3', 'B'))",
                                           column="KIND_CD", values=["SNTLCHK3", "B"])],
        foreign_keys=[ForeignKey(columns=["KIND_CD"], parent_table="dbo.KIND",
                                 parent_columns=["KIND_CD"])])
    model = PhysicalModel(tables=[kind, person])

    def profiled(name, **held):
        base = dict(table="dbo.PERSON", column=name, row_count=500, cardinality="low",
                    approximate_distinct=False)
        base.update(held)
        return ColumnProfile(**base)

    report = ProfileReport(columns=[
        profiled("PERSON_ID", distinct_count=500, distinct_ratio=1.0, cardinality="unique"),
        # A short code, kept -- but not its value only three rows hold.
        profiled("KIND_CD", distinct_count=2, max_length=1,
                 top_values=[TopValue(value="B", count=497, share=0.994),
                             TopValue(value="SNTLTOP4", count=3, share=0.006)]),
        # Forty values on 500 rows: under 20 rows each, so none is kept.
        profiled("GRADE_CD", distinct_count=40, max_length=2, minimum="SNTLMIN5",
                 maximum="SNTLMAX6",
                 top_values=[TopValue(value="SNTLTOP7", count=300, share=0.6)]),
        profiled("TIER_CD", distinct_count=3, max_length=1,
                 check_digit={"scheme": "luhn", "share": 1.0, "tested": 500,
                              "sample": "SNTLDIG8"}),
        # A date and a number with no code in their names keep no range.
        profiled("VISIT_DT", distinct_count=12, minimum="1931-07-07", maximum="1931-08-08"),
        profiled("SCORE", distinct_count=10, minimum=77713, maximum=77714,
                 distribution={"kind": "normal", "skew": 0.1, "min": 77713, "max": 77714,
                               "buckets": [{"value": "SNTLBKT9", "count": 3}]}),
        profiled("CARD_NO", distinct_count=500, cardinality="unique",
                 check_digit={"scheme": "SNTLDIG10", "share": 1.0, "tested": 500}),
    ])
    sentinels = ["SNTLCHK1", "SNTLDEF2", "SNTLCHK3", "SNTLTOP4", "SNTLMIN5", "SNTLMAX6",
                 "SNTLTOP7", "SNTLDIG8", "SNTLBKT9", "SNTLDIG10", "1931-07-07", "1931-08-08",
                 "77713", "77714"]
    return model, report, sentinels


def test_no_planted_value_reaches_any_saved_file(tmp_path, monkeypatch):
    from sdg.accelerator import plan

    model, report, sentinels = _planted()
    monkeypatch.setattr(sources, "folder_for", lambda name: tmp_path)
    # The summary made before anything is scrubbed, as read() would not.
    read = landscape.Landscape("SDMT_FAKE_SRC", model, report,
                               landscape.summary(model, report))
    folder = landscape.save(read)
    held = plan.propose(landscape.load("SDMT_FAKE_SRC"))
    plan.save(held, folder)
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    assert {p.name for p in files} >= {"model.json", "profile.json", "landscape.json",
                                       "proposal.json"}
    for path in files:
        text = path.read_text(encoding="utf-8")
        for sentinel in sentinels:
            assert sentinel not in text, f"{path.name} kept {sentinel}"
    # What is no value stays: the code's common value, the check digit's scheme.
    saved = landscape.load("SDMT_FAKE_SRC").profile
    assert [t.value for t in saved.column("dbo.PERSON", "KIND_CD").top_values] == ["B"]
    assert saved.column("dbo.PERSON", "TIER_CD").check_digit == {
        "scheme": "luhn", "share": 1.0, "tested": 500}
    assert saved.column("dbo.PERSON", "SCORE").distribution == {"kind": "normal", "skew": 0.1}
    assert saved.column("dbo.PERSON", "CARD_NO").check_digit is None


def test_a_session_setting_is_set_on_the_connection_not_in_the_statement(monkeypatch):
    """ConnectionRegistry.query runs a registry's own session settings on the
    fresh connection before the statement; the statement itself is unchanged."""
    executed = []

    class Cursor:
        description = None

        def execute(self, sql, *params):
            executed.append(sql)

    class Connection:
        timeout = 0

        def cursor(self):
            return Cursor()

        def close(self):
            pass

    import contextlib

    registry = _fake()

    @contextlib.contextmanager
    def connect(name, *, autocommit=False):
        yield Connection()

    monkeypatch.setattr(registry, "connect", connect)
    with registry.lock_timeout(5000):
        ConnectionRegistry.query(registry, sources.SOURCE, "SELECT 1")
    ConnectionRegistry.query(registry, sources.SOURCE, "SELECT 2")
    assert executed == ["SET LOCK_TIMEOUT 5000", "SELECT 1", "SELECT 2"]
    with pytest.raises(ValueError):
        with registry.lock_timeout("5000; DROP TABLE x"):
            pass

