"""A renamed source: the same structure under names the proposer has never seen.

docs/day0-accelerator-design.md §6: a copy of the demo source with every table
and column renamed must give the same record kinds and levels. seed/
renamed_source.py builds the copy, SDMT_RENAMED_SRC, with no foreign key
declared, and writes its rename map to tests/fixtures/accelerator/
renamed-map.json.

Fast part: a map is applied to each recorded model and profile in memory
(declared keys renamed with the rest) -- the kept map for the health source,
the seed's own rename for the bank -- and the proposal, its names mapped back,
must be the unrenamed one level for level (id, kind, selection, order, path),
with the same record kinds and tables: names alone decide nothing. Generic
tokens (ID, DT, NAME, TYPE, EFF, the base catalog's words) survive the rename
by design, so what this proves is that the source's own words decide nothing;
a name made only of generic tokens keeps its name in the bank's map.

Slow part (db, slow): the copy is built, read with `sdg accelerate landscape`
and proposed from with `propose`; with no declared key, the keys inference
finds must cover every join on the paths the unrenamed proposal took to the
expected levels.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdg.accelerator import plan
from sdg.accelerator.landscape import Landscape
from sdg.discovery.model import Column, PhysicalModel, Table
from sdg.profiling.model import ProfileReport
from tests.test_accelerator_recovery import EXPECTED, landscape_of, scores

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
MAP_PATH = FIXTURES / "renamed-map.json"
SOURCE = "facets_qa"
RENAMED = "SDMT_RENAMED_SRC"


def _names() -> dict[str, dict[str, str]]:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def _column_names(names: dict) -> dict[str, str]:
    """Old column name to new, whichever table holds it: the rename is
    token-wise, so a name is renamed the same everywhere."""
    out: dict[str, str] = {}
    for qualified, new in names["columns"].items():
        old = qualified.rsplit(".", 1)[1]
        assert out.setdefault(old, new) == new, f"{old} is renamed two ways"
    return out


def rename_model(model: PhysicalModel, names: dict) -> PhysicalModel:
    model = model.model_copy(deep=True)
    tables, columns = names["tables"], names["columns"]

    def col(table: str, name: str) -> str:
        return columns[f"{table}.{name}"]

    for table in model.tables:
        old = table.name
        for key in table.foreign_keys:
            key.columns = [col(old, c) for c in key.columns]
            key.parent_columns = [col(key.parent_table, c) for c in key.parent_columns]
            key.parent_table = tables[key.parent_table]
        for column in table.columns:
            column.name = col(old, column.name)
        table.primary_key = [col(old, c) for c in table.primary_key]
        for constraint in table.unique_constraints:
            constraint.columns = [col(old, c) for c in constraint.columns]
        for constraint in table.check_constraints:
            if constraint.column:
                constraint.column = col(old, constraint.column)
        table.indexed = [col(old, c) for c in table.indexed]
        table.name = tables[old]
        table.table_name = table.name.partition(".")[2]
    return model


def rename_profile(report: ProfileReport, names: dict) -> ProfileReport:
    report = report.model_copy(deep=True)
    tables, columns, by_name = names["tables"], names["columns"], _column_names(names)
    for held in report.columns:
        held.column = columns[f"{held.table}.{held.column}"]
        held.table = tables[held.table]
    for dep in report.dependencies:
        dep.determinant = columns[f"{dep.table}.{dep.determinant}"]
        dep.dependent = columns[f"{dep.table}.{dep.dependent}"]
        dep.table = tables[dep.table]
    def ordered(name: str) -> str:
        # A join ordering names its columns with their tables; a row one does not.
        table, _, column = name.rpartition(".")
        return f"{tables[table]}.{columns[name]}" if table else by_name[column]

    for order in report.orderings:
        order.earlier, order.later = ordered(order.earlier), ordered(order.later)
        order.table = tables[order.table]
        if order.parent_table:
            order.parent_table = tables[order.parent_table]
    for ratio in report.ratios:
        ratio.child_column = columns[f"{ratio.child_table}.{ratio.child_column}"]
        ratio.parent_column = columns[f"{ratio.parent_table}.{ratio.parent_column}"]
        ratio.child_table = tables[ratio.child_table]
        ratio.parent_table = tables[ratio.parent_table]
    for skipped in report.skipped:
        skipped.table = tables[skipped.table]
    report.table_seconds = {tables[t]: s for t, s in report.table_seconds.items()}
    return report


def _level_id(names: dict, level_id: str) -> str:
    """An unrenamed level id (TABLE.COLUMN), renamed."""
    table, _, column = level_id.rpartition(".")
    return f"{names['tables'][table]}.{names['columns'][f'{table}.{column}']}"


def _renamed_scores(held: plan.SourcePlan, names: dict, source: str = SOURCE) -> dict:
    return scores(held, EXPECTED[source], table=lambda t: names["tables"][t],
                  level=lambda i: _level_id(names, i))


def _map_for(source: str) -> dict:
    """The rename of a source's recorded model: the kept map for the health
    source (the one the seed builds the database from), the seed's own
    rename for the bank, not strict, since some bank names are made only of
    generic tokens and keep their names."""
    if source == SOURCE:
        return _names()
    from seed.renamed_source import rename_map

    return rename_map(PhysicalModel.load(FIXTURES / f"{source}-model.json"), strict=False)


class _Back:
    """Renamed names back to the source's own."""

    def __init__(self, names: dict) -> None:
        self.tables = {new: old for old, new in names["tables"].items()}
        self.columns = {(names["tables"][q.rpartition(".")[0]], new): q.rpartition(".")[2]
                        for q, new in names["columns"].items()}

    def table(self, name: str | None) -> str | None:
        return self.tables[name] if name else name

    def column(self, table: str, name: str) -> str:
        return self.columns[(table, name)]

    def level_id(self, level_id: str) -> str:
        table, _, column = level_id.rpartition(".")
        return f"{self.tables[table]}.{self.column(table, column)}"

    def join(self, join) -> tuple:
        return (self.table(join.from_table),
                tuple(self.column(join.from_table, c) for c in join.from_columns),
                self.table(join.to_table),
                tuple(self.column(join.to_table, c) for c in join.to_columns), join.kind)


def _levels(held: plan.SourcePlan, role: str, back: _Back | None = None) -> list[tuple]:
    out = []
    for level in sorted(held.levels(role), key=lambda lv: lv.order):
        if back is None:
            out.append((level.id, level.kind, level.selected, level.order,
                        [(j.from_table, tuple(j.from_columns), j.to_table, tuple(j.to_columns),
                          j.kind) for j in level.path]))
        else:
            out.append((back.level_id(level.id), level.kind, level.selected, level.order,
                        [back.join(j) for j in level.path]))
    return out


def _record_kinds(held: plan.SourcePlan, back: _Back | None = None) -> dict:
    out = {}
    for role, kind in held.record_kinds.items():
        if not kind:
            out[role] = None
            continue
        table = kind["table"]
        out[role] = {
            "table": back.table(table) if back else table,
            "key_columns": [back.column(table, c) if back else c for c in kind["key_columns"]],
            "lines_table": back.table(kind["lines_table"]) if back else kind["lines_table"],
        }
    return out


def _tables(held: plan.SourcePlan, back: _Back | None = None) -> list[tuple]:
    out = []
    for table in held.tables:
        serves = sorted((s["role"], s["as"],
                         (back.level_id(s["level"]) if back and s["level"] else s["level"]) or "")
                        for s in table.serves)
        out.append((back.table(table.table) if back else table.table, serves))
    return out


# --- fast: the map, applied in memory -------------------------------------

def test_the_map_is_the_seeds_and_leaves_no_source_name():
    from seed.renamed_source import rename_map

    names = _names()
    model = PhysicalModel.load(FIXTURES / f"{SOURCE}-model.json")
    # Stable: the seed's rename of the recorded model is the kept map.
    assert {k: names[k] for k in ("tables", "columns")} == rename_map(model)
    assert set(names["tables"]) == set(model.table_names())
    assert set(names["columns"]) == {f"{t.name}.{c.name}" for t, c in model.columns()}
    for old, new in names["tables"].items():
        assert old.upper() != new.upper()
    for qualified, new in names["columns"].items():
        assert qualified.rsplit(".", 1)[1].upper() != new.upper()
    _column_names(names)


@pytest.mark.parametrize("source", ["facets_qa", "bank"])
def test_renamed_in_memory_the_proposal_is_the_same(source):
    names = _map_for(source)
    plain = landscape_of(source)
    renamed = Landscape(name=RENAMED, model=rename_model(plain.model, names),
                        profile=rename_profile(plain.profile, names), summary={})
    unrenamed = plan.propose(plain)
    held = plan.propose(renamed)
    back = _Back(names)
    # Level for level, mapped back to the source's names: id, kind,
    # selection, order and path.
    for role in ("primary", "child"):
        assert _levels(held, role, back) == _levels(unrenamed, role), role
    assert _record_kinds(held, back) == _record_kinds(unrenamed)
    assert _tables(held, back) == _tables(unrenamed)
    assert _renamed_scores(held, names, source) == scores(unrenamed, EXPECTED[source])
    # The same record kinds, under their new names.
    for role in ("primary", "child"):
        expected = EXPECTED[source][role]
        assert held.record_kinds[role]["table"] == names["tables"][expected["table"]]
        lines = expected["lines"]
        assert held.record_kinds[role]["lines_table"] == (names["tables"][lines] if lines
                                                          else None)


# --- slow: the copy, read from the database -------------------------------

def _expected_joins(names: dict) -> list[tuple[str, tuple[str, ...], str, tuple[str, ...], str]]:
    """Every join on the unrenamed proposal's paths to the expected levels,
    in renamed terms, with the level it serves."""
    held = plan.propose(landscape_of(SOURCE))
    columns = names["columns"]
    out = []
    for role in ("primary", "child"):
        wanted = EXPECTED[SOURCE][role]
        for level_id in wanted["walk"] + wanted["narrow"]:
            level = next(lv for lv in held.levels(role) if lv.id == level_id)
            for join in level.path:
                out.append((names["tables"][join.from_table],
                            tuple(columns[f"{join.from_table}.{c}"] for c in join.from_columns),
                            names["tables"][join.to_table],
                            tuple(columns[f"{join.to_table}.{c}"] for c in join.to_columns),
                            f"{role} {level.id}"))
    return out


def _keyed(model: PhysicalModel, a: str, a_cols: tuple, b: str, b_cols: tuple) -> bool:
    """Whether the model holds a key between the two sides, either way round."""
    for child, child_cols, parent, parent_cols in ((a, a_cols, b, b_cols),
                                                   (b, b_cols, a, a_cols)):
        table = model.table(child)
        if table and any(k.parent_table == parent and tuple(k.columns) == child_cols
                         and tuple(k.parent_columns) == parent_cols
                         for k in table.foreign_keys):
            return True
    return False


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from seed import renamed_source

    written = tmp_path_factory.mktemp("renamed") / "renamed-map.json"
    assert renamed_source.main(["--map", str(written)]) == 0
    yield json.loads(written.read_text(encoding="utf-8"))
    assert renamed_source.drop()


@pytest.mark.db
@pytest.mark.slow
def test_the_renamed_copy_is_read_and_its_keys_inferred(built):
    from typer.testing import CliRunner

    from sdg.accelerator import landscape, sources
    from sdg.cli.accelerate import accelerate

    names = _names()
    assert built == names, "the seed's map of the live source is the kept one"
    runner = CliRunner()
    done = runner.invoke(accelerate, ["landscape", RENAMED, "--allow-write-login"])
    assert done.exit_code == 0, done.output
    done = runner.invoke(accelerate, ["propose", RENAMED, "--allow-write-login", "--force"])
    assert done.exit_code == 0, done.output

    read = landscape.load(RENAMED)
    keys = [k for t in read.model.tables for k in t.foreign_keys]
    assert keys and all(k.kind == "inferred" for k in keys), "the copy declares no key"

    expected = _expected_joins(names)
    missed = [j for j in expected if not _keyed(read.model, j[0], j[1], j[2], j[3])]
    assert not missed, (f"{len(missed)} of {len(expected)} joins on the expected paths "
                        f"were not inferred: {missed}")

    held = plan.load(sources.folder_for(RENAMED))
    assert _renamed_scores(held, names) == scores(plan.propose(landscape_of(SOURCE)),
                                                  EXPECTED[SOURCE])

    # Faithful: each identity and each computed column of the source is one
    # in the copy too, and no other column is.
    source_model = PhysicalModel.load(FIXTURES / f"{SOURCE}-model.json")
    assert any(c.is_identity for _, c in source_model.columns())
    assert any(c.is_computed for _, c in source_model.columns())
    for table in source_model.tables:
        copy = read.model.table(names["tables"][table.name])
        assert copy is not None, table.name
        for column in table.columns:
            twin = copy.column(names["columns"][f"{table.name}.{column.name}"])
            assert (twin.is_identity, twin.is_computed) == (
                column.is_identity, column.is_computed), f"{table.name}.{column.name}"


@pytest.mark.db
@pytest.mark.slow
def test_a_pack_built_from_the_renamed_copy_loads(built, tmp_path, monkeypatch):
    """The copy, proposed from (by the test above, or here when run alone),
    named, approved in the in-memory store of test_accelerator_build and
    built into tmp_path: the pack is checked, put in place, and SDM starts on
    it in a process of its own with no problem (startup.check)."""
    from typer.testing import CliRunner

    from sdg import packs
    from sdg.accelerator import approval, builder, landscape, sources, startup, workbench
    from sdg.cli.accelerate import accelerate
    from tests.test_accelerator_approval import FakeStore
    from tests.test_accelerator_build import BuildConn

    if not workbench.has_proposal(RENAMED):
        runner = CliRunner()
        for args in (["landscape", RENAMED, "--allow-write-login"],
                     ["propose", RENAMED, "--allow-write-login", "--force"]):
            done = runner.invoke(accelerate, args)
            assert done.exit_code == 0, done.output
    store = FakeStore()
    monkeypatch.setattr(approval, "_connect", lambda: BuildConn(store))
    monkeypatch.setattr(approval, "second_approver", lambda: (None, False))
    monkeypatch.setattr(approval, "who", lambda: "builder")
    checked: list[list[str]] = []
    real = startup.check

    def spied(folder, timeout=300):
        found = real(folder, timeout)
        checked.append(found)
        return found

    monkeypatch.setattr(startup, "check", spied)

    folder = sources.folder_for(RENAMED)
    read = landscape.load(RENAMED)
    held = plan.load(folder)
    graph = plan.graph_for(read, held)
    held = plan.name_record(held, "primary", "person", "people", graph=graph)
    held = plan.name_record(held, "child", "event", "events", graph=graph)
    held = plan.name_pack(held, "renamed_qa", "Renamed QA")
    plan.save(held, folder)
    approval.submit(RENAMED, held, "builder")
    approval.approve(RENAMED, plan.plan_hash(held), "builder", "builder")

    result = builder.build(RENAMED, by="builder", packs_dir=tmp_path / "packs")
    assert result.problems == [], result.problems
    assert result.folder == tmp_path / "packs" / "renamed_qa"
    assert checked == [[]], "SDM starts on the pack with no problem"
    data = json.loads((result.folder / "pack.json").read_text(encoding="utf-8"))
    packs.check(data, "renamed_qa/pack.json")
    assert data["id"] == "renamed_qa" and data["modules"] == ["pattern_analysis"]
    for name in builder.built_files(RENAMED):
        assert (result.folder / name).is_file(), name
    # Built from the renamed names: no table of the unrenamed source is read.
    extract = (result.folder / "extract" / f"{RENAMED}.sql").read_text(encoding="utf-8")
    names = _names()
    for old in names["tables"]:
        assert f"[{old.partition('.')[2]}]" not in extract, old
    assert store.rows[RENAMED]["built_hash"]


# --- the seed's guards and its SQL (no database) --------------------------

def _settings(server: str):
    from types import SimpleNamespace

    from sdg.config import get_settings

    real = get_settings()
    return SimpleNamespace(mssql=SimpleNamespace(server=server, port=real.mssql.port),
                           source_db=real.source_db, ref_db=real.ref_db,
                           denorm_db=real.denorm_db, target_db=real.target_db)


def test_the_copy_is_made_on_this_machine_only(monkeypatch):
    from seed import renamed_source

    monkeypatch.setattr(renamed_source, "not_a_demo_machine", lambda: None)
    for server in ("localhost", ".", "(local)", "127.0.0.1", "localhost\\SQLEXPRESS",
                   "localhost,1433"):
        assert renamed_source.is_local(server), server
    for server in ("shared-sql01", "10.0.0.5", "sql.example.com\\PROD"):
        assert not renamed_source.is_local(server), server
        monkeypatch.setattr(renamed_source, "get_settings", lambda s=server: _settings(s))
        with pytest.raises(renamed_source.Refused, match="not this machine"):
            renamed_source.check()


def test_a_protected_source_is_never_copied_from(monkeypatch):
    """--from with a protected source is refused before the source is even
    opened: the guard reads the source's database, not only the target."""
    from seed import renamed_source
    from sdg.accelerator import sources
    from sdg.config import ProtectedDatabase, get_settings

    monkeypatch.setattr(renamed_source, "not_a_demo_machine", lambda: None)
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "PRODDB")
    here = get_settings().mssql
    monkeypatch.setattr(sources, "source_settings",
                        lambda name: (here, "PRODDB"))
    monkeypatch.setattr(sources, "registry_for",
                        lambda *a, **k: pytest.fail("the protected source was opened"))
    with pytest.raises(ProtectedDatabase, match="copy rows from"):
        renamed_source.read_model("SDMT_FAKE_SRC")
    monkeypatch.setenv("SDG_PROTECTED_DATABASES", "SDMT_FAKE_PROD")
    monkeypatch.setattr(sources, "source_settings",
                        lambda name: (here, "SDMT_FAKE_PROD"))
    with pytest.raises(ProtectedDatabase):
        renamed_source.read_model("SDMT_FAKE_SRC")


def test_a_large_source_table_is_copied_only_when_allowed():
    from seed import renamed_source

    model = PhysicalModel(tables=[
        Table(name="dbo.SMALL", schema_name="dbo", table_name="SMALL", row_count=10),
        Table(name="dbo.HUGE", schema_name="dbo", table_name="HUGE",
              row_count=renamed_source.MAX_COPY_ROWS + 1)])
    with pytest.raises(renamed_source.Refused, match="--allow-large"):
        renamed_source.refuse_large(model)
    renamed_source.refuse_large(model, allow_large=True)
    model.tables.pop()
    renamed_source.refuse_large(model)


def test_the_copy_keeps_identity_computed_columns_and_indexes():
    from seed import renamed_source as seed

    table = Table(
        name="dbo.T", schema_name="dbo", table_name="T", row_count=10, primary_key=["T_ID"],
        columns=[Column(name="T_ID", ordinal=1, type_name="int", is_identity=True),
                 Column(name="T_CD", ordinal=2, type_name="char", max_length=2,
                        nullable=True),
                 Column(name="T_HASH", ordinal=3, type_name="char", is_computed=True,
                        computed_definition="(concat([T_ID],'T_CD',T_CD))")])
    names = {"tables": {"dbo.T": "dbo.ZAZA"},
             "columns": {"dbo.T.T_ID": "ZAZA_ID", "dbo.T.T_CD": "ZAZA_CD",
                         "dbo.T.T_HASH": "ZAZA_HASH"}}
    extras = seed.Extras(identity={("dbo.T", "T_ID"): (100, 5)},
                         persisted={("dbo.T", "T_HASH")}, clustered_key={"dbo.T": False},
                         indexes={"dbo.T": [seed.Index("IX_T", False, [("T_CD", True)],
                                                       ["T_ID"]),
                                            seed.Index("IX_F", False, [("T_CD", False)],
                                                       filtered=True)]})
    created = seed.create_table_sql(table, names, extras)
    assert "[ZAZA_ID] int IDENTITY(100, 5) NOT NULL" in created
    # Renamed inside its definition; quoted text left as it is.
    assert "[ZAZA_HASH] AS (concat([ZAZA_ID],'T_CD',[ZAZA_CD])) PERSISTED" in created
    assert "PRIMARY KEY NONCLUSTERED ([ZAZA_ID])" in created
    index = seed.create_index_sql(table, names, extras.indexes["dbo.T"][0], 1)
    assert index == ("CREATE NONCLUSTERED INDEX [IX_ZAZA_1] ON [dbo].[ZAZA] "
                     "([ZAZA_CD] DESC) INCLUDE ([ZAZA_ID])")
    assert seed.create_index_sql(table, names, extras.indexes["dbo.T"][1], 2) is None
    copied = seed.copy_rows_sql(table, names, "SRC")
    assert copied.startswith("SET IDENTITY_INSERT [dbo].[ZAZA] ON;")
    assert copied.endswith("SET IDENTITY_INSERT [dbo].[ZAZA] OFF")
    assert "T_HASH" not in copied and "ZAZA_HASH" not in copied
    # The metadata the copy reads is read only.
    from sdg.accelerator.source_read import refuse_unless_read

    for sql in (seed.IDENTITY_SQL, seed.PERSISTED_SQL, seed.INDEX_SQL):
        refuse_unless_read(sql)


def test_the_copy_refuses_what_it_cannot_recreate_and_caps_names():
    from seed import renamed_source as seed

    with pytest.raises(seed.Refused, match="alias type"):
        seed.sql_type(Column(name="X", ordinal=1, type_name="flag_t"))
    with pytest.raises(seed.Refused, match="binary"):
        seed.sql_type(Column(name="X", ordinal=1, type_name="binary"))
    assert seed.sql_type(Column(name="X", ordinal=1, type_name="varbinary",
                                max_length=-1)) == "varbinary(max)"
    assert seed.sql_type(Column(name="X", ordinal=1, type_name="binary",
                                max_length=16)) == "binary(16)"
    long = "Z" * 200
    one, two = seed.object_name("UQ_", long, "_1"), seed.object_name("UQ_", long, "_2")
    assert len(one) == len(two) == seed.MAX_NAME and one != two
    assert seed.object_name("PK_", "SHORT") == "PK_SHORT"
