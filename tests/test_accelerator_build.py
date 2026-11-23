"""Build: a business pack written from an approved source plan.

Plans are proposed from the recorded bank and FACETS_QA fixtures, submitted
and approved in the in-memory store of tests/test_accelerator_approval.py
(with the one statement a build adds), and built into tmp_path: no database,
and nothing is written in the repository. Each fixture is built once for the
module; the build starts SDM on the pack in a process of its own
(startup.check), which is slow.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from sdg import packs
from sdg.accelerator import approval, builder, plan, sources, startup
from sdg.accelerator import landscape as reading
from sdg.extract import run as extract_run
from tests.test_accelerator_approval import FakeConn, FakeCursor, FakeStore
from tests.test_neutrality import TERMS

FIXTURES = Path(__file__).parent / "fixtures" / "accelerator"
ROOT = Path(__file__).resolve().parents[1]
# Each fixture: its source name, and the person's names for its record kinds.
SOURCES = {
    "bank": ("BANK", ("customer", "customers"), ("account", "accounts"), "acme_bank"),
    "facets_qa": ("FACETS_QA", ("person", "people"), ("event", "events"), "qa_events"),
}
# Keys proposed as surrogates for their names (CUSTOMER, ACCOUNT) that the
# person confirms hold nothing personal, as they would for the demo bank.
# Columns whose names read as personal, kept on the person's word: the demo
# bank's plain-number keys, and the organization names and state its levels
# and features read (as a person at the customer would confirm them).
CONFIRMED_KEYS = {
    "bank": [("dbo.CUSTOMER", "CUSTOMER_ID"), ("dbo.ACCOUNT", "ACCOUNT_ID"),
             ("dbo.BRANCH", "BRANCH_NAME"), ("dbo.PRODUCT", "PRODUCT_NAME"),
             ("dbo.CUSTOMER", "ADDR_STATE")],
    "facets_qa": [("dbo.CMC_GRGR_GROUP", "GRGR_NAME"), ("dbo.CMC_SGSG_SUBGROUP", "SGSG_NAME")],
}


# --- the store, with the statement a build adds ------------------------------------

class BuildCursor(FakeCursor):
    def execute(self, sql: str, *params) -> "BuildCursor":
        if sql == approval._sql(approval.BUILT):
            self.conn.store.executed.append((sql, params))
            built, at, source, digest = params
            self.rowcount = self._update(
                lambda r: r["source"] == source and r["status"] == "approved"
                and r["plan_hash"] == digest,
                {"built_hash": built, "built_at": at, "built_plan_hash": digest})
            return self
        return super().execute(sql, *params)


class BuildConn(FakeConn):
    def cursor(self) -> BuildCursor:
        return BuildCursor(self)


def _work(folder: Path, fixture: str) -> None:
    """A source's work folder: the recorded landscape, as `landscape` saves it."""
    folder.mkdir(parents=True, exist_ok=True)
    source = SOURCES[fixture][0]
    for name in ("model", "profile"):
        shutil.copyfile(FIXTURES / f"{fixture}-{name}.json", folder / f"{name}.json")
    (folder / "landscape.json").write_text(json.dumps({"source": source}), encoding="utf-8")


def _setup(mp: pytest.MonkeyPatch, work: Path) -> FakeStore:
    store = FakeStore()
    mp.setattr(approval, "_connect", lambda: BuildConn(store))
    mp.setattr(approval, "second_approver", lambda: (None, False))
    mp.setattr(approval, "who", lambda: "builder")
    mp.setattr(sources, "folder_for", lambda name: work)
    return store


def _named(fixture: str) -> plan.SourcePlan:
    """The fixture's plan as proposed, with its record kinds and pack named."""
    source, one, other, pack_id = SOURCES[fixture]
    read = reading.load(source)
    held = plan.propose(read)
    graph = plan.graph_for(read, held)
    held = plan.name_record(held, "primary", *one, graph=graph)
    held = plan.name_record(held, "child", *other, graph=graph)
    for table, column in CONFIRMED_KEYS.get(fixture, []):
        held = plan.set_privacy(held, table, column, "kept", graph=graph,
                                confirm_not_personal=True)
    return plan.name_pack(held, pack_id, f"{pack_id.replace('_', ' ').title()}")


_NAMED: list[dict] = []


def _named_dict() -> dict:
    """The bank plan as named, from the fixture alone (no work folder)."""
    if not _NAMED:
        from sdg.accelerator.landscape import Landscape
        from sdg.discovery.model import PhysicalModel
        from sdg.profiling.model import ProfileReport

        read = Landscape(name="BANK", model=PhysicalModel.load(FIXTURES / "bank-model.json"),
                         profile=ProfileReport.load(FIXTURES / "bank-profile.json"), summary={})
        held = plan.propose(read)
        graph = plan.graph_for(read, held)
        held = plan.name_record(held, "primary", "customer", "customers", graph=graph)
        held = plan.name_record(held, "child", "account", "accounts", graph=graph)
        for table, column in CONFIRMED_KEYS["bank"]:
            held = plan.set_privacy(held, table, column, "kept", graph=graph,
                                    confirm_not_personal=True)
        _NAMED.append(plan.name_pack(held, "acme_bank", "Acme bank").to_dict())
    return json.loads(json.dumps(_NAMED[0]))


def _approve(work: Path, held: plan.SourcePlan) -> None:
    plan.save(held, work)
    approval.submit(held.source, held, "builder")
    approval.approve(held.source, plan.plan_hash(held), "builder", "builder")


class Built:
    def __init__(self, fixture: str, result: builder.BuildResult, store: FakeStore,
                 held: plan.SourcePlan, checked: list[list[str]]) -> None:
        self.fixture, self.result, self.store, self.held = fixture, result, store, held
        self.checked = checked
        self.source = SOURCES[fixture][0]

    def text(self, name: str) -> str:
        return (self.result.folder / name).read_text(encoding="utf-8")

    def json(self, name: str) -> dict:
        return json.loads(self.text(name))


def _build(fixture: str, tmp: Path) -> Built:
    with pytest.MonkeyPatch.context() as mp:
        work = tmp / "work"
        _work(work, fixture)
        store = _setup(mp, work)
        held = _named(fixture)
        _approve(work, held)
        checked: list[list[str]] = []
        real = startup.check

        def spied(folder, timeout=300):
            found = real(folder, timeout)
            checked.append(found)
            return found

        mp.setattr(startup, "check", spied)
        result = builder.build(held.source, by="builder", packs_dir=tmp / "packs")
        return Built(fixture, result, store, held, checked)


@pytest.fixture(scope="module")
def bank(tmp_path_factory) -> Built:
    return _build("bank", tmp_path_factory.mktemp("bank"))


@pytest.fixture(scope="module")
def facets(tmp_path_factory) -> Built:
    return _build("facets_qa", tmp_path_factory.mktemp("facets"))


@pytest.fixture
def work(tmp_path, monkeypatch) -> tuple[Path, FakeStore]:
    """A bank work folder and store of the test's own."""
    folder = tmp_path / "work"
    _work(folder, "bank")
    return folder, _setup(monkeypatch, folder)


@pytest.fixture
def facets_work(tmp_path, monkeypatch) -> tuple[Path, FakeStore]:
    """A FACETS_QA work folder and store of the test's own."""
    folder = tmp_path / "work"
    _work(folder, "facets_qa")
    return folder, _setup(monkeypatch, folder)


# --- what a build writes --------------------------------------------------------------

@pytest.mark.parametrize("which", ["bank", "facets"])
def test_every_file_is_written_checked_and_recorded(which, request):
    built: Built = request.getfixturevalue(which)
    result = built.result
    assert result.problems == [], result.problems
    assert result.folder == result.folder.parent / result.pack_id
    written = sorted(p.relative_to(result.folder).as_posix()
                     for p in result.folder.rglob("*") if p.is_file())
    assert written == sorted(builder.built_files(built.source) + [builder.MARKER])
    assert sorted(result.files) == sorted(builder.built_files(built.source))
    for name, digest in result.files.items():
        assert builder._sha(built.text(name)) == digest, name
    # Checked before it was put in place: pack.json, and SDM started on it.
    packs.check(built.json("pack.json"), "built pack.json")
    assert built.checked == [[]], built.checked
    # Put in place, nothing left beside it, and recorded as built.
    beside = sorted(p.name for p in result.folder.parent.iterdir())
    assert beside == [result.pack_id], beside
    row = built.store.rows[built.source]
    marker = json.loads(built.text(builder.MARKER))
    # The hash covers every file and what the marker records for a load.
    sealed = builder.sealed(marker["reads"], marker["store"])
    assert row["built_hash"] == builder.built_hash(result.files, sealed) and row["built_at"]
    assert row["built_hash"] != builder.built_hash(result.files)
    assert marker["files"] == result.files and marker["built_hash"] == row["built_hash"]
    assert marker["plan_hash"] == plan.plan_hash(built.held) and marker["built_by"] == "builder"


def test_the_built_pack_offers_analysis_only_and_keeps_its_privacy_by_source(bank):
    data = bank.json("pack.json")
    assert data["id"] == "acme_bank" and data["modules"] == ["pattern_analysis"]
    assert data["formats"] == {}
    assert {k: e["role"] for k, e in data["entities"].items()} == {
        "customers": "primary", "accounts": "child"}
    assert data["entities"]["accounts"]["line_anchor"] == "TXN_SEQ"
    assert data["extract"]["default_source"] == "BANK"
    kept = data["extract"]["kept"]["BANK"]
    assert "CUSTOMER_ID" in kept and "TXN_SEQ" in kept
    assert data["sql"] == {"_about": data["sql"]["_about"],
                           "store_tables": ["sql/store_tables.sql"],
                           "store_indexes": ["sql/store_indexes.sql"]}


@pytest.mark.parametrize("which", ["bank", "facets"])
def test_every_mean_age_has_a_band_in_a_pattern_name(which, request):
    """A pattern's name carries its age band where the pack names an age
    feature: the bands end in a catch-all, so every mean age has one (an
    empty list stopped discovery with StopIteration)."""
    built: Built = request.getfixturevalue(which)
    naming = built.json("features.json")["primary_analysis"]["naming"]
    if not naming["age_feature"]:
        pytest.skip("no age feature in this plan")
    ages = naming["ages"]
    assert ages and ages[-1][0] is None, ages
    limits = [below for below, _ in ages[:-1]]
    assert limits == sorted(limits) and all(isinstance(b, int) for b in limits)
    for mean in (0, 17.9, 18, 44, 64.9, 65, 120):
        assert next(word for below, word in ages if below is None or mean < below)


def test_the_built_pack_loads_where_sdg_packs_dir_points(bank, monkeypatch):
    monkeypatch.setenv("SDG_PACKS_DIR", str(bank.result.folder.parent))
    packs.load.cache_clear()
    try:
        pack = packs.load(bank.result.pack_id)
        assert pack.folder == bank.result.folder
        assert pack.primary == "customers" and pack.child == "accounts"
        assert pack.term("accounts", "Many") == "Accounts"
        loaders = pack.value("features:loaders")
        assert loaders["customers"]["table"] == "customers_denorm"
        assert loaders["accounts"]["table"] == "accounts_denorm"
        assert pack.sql("store_tables") == [bank.result.folder / "sql" / "store_tables.sql"]
    finally:
        packs.load.cache_clear()


def test_the_store_scripts_refuse_production_and_the_source_and_are_add_only(bank):
    from sdg.accelerator.sqlgen import SOURCE_REFUSAL
    from tests.test_customer_scripts_guard import _batches, _code

    for name in ("sql/store_tables.sql", "sql/store_indexes.sql"):
        path = bank.result.folder / name
        first, second = _batches(path)[:2]
        # The source, and every protected database, named when it was built
        # (no production database is built in).
        assert "N'BANK'" in _code(first)[0] and _code(first)[0].startswith("IF DB_NAME() IN (")
        assert _code(first)[1] == f"THROW 50000, N'{SOURCE_REFUSAL}', 1;", name
        assert _code(second) == [_code(first)[0], "SET NOEXEC ON;"], name
        body = "\n".join(_batches(path)[2:])
        # Add-only: the one ALTER adds a missing column, NULL, and nothing drops.
        adds = re.findall(r"ALTER TABLE sdm\.\w+ ADD \[\w+\] [\w(),]+ NULL;", body)
        assert len(re.findall(r"\bALTER\b", body)) == len(adds), name
        assert not re.search(r"\b(DROP|TRUNCATE|DELETE|UPDATE|INSERT)\b", body), name
    tables = bank.text("sql/store_tables.sql")
    assert tables.count("CREATE TABLE sdm.") == 2
    assert "IF OBJECT_ID('sdm.customers_denorm', 'U') IS NULL" in tables
    assert "[CUSTOMER_ID] int NOT NULL" in tables and "[TXN_SEQ] int NOT NULL" in tables
    assert ("IF COL_LENGTH('sdm.accounts_denorm', 'PRODUCT_NAME') IS NULL\n"
            "    ALTER TABLE sdm.accounts_denorm ADD [PRODUCT_NAME] varchar(60) NULL;") in tables


def test_the_child_type_column_is_its_first_narrow_level(facets, bank, work):
    folder, _ = work
    analysis = facets.json("features.json")["child_analysis"]
    assert analysis["type_column"] == "CLCL_CL_TYPE"
    assert facets.json("features.json")["store"]["type_filter"] == "clcl_cl_type"
    # The bank's narrow level is not selected at first: no type column.
    assert bank.json("features.json")["child_analysis"]["type_column"] is None
    held = _named("bank")
    read = reading.load("BANK")
    graph = plan.graph_for(read, held)
    held = plan.select_level(held, "child", "dbo.PRODUCT.PRODUCT_TYPE", True, graph=graph)
    features = builder.features_json(held, graph)
    assert features["child_analysis"]["type_column"] == "PRODUCT_TYPE"
    assert features["child_analysis"]["name"]["type"] == "product_type"


def test_the_loaders_read_the_levels_as_the_structure_names_them(bank):
    loaders = bank.json("features.json")["loaders"]
    accounts = loaders["accounts"]
    assert accounts["columns"] == "store.claim_columns"
    assert accounts["order"] == ["ACCOUNT_ID", "TXN_SEQ"]
    assert [f["key"] for f in accounts["filters"]] == ["branch_id", "product_code"]
    assert accounts["filters"][0]["kind"] == "ids"
    assert accounts["filters"][1] == {"key": "product_code", "column": "PRODUCT_CODE",
                                      "kind": "values", "skip_blank": True}
    assert accounts["sizes"]["levels"] == [["BRANCH_ID", "BRANCH_NAME"],
                                           ["BRANCH_ID", "BRANCH_NAME", "PRODUCT_CODE",
                                            "PRODUCT_NAME"]]
    assert [s["takes"] for s in accounts["scope"]] == [[], ["branch_id"]]
    customers = loaders["customers"]
    assert [d["from"] for d in customers["derive"]] == ["BIRTH_DATE", "CUST_SINCE"]
    store = bank.json("features.json")["store"]
    assert store["combo_key"] == ["branch_id", "product_code"]
    assert store["scope_columns"] == {} and store["entity_tag"] == {}


def test_unconfirmed_names_that_read_as_personal_are_not_in_the_pack():
    """A label or feature whose name reads as a person, an account or a
    contact is withheld until the person confirms it: the level is shown by
    its key, and the feature is not measured."""
    from sdg.accelerator.landscape import Landscape
    from sdg.discovery.model import PhysicalModel
    from sdg.profiling.model import ProfileReport

    read = Landscape(name="BANK", model=PhysicalModel.load(FIXTURES / "bank-model.json"),
                     profile=ProfileReport.load(FIXTURES / "bank-profile.json"), summary={})
    held = plan.propose(read)
    graph = plan.graph_for(read, held)
    held = plan.name_record(held, "primary", "customer", "customers", graph=graph)
    held = plan.name_record(held, "child", "account", "accounts", graph=graph)
    named = {(c.source_column, c.serves): c.privacy for t in held.structure for c in t.columns}
    assert named[("BRANCH_NAME", "level_label")] == "withheld"
    assert named[("ADDR_STATE", "feature")] == "withheld"
    features = builder.features_json(held, graph)
    levels = features["loaders"]["accounts"]["sizes"]["levels"]
    assert levels == [["BRANCH_ID"], ["BRANCH_ID", "PRODUCT_CODE"]]
    assert "addr_state" not in features["patterns"]["discovery_defaults"]["customers"]
    with pytest.raises(ValueError, match="confirm it holds nothing personal"):
        plan.set_privacy(held, "dbo.CUSTOMER", "ADDR_STATE", "kept", graph=graph)


def test_a_feature_of_each_line_is_offered_and_built_when_chosen():
    """At lines grain the lines' own codes and amounts are offered, not
    selected (product owner, 2026-11-21); chosen, each is a column of the
    child's table read from the lines, and a feature of the pack."""
    from sdg.accelerator import sqlgen
    from sdg.accelerator.landscape import Landscape
    from sdg.discovery.model import PhysicalModel
    from sdg.profiling.model import ProfileReport

    read = Landscape(name="BANK", model=PhysicalModel.load(FIXTURES / "bank-model.json"),
                     profile=ProfileReport.load(FIXTURES / "bank-profile.json"), summary={})
    held = plan.SourcePlan.from_dict(_named_dict())
    graph = plan.graph_for(read, held)
    offered = {f.id: f for f in held.features("child") if f.table == "dbo.TXN"}
    assert set(offered) == {"dbo.TXN.TXN_TYPE", "dbo.TXN.CHANNEL", "dbo.TXN.AMOUNT"}
    assert not any(f.selected for f in offered.values())
    assert offered["dbo.TXN.AMOUNT"].kind == "range" and offered["dbo.TXN.CHANNEL"].kind == "in"
    for feature_id in ("dbo.TXN.AMOUNT", "dbo.TXN.CHANNEL"):
        held = plan.select_feature(held, "child", feature_id, True, graph=graph)
    child = next(t for t in held.structure if t.role == "child")
    assert child.grain == "lines"
    read_from = {c.source_column: c for c in child.columns if c.serves == "feature"}
    assert read_from["AMOUNT"].source_table == "dbo.TXN" and read_from["AMOUNT"].privacy == "kept"
    assert read_from["CHANNEL"].source_table == "dbo.TXN"
    features = builder.features_json(held, graph)
    analysis = features["child_analysis"]
    assert [read_from["AMOUNT"].name, read_from["AMOUNT"].name.lower()] in         analysis["continuous"]
    assert [read_from["CHANNEL"].name, read_from["CHANNEL"].name.lower()] in         analysis["categorical"]
    sql = sqlgen.block_sql(held, child, graph)
    assert "[AMOUNT]" in sql and "[CHANNEL]" in sql
    # Not chosen, a line's feature leaves no column behind.
    held = plan.select_feature(held, "child", "dbo.TXN.CHANNEL", False, graph=graph)
    child = next(t for t in held.structure if t.role == "child")
    assert "CHANNEL" not in {c.source_column for c in child.columns}


# --- refusals ------------------------------------------------------------------------

def _nothing_in(folder: Path) -> bool:
    return not folder.exists() or not any(folder.iterdir())


def test_an_unapproved_plan_is_refused(work, tmp_path):
    folder, store = work
    held = _named("bank")
    plan.save(held, folder)
    with pytest.raises(approval.ApprovalRefused, match="never submitted"):
        builder.build("BANK", by="builder", packs_dir=tmp_path / "packs")
    approval.submit("BANK", held, "builder")
    with pytest.raises(approval.ApprovalRefused, match="pending review, not approved"):
        builder.build("BANK", by="builder", packs_dir=tmp_path / "packs")
    assert _nothing_in(tmp_path / "packs")


def test_an_approved_plan_edited_since_is_refused(work, tmp_path):
    folder, store = work
    held = _named("bank")
    _approve(folder, held)
    plan.save(plan.name_pack(held, "acme_bank", "Acme retail bank"), folder)
    with pytest.raises(approval.ApprovalRefused, match="changed since it was approved"):
        builder.build("BANK", by="builder", packs_dir=tmp_path / "packs")
    assert _nothing_in(tmp_path / "packs") and store.rows["BANK"]["built_hash"] is None


@pytest.mark.parametrize("shipped", ["health", "banking"])
def test_a_shipped_pack_id_is_refused(work, tmp_path, shipped):
    folder, _ = work
    held = _named("bank")
    held.pack = {"id": shipped, "name": "Shipped"}  # past name_pack's own refusal
    _approve(folder, held)
    with pytest.raises(builder.BuildRefused, match="shipped with SDM"):
        builder.build("BANK", by="builder", packs_dir=tmp_path / "packs")
    assert _nothing_in(tmp_path / "packs")


@pytest.mark.parametrize("inside", ["sdg/packs", "work/generated_packs", "."])
def test_a_packs_folder_inside_the_repository_is_refused(work, inside):
    folder, _ = work
    _approve(folder, _named("bank"))
    target = ROOT / inside
    existed = target.exists()
    with pytest.raises(builder.BuildRefused, match="inside SDM's own folder"):
        builder.build("BANK", by="builder", packs_dir=target)
    assert target.exists() == existed
    assert not (ROOT / "sdg" / "packs" / "acme_bank").exists()


def test_sdg_packs_dir_unset_is_refused(work, monkeypatch):
    folder, _ = work
    _approve(folder, _named("bank"))
    monkeypatch.delenv("SDG_PACKS_DIR", raising=False)
    with pytest.raises(builder.BuildRefused, match="Set SDG_PACKS_DIR"):
        builder.build("BANK", by="builder")


def test_a_plan_withdrawn_while_it_builds_is_never_recorded_or_put_in_place(work, tmp_path,
                                                                            monkeypatch):
    folder, store = work
    _approve(folder, _named("bank"))

    def withdrawn(staged, timeout=300):
        approval.withdraw("BANK")
        return []

    monkeypatch.setattr(startup, "check", withdrawn)
    with pytest.raises(approval.ApprovalRefused, match="draft, not approved"):
        builder.build("BANK", by="builder", packs_dir=tmp_path / "packs")
    assert not (tmp_path / "packs" / "acme_bank").exists()
    assert store.rows["BANK"]["built_hash"] is None


def test_a_rebuild_keeps_the_earlier_build_beside_it(work, tmp_path, monkeypatch):
    folder, store = work
    _approve(folder, _named("bank"))
    monkeypatch.setattr(startup, "check", lambda staged, timeout=300: [])
    packs_dir = tmp_path / "packs"
    first = builder.build("BANK", by="builder", packs_dir=packs_dir)
    assert not list(packs_dir.glob("acme_bank.previous-*"))
    # A hand edit in the built folder.
    (first.folder / "notes.txt").write_text("first", encoding="utf-8")
    second = builder.build("BANK", by="builder", packs_dir=packs_dir)
    assert first.folder == second.folder and (second.folder / "pack.json").is_file()
    earlier = sorted(packs_dir.glob("acme_bank.previous-*"))
    assert len(earlier) == 1 and (earlier[0] / builder.MARKER).is_file()
    assert (earlier[0] / "notes.txt").read_text(encoding="utf-8") == "first"
    assert "previous-<date and" in (second.folder / "README.txt").read_text(encoding="utf-8")
    # A third build keeps both earlier builds, hand edits and all: none is
    # ever removed by a build.
    (second.folder / "notes.txt").write_text("second", encoding="utf-8")
    third = builder.build("BANK", by="builder", packs_dir=packs_dir)
    kept = sorted(packs_dir.glob("acme_bank.previous-*"))
    assert len(kept) == 2 and earlier[0] in kept
    assert sorted((k / "notes.txt").read_text(encoding="utf-8") for k in kept) == [
        "first", "second"]
    assert (third.folder / "pack.json").is_file() and not (third.folder / "notes.txt").exists()


def test_an_earlier_build_is_never_moved_over_another(tmp_path):
    from datetime import datetime

    when = datetime(2026, 11, 21, 10, 0, 0)
    taken = builder.previous_folder(tmp_path, "acme_bank", when)
    assert taken.name == "acme_bank.previous-20261121-100000"
    taken.mkdir()
    again = builder.previous_folder(tmp_path, "acme_bank", when)
    assert again.name == "acme_bank.previous-20261121-100000-2" and not again.exists()


def test_a_pack_that_does_not_start_is_left_unplaced_with_its_problems(work, tmp_path,
                                                                      monkeypatch):
    folder, store = work
    _approve(folder, _named("bank"))
    monkeypatch.setattr(startup, "check", lambda staged, timeout=300: ["x does not import"])
    result = builder.build("BANK", by="builder", packs_dir=tmp_path / "packs")
    assert result.problems == ["x does not import"]
    assert result.folder == tmp_path / "packs" / "acme_bank.building" / "acme_bank"
    assert not (tmp_path / "packs" / "acme_bank").exists()
    assert store.rows["BANK"]["built_hash"] is None


def test_a_plan_edited_to_keep_a_locked_column_is_refused(work, tmp_path):
    folder, _ = work
    held = _named("bank")
    graph = plan.graph_for(reading.load("BANK"), held)
    held = plan.add_extra_column(held, "primary", "dbo.CUSTOMER", "LAST_NAME", graph=graph)
    column = next(c for t in held.structure for c in t.columns if c.name == "LAST_NAME")
    # Kept and still locked: refused at submit, without the key graph.
    column.privacy = "kept"
    assert any("locked and cannot be kept" in p for p in approval.problems(held))
    # Kept and unlocked by hand: submit cannot tell, the build can.
    column.locked, column.proposed = False, "kept"
    assert approval.problems(held) == []
    _approve(folder, held)
    with pytest.raises(builder.BuildRefused, match="LAST_NAME is locked"):
        builder.build("BANK", by="builder", packs_dir=tmp_path / "packs")
    assert _nothing_in(tmp_path / "packs")


def test_a_role_or_grain_no_edit_makes_is_refused():
    held = plan.SourcePlan.from_dict(_named_dict())
    held.structure[0].role = "x\nDROP TABLE sdm.t;--"
    assert any("neither is one a build writes" in p for p in approval.problems(held))
    held = plan.SourcePlan.from_dict(_named_dict())
    held.record_kinds["other"] = dict(held.record_kinds["primary"])
    assert any("only primary and child are built" in p for p in approval.problems(held))


def test_a_packs_folder_holding_the_repository_is_refused(work):
    folder, _ = work
    held = _named("bank")
    held.pack = {"id": ROOT.name.lower(), "name": "Over SDM"}
    _approve(folder, held)
    with pytest.raises(builder.BuildRefused, match="SDM's own folder"):
        builder.build("BANK", by="builder", packs_dir=ROOT.parent)
    assert ROOT.is_dir() and not (ROOT.parent / f"{ROOT.name.lower()}.building").exists()


def test_the_dry_run_names_a_column_the_structure_lacks(work):
    held = _named("bank")
    assert approval.problems(held) == []
    child = next(t for t in held.structure if t.role == "child")
    child.columns = [c for c in child.columns if c.name != "PRODUCT_NAME"]
    found = approval.problems(held)
    assert any("PRODUCT_NAME" in p and "not a column of" in p for p in found), found


# --- the extract SQL ---------------------------------------------------------------------

def _rendered(held: plan.SourcePlan, source: str) -> dict[str, str]:
    read = reading.load(source)
    graph = plan.graph_for(read, held)
    return builder.render(held, graph, approved_by="Reviewer", approved_at="2026-11-21",
                          plan_hash=plan.plan_hash(held), databases=["PRODDB", source],
                          packs_dir="C:/packs")


def _blocks(folder: Path, source: str, monkeypatch) -> dict[str, str]:
    """The extract as the runner splits it (sdg/extract/run.py `_statements`)."""
    monkeypatch.setattr(extract_run, "definitions", lambda: folder / "extract")
    return extract_run._statements(source)


def _bare(sql: str) -> str:
    """SQL without its comments and quoted values."""
    sql = re.sub(r"N?'(?:[^']|'')*'", "''", sql)
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


@pytest.mark.parametrize("which", ["bank", "facets"])
def test_one_statement_per_block_every_identifier_bracketed(which, request, monkeypatch):
    built: Built = request.getfixturevalue(which)
    blocks = _blocks(built.result.folder, built.source, monkeypatch)
    assert sorted(blocks) == sorted(t.name for t in built.held.structure)
    names = set()
    for denorm in built.held.structure:
        for column in denorm.columns:
            names |= {column.name, column.source_column,
                      column.source_table.split(".")[-1]}
    for table, select in blocks.items():
        bare = _bare(select).strip()
        assert bare.count(";") == 1 and bare.endswith(";"), table
        unbracketed = re.sub(r"\[[^\]]*\]", "[]", bare)
        loose = sorted(n for n in names if re.search(rf"\b{re.escape(n)}\b", unbracketed))
        assert not loose, (table, loose)


def test_the_child_follows_the_primarys_scope(facets, monkeypatch):
    blocks = _blocks(facets.result.folder, facets.source, monkeypatch)
    child = blocks[next(t.name for t in facets.held.structure if t.role == "child")]
    assert "DATEADD(month, -24, CAST(GETDATE() AS date))" in child
    assert re.search(r"AND EXISTS \(\s+SELECT 1\s+FROM \[dbo\]\.\[CMC_MEME_MEMBER\] AS p0",
                     child), child
    assert "p0.[MEME_CK] = t1.[MEME_CK]" in child


def test_a_withheld_column_is_never_selected_as_itself(work, monkeypatch):
    held = _named("bank")
    graph = plan.graph_for(reading.load("BANK"), held)
    held = plan.add_extra_column(held, "primary", "dbo.CUSTOMER", "LAST_NAME", graph=graph)
    column = next(c for t in held.structure for c in t.columns if c.source_column == "LAST_NAME")
    assert column.privacy == "withheld" and column.locked
    texts = _rendered(held, "BANK")
    extract = texts["extract/BANK.sql"]
    assert "CAST(NULL AS varchar(40)) AS [LAST_NAME]" in extract
    assert ".[LAST_NAME]" not in extract
    data = json.loads(texts["pack.json"])["extract"]
    assert "LAST_NAME" in data["withheld"]["BANK"] and "LAST_NAME" not in data["kept"]["BANK"]
    assert "LAST_NAME" in json.loads(texts["features.json"])["store"]["personal_columns"]


def test_a_quote_in_a_criterion_value_is_doubled(work):
    held = _named("bank")
    graph = plan.graph_for(reading.load("BANK"), held)
    criterion = "child:values:dbo.ACCOUNT.ACCT_STATUS"
    held = plan.set_criterion(held, criterion, values=["O'X"], graph=graph)
    held = plan.select_criterion(held, criterion, True, graph=graph)
    extract = _rendered(held, "BANK")["extract/BANK.sql"]
    assert "t1.[ACCT_STATUS] IN ('O''X')" in extract
    assert "'O'X'" not in extract


def test_a_dated_link_is_read_through_its_pick(facets_work):
    held = _named("facets_qa")
    graph = plan.graph_for(reading.load("FACETS_QA"), held)
    held = plan.select_level(held, "primary", "dbo.CMC_PRPR_PROV.PRPR_ID", True, graph=graph)
    primary = next(t for t in held.structure if t.role == "primary")
    assert [p["table"] for p in primary.pick] == ["dbo.CMC_MEPR_PRIM_PROV"]
    extract = _rendered(held, "FACETS_QA")["extract/FACETS_QA.sql"]
    start, end = primary.pick[0]["start"], primary.pick[0]["end"]
    # Ties on the latest start broken alike every run: an open end first, then
    # the later end, then the key.
    assert (f"ROW_NUMBER() OVER (PARTITION BY b.[MEME_CK] ORDER BY b.[{start}] DESC, "
            f"CASE WHEN b.[{end}] IS NULL THEN 0 ELSE 1 END, b.[{end}] DESC, "
            f"b.[MEPR_PCP_TYPE]) AS sdm_pick") in extract
    # In force today: a start before tomorrow, whatever its time of day.
    assert (f"WHERE b.[{start}] < DATEADD(day, 1, CAST(GETDATE() AS date))\n"
            f"      AND (b.[{end}] IS NULL OR b.[{end}] >= CAST(GETDATE() AS date))") in extract
    # Only what the block reads is carried: the key, the dates, the next join.
    assert "b.*" not in extract
    assert re.search(rf"SELECT b\.\[MEME_CK\], b\.\[{start}\], b\.\[{end}\], "
                     rf"b\.\[PRPR_ID\],", extract), extract
    assert re.search(r"LEFT JOIN pick_1 AS t\d+ ON t\d+\.\[MEME_CK\] = t0\.\[MEME_CK\] "
                     r"AND t\d+\.sdm_pick = 1", extract), extract


# --- neutrality ----------------------------------------------------------------------

def _code_names() -> set[str]:
    """The code's own key names: every key of the template (the banking pack's,
    with no business value), and every step and place in slots.json."""
    names: set[str] = set()

    def keys(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                names.add(key)
                keys(value)
        elif isinstance(node, list):
            for value in node:
                keys(value)

    for name in ("pack.json", "features.json", "story.json", "glossary.json"):
        keys(json.loads((builder.TEMPLATE / name).read_text(encoding="utf-8")))
    for place in builder._slots().values():
        dotted = place.partition(":")[2]
        names.add(dotted)
        names.update(dotted.split("."))
    return names


def test_the_built_bank_pack_says_no_health_word(bank):
    allowed = _code_names()
    found = {}
    for name in builder.built_files(bank.source):
        text = bank.text(name)
        if name.endswith(".json"):
            strings: list[str] = []

            def walk(node, key=None) -> None:
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k not in allowed:
                            strings.append(k)
                        if k != "_about":
                            walk(v, k)
                elif isinstance(node, list):
                    for v in node:
                        walk(v, key)
                elif isinstance(node, str) and node not in allowed:
                    strings.append(node)

            walk(json.loads(text))
            text = "\n".join(strings)
        hits = sorted({term for term, pattern in TERMS.items() if pattern.search(text)})
        if hits:
            found[name] = hits
    assert not found, found


# --- the commands ----------------------------------------------------------------------

def test_the_commands_build_and_list_the_files(work, tmp_path):
    from typer.testing import CliRunner

    from sdg.cli.accelerate import accelerate

    folder, store = work
    _approve(folder, _named("bank"))
    runner = CliRunner()
    packs_dir = str(tmp_path / "packs")
    done = runner.invoke(accelerate, ["build", "BANK", "--packs-dir", packs_dir])
    said = " ".join(done.output.split())
    assert done.exit_code == 0, done.output
    assert "Built the acme_bank pack" in said and "SDG_PACK=acme_bank" in said
    assert store.rows["BANK"]["built_hash"]
    listed = runner.invoke(accelerate, ["files", "BANK", "--packs-dir", packs_dir])
    assert listed.exit_code == 0 and "extract/BANK.sql" in listed.output
    assert "not built" not in listed.output
    shown = runner.invoke(accelerate, ["files", "BANK", "extract/BANK.sql",
                                       "--packs-dir", packs_dir])
    assert shown.exit_code == 0 and "-- table: customers_denorm" in shown.output
    assert "[dbo].[CUSTOMER]" in shown.output
    refused = runner.invoke(accelerate, ["files", "BANK", "../../.env", "--packs-dir", packs_dir])
    assert refused.exit_code == 1 and "not a file a build writes" in refused.output


def test_a_lines_grain_key_is_not_null_only_where_the_lines_hold_it_so(work):
    from sdg.accelerator import sqlgen

    held = _named("bank")
    graph = plan.graph_for(reading.load("BANK"), held)
    tables = sqlgen.store_tables_sql(held, graph, [])
    assert "[ACCOUNT_ID] bigint NOT NULL" in tables
    # The key is selected from the lines: their column says whether it may be NULL.
    graph.table("dbo.TXN").column("ACCOUNT_ID").nullable = True
    tables = sqlgen.store_tables_sql(held, graph, [])
    assert "[ACCOUNT_ID] bigint NULL" in tables and "[TXN_SEQ] int NOT NULL" in tables


# --- the phase 2 full review: joins, ties, scope, indexes, gates -----------------------

def _graph_of(source: str, held: plan.SourcePlan):
    return plan.graph_for(reading.load(source), held)


def _extract(held: plan.SourcePlan, graph) -> str:
    from sdg.accelerator import sqlgen

    return sqlgen.extract_sql(held, graph, approved_by="Reviewer", approved_at="2026-11-21",
                              plan_hash=plan.plan_hash(held))


@pytest.mark.parametrize("which", ["bank", "facets"])
def test_every_extract_statement_runs_on_at_most_two_cores(which, request, monkeypatch):
    built: Built = request.getfixturevalue(which)
    for table, select in _blocks(built.result.folder, built.source, monkeypatch).items():
        assert select.rstrip().endswith("\nOPTION (MAXDOP 2);"), table


def test_a_link_with_no_primary_key_breaks_ties_by_every_column_read(facets_work):
    held = _named("facets_qa")
    graph = _graph_of("FACETS_QA", held)
    held = plan.select_level(held, "primary", "dbo.CMC_PRPR_PROV.PRPR_ID", True, graph=graph)
    (pick,) = next(t for t in held.structure if t.role == "primary").pick
    graph.table(pick["table"]).primary_key = []
    extract = _extract(held, graph)
    start, end = pick["start"], pick["end"]
    assert "has no primary key: ties are broken by every column read" in extract
    # Partitioned by the key it is entered by; the rest of what it carries breaks ties.
    assert (f"ORDER BY b.[{start}] DESC, CASE WHEN b.[{end}] IS NULL THEN 0 ELSE 1 END, "
            f"b.[{end}] DESC, b.[PRPR_ID]) AS sdm_pick") in extract


def test_a_join_up_to_columns_no_key_holds_is_refused(work):
    from sdg.accelerator import sqlgen

    held = _named("bank")
    graph = _graph_of("BANK", held)
    level_table = graph.table("dbo.BRANCH")
    level_table.primary_key, level_table.unique_constraints = [], []
    for column in level_table.columns:
        column.is_unique = False
    with pytest.raises(sqlgen.SqlRefused, match="neither its primary key nor a unique index"):
        _extract(held, graph)
    # The submit gate writes the SQL too, given the key graph.
    found = approval.problems(held, graph)
    assert any("neither its primary key nor a unique index" in p for p in found), found


def test_a_label_joined_on_columns_no_key_holds_is_refused(facets_work):
    from sdg.accelerator import sqlgen

    held = _named("facets_qa")
    graph = _graph_of("FACETS_QA", held)
    level = next(lv for lv in held.levels("primary")
                 if lv.selected and lv.label_table and lv.label_table != lv.table)
    label = graph.table(level.label_table)
    label.primary_key, label.unique_constraints = [], []
    for column in label.columns:
        column.is_unique = False
    with pytest.raises(sqlgen.SqlRefused, match=re.escape(f"{level.id}'s label meets "
                                                          f"{level.label_table}")):
        _extract(held, graph)


def test_a_child_linked_to_the_primary_by_two_keys_follows_only_a_stated_one(facets_work):
    from sdg.accelerator import sqlgen
    from sdg.accelerator import structure as structure_rules
    from sdg.accelerator.graph import Edge
    from sdg.accelerator.hierarchy import Join

    held = _named("facets_qa")
    graph = _graph_of("FACETS_QA", held)
    child, primary = held.record_kinds["child"]["table"], held.record_kinds["primary"]["table"]
    (edge,) = structure_rules.primary_edges(held, graph)
    assert any(c.selected for c in held.criteria["primary"])
    other = next(c.name for c in graph.table(child).columns if c.name not in edge.child_columns)
    graph._add(Edge(child, [other], primary, list(edge.parent_columns), "inferred"))
    with pytest.raises(sqlgen.SqlRefused, match="links to the primary by 2 keys"):
        _extract(held, graph)
    held = plan.state_join(held, "child", Join(child, list(edge.child_columns), primary,
                                               list(edge.parent_columns), "stated", None),
                           graph=graph)
    extract = _extract(held, graph)
    key = edge.child_columns[0]
    assert re.search(rf"p0\.\[{key}\] = t\d+\.\[{key}\]", extract), extract


def test_a_criterion_on_a_linked_table_says_it_acts_as_an_inner_join(facets_work):
    held = _named("facets_qa")
    record = held.record_kinds["primary"]["table"]
    bridge = next(c for c in held.criteria["primary"]
                  if c.kind == "in_force" and c.table != record)
    assert "as an inner join would" in bridge.reason
    own = next(c for c in held.criteria["primary"] if c.kind == "in_force" and c.table == record)
    assert "the child's records follow the primary's criteria" in own.reason


def test_the_readme_says_how_the_extract_reads_and_when_to_index(bank):
    readme = " ".join(bank.text("README.txt").split())
    assert "a full read of every table it names" in readme
    assert "run it off-peak, or against a replica of the source" in readme
    assert "After the first extract, hand sql\\store_indexes.sql to the DBA" in readme
    assert "acts as an inner join" in readme
    assert "The child's records follow the primary's criteria" in readme
    indexes = bank.text("sql/store_indexes.sql")
    assert "Run it after\n-- the first extract" in indexes


def test_an_index_key_wider_than_sql_server_takes_is_left_out(work):
    from sdg.accelerator import sqlgen

    assert sqlgen.key_bytes("nvarchar(10)") == 20 and sqlgen.key_bytes("varchar(10)") == 10
    assert sqlgen.key_bytes("int") == 4 and sqlgen.key_bytes("nvarchar(9)") == 18
    assert sqlgen.key_bytes("varchar(max)") is None and sqlgen.key_bytes("xml") is None
    held = _named("bank")
    child = next(t for t in held.structure if t.role == "child")
    column = {c.name: c for c in child.columns}
    indexes = sqlgen.store_indexes_sql(held, [])
    assert "ix_accounts_denorm_product_code" in indexes
    assert "ix_accounts_denorm_account_id_txn_seq" in indexes
    # One wide column alone, and a composite key wide only in sum.
    column["PRODUCT_CODE"].sql_type = "nvarchar(900)"
    column["ACCOUNT_ID"].sql_type = "varchar(1697)"
    indexes = sqlgen.store_indexes_sql(held, [])
    assert ("-- PRODUCT_CODE: up to 1800 bytes, more than the 1700 an index key holds, so not "
            "indexed.") in indexes
    assert ("-- ACCOUNT_ID, TXN_SEQ: up to 1701 bytes, more than the 1700 an index key holds, "
            "so not indexed.") in indexes
    assert "ix_accounts_denorm_product_code" not in indexes
    assert "ix_accounts_denorm_account_id_txn_seq" not in indexes


def test_the_submit_gate_refuses_a_plan_whose_sql_cannot_be_written(work):
    folder, store = work
    held = _named("bank")
    graph = _graph_of("BANK", held)
    graph.table("dbo.PRODUCT").primary_key = []
    graph.table("dbo.PRODUCT").unique_constraints = []
    for column in graph.table("dbo.PRODUCT").columns:
        column.is_unique = False
    plan.save(held, folder)
    with pytest.raises(approval.ApprovalRefused, match="The pack's SQL cannot be written"):
        approval.submit("BANK", held, "builder", graph)
    assert "BANK" not in store.rows
    # Without the graph the SQL is not written, and the build checks it again.
    assert approval.problems(held) == []


def test_two_levels_of_one_word_get_a_scope_slug_each(work, monkeypatch):
    held = _named("bank")
    monkeypatch.setattr(builder, "level_word", lambda column: "Group")
    loaders = builder.features_json(held)["loaders"]
    assert [s["level"] for s in loaders["accounts"]["scope"]] == ["group", "group_2"]
    assert [s["level"] for s in loaders["customers"]["scope"]] == ["group"]


def test_one_filter_reading_two_columns_is_refused(work):
    held = _named("bank")
    child = next(t for t in held.structure if t.role == "child")
    level = next(c for c in child.columns if c.serves == "level_key" and c.name == "BRANCH_ID")
    level.name = "Branch_Id"  # by hand: the same filter key, another column
    with pytest.raises(builder.BuildRefused, match="one filter reads one column"):
        builder.features_json(held)
    assert any("one filter reads one column" in p for p in approval.problems(held))


@pytest.mark.parametrize("pack_id", ["../evil", "a/b", "a\\b", "Acme", "ab\n", "",
                                     "zz_own_folder"])
def test_a_pack_id_a_build_may_not_write_is_refused_and_nothing_is_written(
        work, tmp_path, monkeypatch, pack_id):
    folder, store = work
    if pack_id == "zz_own_folder":
        # A folder among SDM's own packs that is not a shipped pack.
        own = tmp_path / "own_packs"
        (own / pack_id).mkdir(parents=True)
        monkeypatch.setattr(packs, "BUILT_IN", own)
        assert pack_id not in plan.SHIPPED_PACKS
    held = _named("bank")
    held.pack = {"id": pack_id, "name": "Edited by hand"}
    plan.save(held, folder)
    # Approved as it stands, past the gates an edit would meet.
    store.exists = True
    store.rows["BANK"] = {c: None for c in approval.COLUMNS} | {
        "status": "approved", "plan_hash": plan.plan_hash(held), "plan_json": "{}",
        "approved_by": "Reviewer"}
    with pytest.raises(builder.BuildRefused, match="Name the pack|shipped with SDM"):
        builder.check_pack_id(pack_id)
    packs_dir = tmp_path / "packs"
    with pytest.raises(builder.BuildRefused, match="Name the pack|shipped with SDM"):
        builder.build("BANK", by="builder", packs_dir=packs_dir)
    assert _nothing_in(packs_dir)
    assert not (tmp_path / "evil").exists() and not (tmp_path / "a").exists()
    assert store.rows["BANK"]["built_hash"] is None


def test_a_landscape_read_again_after_approval_refuses_the_build(work, tmp_path):
    folder, store = work
    _approve(folder, _named("bank"))
    profile = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
    profile["profiled"] = "2099-01-01T00:00:00"
    (folder / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="read again after this proposal"):
        builder.build("BANK", by="builder", packs_dir=tmp_path / "packs")
    assert _nothing_in(tmp_path / "packs") and store.rows["BANK"]["built_hash"] is None


def test_a_build_records_the_plan_it_was_built_from(work, tmp_path, monkeypatch):
    folder, store = work
    held = _named("bank")
    _approve(folder, held)
    monkeypatch.setattr(startup, "check", lambda staged, timeout=300: [])
    builder.build("BANK", by="builder", packs_dir=tmp_path / "packs")
    row = store.rows["BANK"]
    assert row["built_hash"] and row["built_plan_hash"] == plan.plan_hash(held)


# --- startup.check: negative controls --------------------------------------------------

def _banking_copy(tmp_path: Path, part: str, drop: list[str]) -> Path:
    """The banking pack copied under another id, one place deleted from a file."""
    folder = tmp_path / "packs" / "bare_check"
    shutil.copytree(packs.BUILT_IN / "banking", folder)
    pack = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
    pack["id"] = "bare_check"
    (folder / "pack.json").write_text(json.dumps(pack, indent=1), encoding="utf-8")
    data = json.loads((folder / part).read_text(encoding="utf-8"))
    node = data
    for step in drop[:-1]:
        node = node[step]
    del node[drop[-1]]
    (folder / part).write_text(json.dumps(data, indent=1), encoding="utf-8")
    return folder


def test_startup_check_names_a_required_place_the_pack_lacks(tmp_path):
    problems = startup.check(_banking_copy(tmp_path, "features.json", ["loaders"]))
    assert problems, "a pack without its loaders was taken as starting"
    assert any("features:loaders" in p for p in problems), problems


def test_startup_check_names_a_module_that_does_not_import(tmp_path):
    problems = startup.check(_banking_copy(tmp_path, "story.json", ["naming_states"]))
    assert any("does not import" in p and "story:naming_states" in p for p in problems), problems


def test_lines_joined_to_a_record_on_columns_no_key_holds_are_refused(work):
    from sdg.accelerator import sqlgen

    held = _named("bank")
    graph = _graph_of("BANK", held)
    record = graph.table(held.record_kinds["child"]["table"])
    record.primary_key, record.unique_constraints = [], []
    for column in record.columns:
        column.is_unique = False
    with pytest.raises(sqlgen.SqlRefused, match=f"meets {held.record_kinds['child']['table']}"):
        _extract(held, graph)
