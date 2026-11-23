"""One loader for every entity reads exactly what the two old loaders read.

sdg.analyzer.entity_loader took over from data_loader (members) and
claims_data_loader (claims), reading how from the business pack. The old
loaders are kept verbatim in tests/fixtures/loaders_before_merge (only their
imports changed, so they load from there), and here both are run on the same
store and held equal: the same WHERE clauses, the same frames row for row and
column for column, the same family, labels and filters, the same refusals.

The store is SQLite, with the pattern store's tables in a schema called sdm:
it reads the loaders' SQL as SQL Server would for everything they use. A
second test reads the real store, when there is one.
"""

from __future__ import annotations

import importlib.util
import random
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

from sdg import packs
from sdg.analyzer import entity_loader, provider_features
from sdg.analyzer.services import cache

BEFORE = Path(__file__).parent / "fixtures" / "loaders_before_merge"
ATTACK = "X') ; DROP TABLE sdm.patterns; --"


def _before(name: str):
    module_name = f"{name}_before_merge"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, BEFORE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


old_members = _before("data_loader")
old_claims = _before("claims_data_loader")


# ── A seeded store ────────────────────────────────────────────────────────────

GROUPS = [(101, "Acme Corp "), (202, "Beta LLC"), (303, "Gamma Co")]
CATS = [("MED ", "Medical"), ("DEN", "Dental "), ("", "None")]
PLANS = [("PLN1", "Plan One"), ("PLN2 ", "Plan Two"), ("O'BRIEN", "Quoted plan")]
PRODUCTS = [("PRD1", "Product One"), ("PRD2", "Product Two ")]


def _value(column: str, rng: random.Random, i: int):
    if column.endswith("_DT"):
        return f"{rng.randint(1940, 2024)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    if column.endswith(("_AMT", "_ALLOW", "_CHG")) or column in ("CLCL_ME_AGE",):
        return round(rng.uniform(0, 900), 2) if column != "CLCL_ME_AGE" else rng.randint(0, 90)
    if column.endswith("_CK") or column == "MEME_SFX":
        return rng.randint(1, 9999)
    return rng.choice(["A", "B ", "C", "", None, f"V{i % 7}"])


def _member_rows(rng: random.Random) -> list[dict]:
    columns = packs.held("features:store.member_columns")
    rows = []
    for meme in range(1, 121):
        g, c, p, d = rng.choice(GROUPS), rng.choice(CATS), rng.choice(PLANS), rng.choice(PRODUCTS)
        # Some members have several enrollments; the newest is the one read.
        for n in range(rng.choice([1, 1, 2, 3])):
            row = {col: _value(col, rng, meme) for col in columns}
            row.update(MEME_CK=meme, SBSB_CK=1000 + meme // 3, GRGR_CK=g[0], GRGR_NAME=g[1],
                       CSPD_CAT=c[0], CSPD_CAT_DESC=c[1], CSPI_ID=p[0], PLDS_DESC=p[1],
                       PDPD_ID=d[0], PDDS_DESC=d[1], MEME_REL=rng.choice(["M", "S", "D"]),
                       MEME_SEX=rng.choice(["F", "M", "U"]),
                       MEME_ORIG_EFF_DT=f"{2010 + n + meme % 9}-0{1 + n}-15")
            if meme % 17 == 0:
                row["MEME_BIRTH_DT"] = "not a date"
            rows.append(row)
    return rows


def _claim_rows(rng: random.Random) -> list[dict]:
    columns = packs.held("features:store.claim_columns")
    rows = []
    for claim in range(1, 151):
        g, p, d = rng.choice(GROUPS), rng.choice(PLANS), rng.choice(PRODUCTS)
        for line in range(1, rng.choice([2, 3, 4])):
            row = {col: _value(col, rng, claim) for col in columns}
            row.update(CLCL_ID=f"C{claim:05d} ", CDML_SEQ_NO=line, GRGR_CK=g[0], GRGR_NAME=g[1],
                       CSPI_ID=p[0], PLDS_DESC=p[1], PDPD_ID=d[0],
                       CLCL_CL_TYPE=rng.choice(["P", "I ", "D"]), MEME_SEX=rng.choice(["F", "M "]),
                       CLCL_ME_AGE=rng.randint(0, 90),
                       IDCD_ID_PRIMARY=rng.choice(["E11", "I10 ", "", "E11'"]),
                       IPCD_ID=rng.choice(["99213", "99214", "9'"]),
                       CLCL_NTWK_IND=rng.choice(["I", "O"]))
            rows.append(row)
    return rows


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    folder = tmp_path_factory.mktemp("store")
    main, sdm = folder / "main.db", folder / "sdm.db"
    rng = random.Random(20261108)
    with sqlite3.connect(sdm) as conn:
        for name, rows in (("member_denorm", _member_rows(rng)),
                           ("claims_denorm", _claim_rows(rng))):
            pd.DataFrame(rows).to_sql(name, conn, index=False)
        homes = [{"MEME_CK": m, "ADDR_ROLE": role, "SBAD_ZIP": f"4{m:04d}-1234",
                  "SBAD_CITY": f" City{m % 5} ", "SBAD_STATE": "OH "}
                 for m in range(1, 121, 2) for role in ("HOME", "MAIL")]
        homes.append({"MEME_CK": 3, "ADDR_ROLE": "HOME", "SBAD_ZIP": "99999",
                      "SBAD_CITY": "Second", "SBAD_STATE": "KY"})
        pd.DataFrame(homes).to_sql("member_address", conn, index=False)
    sqlite3.connect(main).close()

    def connect():
        conn = sqlite3.connect(main)
        conn.execute(f"ATTACH DATABASE '{sdm.as_posix()}' AS sdm")
        return conn

    present = {}
    with sqlite3.connect(sdm) as conn:
        for name in ("member_denorm", "claims_denorm", "member_address"):
            present[f"sdm.{name}"] = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
    return connect, present


SENT: dict[str, list[str]] = {"old": [], "new": []}


@pytest.fixture(autouse=True)
def on_the_store(store, monkeypatch):
    connect, present = store
    # Every query each loader sends, as text, for the two to be held equal
    # as SQL Server would read them, not only by what SQLite returns.
    for side, modules in (("old", (old_members, old_claims)), ("new", (entity_loader,))):
        SENT[side] = []
        for module in modules:
            def spy(sql, conn, *rest, _real=module.read_sql, _side=side):
                # A comment on a line of its own is not SQL Server's to read.
                said = (line for line in sql.splitlines() if not line.strip().startswith("--"))
                SENT[_side].append(" ".join(" ".join(said).split()))
                return _real(sql, conn, *rest)
            monkeypatch.setattr(module, "read_sql", spy)
    for module, name in ((old_members, "_conn"), (old_claims, "_conn"),
                         (entity_loader, "connect")):
        monkeypatch.setattr(module, name, connect)
    for module in (old_members, entity_loader):
        monkeypatch.setattr(module, "_PRESENT", dict(present))
    # Scope choices are cached; each test reads the store.
    cache.clear_all()

    # The tables beside the store: the same stand-in for both loaders,
    # recording what each was asked.
    def members(df, reference_date):
        df["_joined_as_of"] = reference_date
        return df

    def claims(df):
        df["_joined"] = "claims"
        return df

    monkeypatch.setattr(provider_features, "members", members)
    monkeypatch.setattr(provider_features, "claims", claims)


# What the new loader sends on purpose that the old ones did not, each a fix
# (STATUS.md, the loader fixes): the old queries with these edits made must be
# the new ones, word for word.
FIXED_SQL: list[tuple[str, str]] = []
# The newest enrollment, ties broken the same way every time: the scope
# levels, then every other column the query reads, in the order it reads them.
_NEWEST = re.compile(r"^WITH ranked AS \( SELECT (.*?), ROW_NUMBER\(\) OVER \(PARTITION BY MEME_CK "
                     r"ORDER BY MEME_ORIG_EFF_DT DESC\) AS _rn")
TIES = ["GRGR_CK", "CSPD_CAT", "CSPI_ID", "PDPD_ID"]


def _ties_broken(match) -> str:
    read = [c.strip("[]") for c in match.group(1).split(", ")]
    order = TIES + [c for c in read if c not in TIES + ["MEME_ORIG_EFF_DT"]]
    return (f"WITH ranked AS ( SELECT {match.group(1)}, ROW_NUMBER() OVER (PARTITION BY MEME_CK "
            f"ORDER BY MEME_ORIG_EFF_DT DESC, {', '.join(order)}) AS _rn")
# The home address, read in one order, so the first per member is always the same.
_HOMES = re.compile(r"^(SELECT MEME_CK, SBAD_ZIP AS MEME_ZIP, .* FROM sdm\.member_address WHERE .*\))$")


def _as_fixed(sql: str) -> str:
    for before, after in FIXED_SQL:
        sql = sql.replace(before, after)
    sql = _NEWEST.sub(_ties_broken, sql)
    return _HOMES.sub(lambda m: m.group(1) + " ORDER BY MEME_CK, SBAD_ZIP, SBAD_CITY, SBAD_STATE",
                      sql)


def _same_sql():
    """Both loaders sent the same queries, in the same order, word for word
    (the old ones with the fixes made)."""
    assert [_as_fixed(s) for s in SENT["old"]] == SENT["new"]
    SENT["old"].clear()
    SENT["new"].clear()


# The claim columns the strip was always meant to trim. Under pandas 3 the old
# loader's strip never ran (it looked for object columns; text is str now).
STRIPPED = ["CSPI_ID", "PDPD_ID", "CLCL_CL_TYPE", "CLCL_NTWK_IND", "CLCL_CUR_STS", "GRGR_NAME",
            "PLDS_DESC", "IDCD_ID_PRIMARY", "IPCD_ID", "PRPR_MCTR_SPEC", "MEME_SEX", "PSCD_ID"]


def _stripped(rows):
    """The old claims frame as the strip meant it: text trimmed, NULL left NULL."""
    rows = rows.copy()
    for col in STRIPPED:
        if col not in rows.columns:
            continue
        # The old strip did run on an object column (pandas 3 reads one only
        # when it is all NULL); text columns it never reached.
        if rows[col].dtype != object and pd.api.types.is_string_dtype(rows[col]):
            rows[col] = rows[col].str.strip()
    return rows


def _in_order(grid, count, levels):
    """The old grid, equal counts put in the order of their levels, as the
    new grid always returns them (the old one returned them in any order)."""
    if grid.empty:
        return grid
    by = [count, *[c for c in levels if c in grid.columns]]
    return grid.sort_values(by, ascending=[False] + [True] * (len(by) - 1), kind="stable",
                            na_position="first").reset_index(drop=True)


def _outcome(call):
    try:
        return "ok", call()
    except Exception as e:  # noqa: BLE001 -- the refusal is what is compared
        return "refused", (type(e), str(e))


def _same_frame(old, new):
    if old is None or new is None:
        assert old is None and new is None
        return
    pd.testing.assert_frame_equal(old, new, check_exact=True)


# ── Members ───────────────────────────────────────────────────────────────────

MEMBER_CASES = [
    {},
    {"grgr_ck": [101]},
    {"grgr_ck": ["101", "", None, "x", 202.0]},
    {"grgr_ck": [101], "cspd_cat": ["MED"]},
    {"grgr_ck": [202], "cspd_cat": ["MED", "DEN"], "cspi_id": ["PLN2"]},
    {"cspd_cat": [""]},
    {"cspi_id": ["O'BRIEN"], "pdpd_id": ["PRD1"]},
    {"cspi_id": [ATTACK]},
    {"grgr_ck": [999]},
    {"grgr_ck": ["", None]},
    {"pdpd_id": []},
]


@pytest.mark.parametrize("case", MEMBER_CASES, ids=[str(c) for c in MEMBER_CASES])
@pytest.mark.parametrize("as_of", [None, "2024-06-30"])
def test_members_load_as_they_did(case, as_of):
    assert entity_loader.where("members", case) == old_members._build_where(**case)
    old = _outcome(lambda: old_members.load_filtered_members(**case, reference_date=as_of))
    new = _outcome(lambda: entity_loader.load("members", case, reference_date=as_of))
    assert old[0] == new[0], (old, new)
    _same_sql()
    if old[0] == "refused":
        assert old[1] == new[1]
        return
    rows, labels, family, used = old[1]
    loaded = new[1]
    _same_frame(rows, loaded.rows)
    _same_frame(labels, loaded.labels)
    _same_frame(family, loaded.family)
    assert used == loaded.filters
    assert not rows.empty and "_age" in rows and "MEME_ZIP" in rows


@pytest.mark.parametrize("missing", [
    {"sdm.member_denorm": {"MEME_MARITAL_STATUS", "MEME_ORIG_EFF_DT"}},
    {"sdm.member_address": {"SBAD_ZIP"}},
    {"sdm.member_address": "all"},
], ids=["columns", "an-address-column", "no-address-table"])
def test_members_load_as_they_did_where_the_store_lacks_columns(store, missing, monkeypatch):
    # A customer's store need not carry every column the pack reads: both
    # loaders read only what is there, the same way.
    _, present = store
    have = {name: set(cols) for name, cols in present.items()}
    for name, gone in missing.items():
        have[name] = set() if gone == "all" else have[name] - gone
    for module in (old_members, entity_loader):
        monkeypatch.setattr(module, "_PRESENT", {k: set(v) for k, v in have.items()})
    old = _outcome(lambda: old_members.load_filtered_members(grgr_ck=[101]))
    new = _outcome(lambda: entity_loader.load("members", {"grgr_ck": [101]}))
    assert old[0] == new[0] == "ok", (old, new)
    _same_sql()
    _same_frame(old[1][0], new[1].rows)
    _same_frame(old[1][2], new[1].family)


# ── Claims ────────────────────────────────────────────────────────────────────

CLAIM_CASES = [
    {},
    {"grgr_ck": [101]},
    {"grgr_ck": ["12345", "", None, "  ", 202]},
    {"cspi_id": ["PLN1", " "], "pdpd_id": ["", "PRD2"]},
    {"claim_type": ["P", "I"]},
    {"gender": "F"},
    {"gender": ["M", ""]},
    {"age_min": "18", "age_max": 64},
    {"age_min": 0},
    {"age_min": "", "age_max": None},
    {"age_min": "abc"},
    {"diagnosis": ["E11'"], "procedure": ["9'"]},
    {"diagnosis": ["E11", "I10"], "grgr_ck": [303]},
    {"cspi_id": [ATTACK]},
    {"grgr_ck": [999]},
    {"cspd_cat": ["MED"]},
]


@pytest.mark.parametrize("case", CLAIM_CASES, ids=[str(c) for c in CLAIM_CASES])
def test_claims_load_as_they_did(case):
    old_where = _outcome(lambda: old_claims._build_claims_where(**case))
    new_where = _outcome(lambda: entity_loader.where("claims", case))
    assert old_where[0] == new_where[0], (old_where, new_where)
    if old_where[0] == "ok":
        assert old_where[1] == new_where[1]
    old = _outcome(lambda: old_claims.load_filtered_claims(**case))
    new = _outcome(lambda: entity_loader.load("claims", case))
    assert old[0] == new[0], (old, new)
    _same_sql()
    if old[0] == "refused":
        # A filter claims never took is refused by both, in their own words.
        assert old[1][0] == new[1][0]
        if old[1][0] is not TypeError:
            assert old[1] == new[1]
        return
    rows, used = old[1]
    _same_frame(_stripped(rows), new[1].rows)
    assert used == new[1].filters
    assert new[1].labels is None and new[1].family is None


# ── Grids and counts ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("depth", [0, 1, 2, 3, 9])
@pytest.mark.parametrize("minimum", [0, 5])
def test_combination_sizes_are_as_they_were(depth, minimum):
    _same_frame(_in_order(old_members.get_combination_sizes(max_depth=depth, min_members=minimum),
                          "n_members", ["GRGR_CK", "GRGR_NAME", "CSPD_CAT", "CSPD_CAT_DESC",
                                        "CSPI_ID"]),
                entity_loader.combination_sizes("members", max_depth=depth, minimum=minimum))
    _same_frame(_in_order(old_claims.get_claims_combination_sizes(max_depth=depth, min_claims=minimum),
                          "n_claims", ["GRGR_CK", "GRGR_NAME", "CSPI_ID", "PLDS_DESC", "PDPD_ID"]),
                entity_loader.combination_sizes("claims", max_depth=depth, minimum=minimum))
    _same_sql()


def test_overview_counts_are_as_they_were():
    assert entity_loader.overview("members") == old_members.get_overview_metrics()
    assert entity_loader.overview("claims") == old_claims.get_claims_overview.__wrapped__()
    _same_sql()


# ── What the old loaders did not do alike ─────────────────────────────────────

@pytest.mark.parametrize("entity, filters, clause", [
    ("members", {"cspd_cat": "MED"}, "WHERE CSPD_CAT IN ('MED')"),
    ("members", {"grgr_ck": "101"}, "WHERE GRGR_CK IN (101)"),
    ("members", {"grgr_ck": 101}, "WHERE GRGR_CK IN (101)"),
    ("claims", {"cspi_id": "PLN1"}, "WHERE CSPI_ID IN ('PLN1')"),
    ("claims", {"diagnosis": "E11", "procedure": "9'"},
     "WHERE IDCD_ID_PRIMARY IN ('E11') AND IPCD_ID IN ('9''')"),
    ("claims", {"grgr_ck": "12"}, "WHERE GRGR_CK IN (12)"),
    ("claims", {"gender": " "}, ""),
])
def test_one_value_sent_bare_is_one_value(entity, filters, clause):
    """The old loaders read a bare value apart: the claims loader took a bare
    gender as one value but split a bare plan into its letters, and the
    member loader split every one. Any filter now takes it as one value."""
    assert entity_loader.where(entity, filters) == clause


def test_a_filter_an_entity_does_not_have_is_refused():
    with pytest.raises(TypeError, match="members are not filtered by claim_type"):
        entity_loader.load("members", {"claim_type": ["P"]})
    with pytest.raises(TypeError, match="claims are not filtered by cspd_cat"):
        entity_loader.where("claims", {"cspd_cat": ["MED"]})


def test_the_connection_is_closed_when_a_query_fails(store, monkeypatch):
    connect, _ = store
    opened = []

    class Watched:
        def __init__(self):
            self.conn, self.closed = connect(), False
            opened.append(self)

        def close(self):
            self.closed = True
            self.conn.close()

    def broken(sql, conn, *rest):
        raise RuntimeError("the server went away")

    monkeypatch.setattr(entity_loader, "connect", Watched)
    monkeypatch.setattr(entity_loader, "read_sql", broken)
    for call in (lambda: entity_loader.load("members", {}),
                 lambda: entity_loader.load("claims", {}),
                 lambda: entity_loader.combination_sizes("claims"),
                 lambda: entity_loader.overview("members")):
        with pytest.raises(RuntimeError, match="went away"):
            call()
    assert len(opened) == 4 and all(c.closed for c in opened)


# ── Scope choices ─────────────────────────────────────────────────────────────
#
# The option lists were four functions per entity (data_loader.get_groups ...,
# claims_data_loader.get_claims_groups ...) and a conversion each in the
# services. entity_loader.options reads every level from the pack's
# loaders.<entity>.scope. Held to the old functions: the same SQL, then the
# old conversions' {key, label}, as the services made them.

def _members_said(frame, key, label):
    """service.groups / plan_categories / plans / products, as they were."""
    return [{"key": str(r[key]).strip(), "label": str(r[label]).strip()}
            for _, r in frame.iterrows()]


def _claims_said(frame, key, label=None):
    """claims_service._opts, as it was."""
    if frame is None or len(frame) == 0:
        return []
    out = []
    for _, r in frame.iterrows():
        k = str(r[key]).strip()
        text = str(r[label]).strip() if label and label in frame.columns else k
        out.append({"key": k, "label": text or k})
    return out


G, C, P, D = [101, 303], ["MED", "DEN"], ["PLN1", "O'BRIEN", ATTACK], ["PRD1"]
SCOPE_CASES = [
    ("members", "groups", {}, lambda: _members_said(old_members.get_groups(), "GRGR_CK", "GRGR_NAME")),
    *[("members", "plan-categories", f, lambda f=f: _members_said(
        old_members.get_plan_categories(f.get("grgr_ck")), "CSPD_CAT", "CSPD_CAT_DESC"))
      for f in ({}, {"grgr_ck": G})],
    *[("members", "plans", f, lambda f=f: _members_said(
        old_members.get_plans(f.get("grgr_ck"), f.get("cspd_cat")), "CSPI_ID", "PLDS_DESC"))
      for f in ({}, {"grgr_ck": G}, {"cspd_cat": C}, {"grgr_ck": G, "cspd_cat": [ATTACK]})],
    *[("members", "products", f, lambda f=f: _members_said(
        old_members.get_products(f.get("grgr_ck"), f.get("cspd_cat"), f.get("cspi_id")),
        "PDPD_ID", "PDDS_DESC"))
      for f in ({}, {"grgr_ck": G, "cspd_cat": C, "cspi_id": P}, {"cspi_id": ["PLN2"]})],
    ("claims", "groups", {}, lambda: _claims_said(old_claims.get_claims_groups(), "GRGR_CK", "GRGR_NAME")),
    *[("claims", "plans", f, lambda f=f: _claims_said(
        old_claims.get_claims_plans(f.get("grgr_ck")), "CSPI_ID", "PLDS_DESC"))
      for f in ({}, {"grgr_ck": G})],
    *[("claims", "products", f, lambda f=f: _claims_said(
        old_claims.get_claims_products(f.get("grgr_ck"), f.get("cspi_id")), "PDPD_ID", "PDDS_DESC"))
      for f in ({}, {"grgr_ck": G, "cspi_id": P})],
    *[("claims", "types", f, lambda f=f: _claims_said(
        old_claims.get_claims_types(f.get("grgr_ck"), f.get("cspi_id"), f.get("pdpd_id")),
        "CLCL_CL_TYPE", "Claim_Type"))
      for f in ({}, {"grgr_ck": G}, {"grgr_ck": G, "cspi_id": P, "pdpd_id": D})],
]


@pytest.mark.parametrize("entity, level, filters, before", SCOPE_CASES,
                         ids=[f"{e}-{lv}-{sorted(f)}" for e, lv, f, _ in SCOPE_CASES])
def test_scope_choices_are_as_they_were(entity, level, filters, before):
    old = before()
    new = entity_loader.options(entity, level, entity_loader.given(**filters))
    _same_sql()
    assert new == old
    if not filters:
        assert new, "an unfiltered level that reads nothing proves little"


# The services' wrappers, which the old routes (/api/hierarchy/*, /api/claims/*)
# call: each level through its own wrapper, every argument it takes, held to
# the old function it replaced.
def _wrapped():
    from sdg.analyzer.services import claims_service as cs, service as ms

    return [
        (lambda: ms.groups(),
         lambda: _members_said(old_members.get_groups(), "GRGR_CK", "GRGR_NAME")),
        (lambda: ms.plan_categories(tuple(G)),
         lambda: _members_said(old_members.get_plan_categories(G), "CSPD_CAT", "CSPD_CAT_DESC")),
        (lambda: ms.plans(tuple(G), tuple(C)),
         lambda: _members_said(old_members.get_plans(G, C), "CSPI_ID", "PLDS_DESC")),
        (lambda: ms.products(tuple(G), tuple(C), ("PLN1", "PLN2")),
         lambda: _members_said(old_members.get_products(G, C, ["PLN1", "PLN2"]),
                               "PDPD_ID", "PDDS_DESC")),
        (lambda: cs.groups(),
         lambda: _claims_said(old_claims.get_claims_groups(), "GRGR_CK", "GRGR_NAME")),
        (lambda: cs.plans(tuple(G)),
         lambda: _claims_said(old_claims.get_claims_plans(G), "CSPI_ID", "PLDS_DESC")),
        (lambda: cs.products(tuple(G), ("PLN1",)),
         lambda: _claims_said(old_claims.get_claims_products(G, ["PLN1"]), "PDPD_ID", "PDDS_DESC")),
        (lambda: cs.claim_types(tuple(G), ("PLN1",), ("PRD1",)),
         lambda: _claims_said(old_claims.get_claims_types(G, ["PLN1"], ["PRD1"]),
                              "CLCL_CL_TYPE", "Claim_Type")),
        (lambda: cs.claim_types(tuple(G), ("PLN1",), ("PRD2",)),
         lambda: _claims_said(old_claims.get_claims_types(G, ["PLN1"], ["PRD2"]),
                              "CLCL_CL_TYPE", "Claim_Type")),
    ]


@pytest.mark.parametrize("at", range(9))
def test_every_old_scope_route_reads_what_it_read(at):
    new, old = _wrapped()[at]
    before = old()
    after = new()
    _same_sql()
    assert after == before


def test_a_blank_choice_narrows_to_the_blank_rows():
    # A claim filter skips a blank value; a blank scope choice is sent, as the
    # old lists sent it: choosing the blank plan reads the blank plan's products.
    entity_loader.options("claims", "products", entity_loader.given(grgr_ck=[101], cspi_id=[""]))
    assert "WHERE GRGR_CK IN (101) AND CSPI_ID IN ('')" in SENT["new"][-1]
    assert "CSPI_ID" not in entity_loader.where("claims", {"cspi_id": [""]})


def test_a_choice_with_no_label_is_labeled_by_its_key(monkeypatch):
    # Deliberate: members' blank labels were shown blank; now, as claims' were,
    # by their key. A NULL label was shown as "None" (pandas 3: "nan"); now by
    # its key. A NULL key was offered as "None" and narrowed to nothing; now
    # it is not offered.
    frame = pd.DataFrame({"CSPI_ID": ["P1 ", "P2", "P3", None],
                          "PLDS_DESC": ["  ", "Two ", None, "Orphan"]})
    monkeypatch.setattr(entity_loader, "read_sql", lambda sql, conn: frame)
    assert entity_loader.options("members", "plans") == [
        {"key": "P1", "label": "P1"}, {"key": "P2", "label": "Two"},
        {"key": "P3", "label": "P3"}]
    groups = pd.DataFrame({"GRGR_CK": [7, None], "GRGR_NAME": [None, "x"]})
    monkeypatch.setattr(entity_loader, "read_sql", lambda sql, conn: groups)
    # A number column holding a NULL reads as floats: the key is still "7".
    assert entity_loader.options("claims", "groups") == [{"key": "7", "label": "7"}]


def test_scope_choices_read_only_the_levels_above():
    # A level narrows by the levels its pack entry takes, whatever else is sent.
    every = entity_loader.given(grgr_ck=[101], cspd_cat=["MED"], cspi_id=["PLN1"], pdpd_id=["PRD1"])
    entity_loader.options("members", "plan-categories", every)
    assert SENT["new"][-1].endswith("WHERE GRGR_CK IN (101) ORDER BY CSPD_CAT_DESC")
    entity_loader.options("members", "groups", every)
    assert "WHERE" not in SENT["new"][-1]


def test_the_scope_levels_are_the_packs():
    assert entity_loader.scope_levels("members") == [
        {"level": "groups", "takes": []},
        {"level": "plan-categories", "takes": ["grgr_ck"]},
        {"level": "plans", "takes": ["grgr_ck", "cspd_cat"]},
        {"level": "products", "takes": ["grgr_ck", "cspd_cat", "cspi_id"]}]
    assert [s["level"] for s in entity_loader.scope_levels("claims")] == [
        "groups", "plans", "products", "types"]
    with pytest.raises(LookupError, match="Claims have no scope level 'plan-categories'"):
        entity_loader.options("claims", "plan-categories")


def test_an_entity_the_pack_does_not_read_is_refused():
    for name in ("subscribers", "_about", "nothing"):
        with pytest.raises(ValueError, match="reads no"):
            entity_loader.load(name, {})


# ── The real store ────────────────────────────────────────────────────────────

def _same_grid(old, new):
    """Grids hold the same rows. SQL Server returns a GROUP BY in no set order
    and the grid sorts by count alone, so rows with equal counts come back in
    any order -- read twice, the old loader's claims grid differs from itself.
    Ties are put in one order before comparing; every row and count is held."""
    def ordered(frame):
        return frame.sort_values(list(frame.columns), kind="stable",
                                 na_position="first").reset_index(drop=True)
    _same_frame(ordered(old), ordered(new))

@pytest.mark.db
def test_the_real_store_loads_as_it_did(monkeypatch):
    """Read only: the same scopes, read both ways from the pattern store."""
    from sdg.analyzer.config import get_connection_string
    import pyodbc

    def real():
        return pyodbc.connect(get_connection_string())

    for module, name in ((old_members, "_conn"), (old_claims, "_conn"),
                         (entity_loader, "connect")):
        monkeypatch.setattr(module, name, real)
    for module in (old_members, entity_loader):
        monkeypatch.setattr(module, "_PRESENT", {})

    grid = entity_loader.combination_sizes("members", max_depth=2, minimum=0)
    _same_grid(old_members.get_combination_sizes(max_depth=2, min_members=0), grid)
    groups = [int(g) for g in grid.sort_values("n_members")["GRGR_CK"].drop_duplicates()][:3]
    for group in groups:
        old = old_members.load_filtered_members(grgr_ck=[group])
        new = entity_loader.load("members", {"grgr_ck": [group]})
        for before, after in zip((old[0], old[1], old[2]), (new.rows, new.labels, new.family)):
            _same_frame(before, after)
        assert old[3] == new.filters

    claims_grid = entity_loader.combination_sizes("claims", max_depth=2, minimum=0)
    _same_grid(old_claims.get_claims_combination_sizes(max_depth=2, min_claims=0), claims_grid)
    for group in [int(g) for g in claims_grid.sort_values("n_claims")["GRGR_CK"]
                  .drop_duplicates()][:3]:
        old_rows, old_used = old_claims.load_filtered_claims(grgr_ck=[group])
        new = entity_loader.load("claims", {"grgr_ck": [group]})
        _same_frame(_stripped(old_rows), new.rows)
        assert old_used == new.filters

    # The scope's choices, every level, narrowed by the first group.
    group = groups[0]
    assert entity_loader.options("members", "groups") == _members_said(
        old_members.get_groups(), "GRGR_CK", "GRGR_NAME")
    assert entity_loader.options("members", "plan-categories", entity_loader.given(
        grgr_ck=[group])) == _members_said(old_members.get_plan_categories([group]),
                                           "CSPD_CAT", "CSPD_CAT_DESC")
    assert entity_loader.options("members", "plans", entity_loader.given(
        grgr_ck=[group])) == _members_said(old_members.get_plans([group]), "CSPI_ID", "PLDS_DESC")
    assert entity_loader.options("members", "products", entity_loader.given(
        grgr_ck=[group])) == _members_said(old_members.get_products([group]),
                                           "PDPD_ID", "PDDS_DESC")
    assert entity_loader.options("claims", "groups") == _claims_said(
        old_claims.get_claims_groups(), "GRGR_CK", "GRGR_NAME")
    for level, before in (("plans", lambda: _claims_said(
                              old_claims.get_claims_plans([group]), "CSPI_ID", "PLDS_DESC")),
                          ("products", lambda: _claims_said(
                              old_claims.get_claims_products([group]), "PDPD_ID", "PDDS_DESC")),
                          ("types", lambda: _claims_said(
                              old_claims.get_claims_types([group]), "CLCL_CL_TYPE", "Claim_Type"))):
        assert entity_loader.options("claims", level, entity_loader.given(grgr_ck=[group]))             == before(), level


@pytest.mark.parametrize("key, before", [("diagnoses", "get_top_diagnoses"),
                                         ("procedures", "get_top_procedures")])
@pytest.mark.parametrize("n", [6, 30])
def test_highlights_send_the_sql_they_sent(key, before, n, monkeypatch):
    """SQLite has no TOP: the query is held equal as text, and the answer the
    route made of a frame (code, description, n by column name) on one frame."""
    sent = {"old": [], "new": []}
    frame = pd.DataFrame({"C": ["E11", "I10"], "D": ["Diabetes ", None], "cnt": [9, 4]})

    def answered(side, columns):
        def read(sql, conn, *rest):
            sent[side].append(" ".join(sql.split()))
            return frame.set_axis(columns, axis=1)
        return read

    spec = next(h for h in entity_loader.highlights("claims") if h["key"] == key)
    columns = [spec["code"], spec["description"], "cnt"]
    monkeypatch.setattr(old_claims, "read_sql", answered("old", columns))
    monkeypatch.setattr(entity_loader, "read_sql", answered("new", columns))
    old = getattr(old_claims, before).__wrapped__(n)
    new = entity_loader.highlight.__wrapped__("claims", key, n)
    assert sent["old"] == sent["new"] and len(sent["new"]) == 1
    # What the old route made of the old frame (routers/claims.py _codes).
    assert new == [{"code": str(r[columns[0]]).strip(), "description": str(r[columns[1]]).strip(),
                    "n": int(r["cnt"])} for _, r in old.iterrows()]


def test_the_highlights_are_the_packs():
    assert [(h["key"], h["code"]) for h in entity_loader.highlights("claims")] == [
        ("diagnoses", "IDCD_ID_PRIMARY"), ("procedures", "IPCD_ID")]
    assert entity_loader.highlights("members") == []
    with pytest.raises(ValueError, match="no highlight 'nothing'"):
        entity_loader.highlight.__wrapped__("claims", "nothing")


@pytest.mark.db
def test_the_real_store_highlights_as_it_did(monkeypatch):
    """Read only: the top diagnoses and procedures, read both ways."""
    from sdg.analyzer.config import get_connection_string
    import pyodbc

    def real():
        return pyodbc.connect(get_connection_string())

    monkeypatch.setattr(old_claims, "_conn", real)
    monkeypatch.setattr(entity_loader, "connect", real)
    for key, before in (("diagnoses", "get_top_diagnoses"), ("procedures", "get_top_procedures")):
        old = getattr(old_claims, before).__wrapped__(8)
        code, desc = old.columns[0], old.columns[1]
        assert entity_loader.highlight.__wrapped__("claims", key, 8) == [
            {"code": str(r[code]).strip(), "description": str(r[desc]).strip(),
             "n": int(r["cnt"] or 0)} for _, r in old.iterrows()], key


# ── The loader fixes, each on its own ─────────────────────────────────────────

def test_claim_text_is_trimmed_and_null_stays_null():
    rows = entity_loader.load("claims", {}).rows
    old = old_claims.load_filtered_claims()[0]
    for col in STRIPPED:
        if col not in rows.columns:
            continue
        text = rows[col].dropna().astype(str)
        assert not text.str.endswith(" ").any(), col
        assert not text.isin(["None", "nan"]).any(), col
        assert rows[col].isna().sum() == old[col].isna().sum(), col
    assert (old["IDCD_ID_PRIMARY"] == "I10 ").any() and (rows["IDCD_ID_PRIMARY"] == "I10").any()


def test_equally_newest_enrollments_pick_the_same_one(tmp_path, monkeypatch):
    path = tmp_path / "sdm.db"
    with sqlite3.connect(path) as conn:
        pd.DataFrame([
            {"MEME_CK": 1, "MEME_ORIG_EFF_DT": "2020-01-01", "GRGR_CK": 303, "CSPD_CAT": "MED",
             "CSPI_ID": "PLN1", "PDPD_ID": "PRD1", "SBSB_CK": 9, "MEME_REL": "M"},
            {"MEME_CK": 1, "MEME_ORIG_EFF_DT": "2020-01-01", "GRGR_CK": 101, "CSPD_CAT": "MED",
             "CSPI_ID": "PLN2", "PDPD_ID": "PRD1", "SBSB_CK": 9, "MEME_REL": "M"},
            {"MEME_CK": 1, "MEME_ORIG_EFF_DT": "2019-01-01", "GRGR_CK": 100, "CSPD_CAT": "MED",
             "CSPI_ID": "PLN0", "PDPD_ID": "PRD1", "SBSB_CK": 9, "MEME_REL": "M"},
        ]).to_sql("member_denorm", conn, index=False)

    def connect():
        conn = sqlite3.connect(":memory:")
        conn.execute(f"ATTACH DATABASE '{path.as_posix()}' AS sdm")
        return conn

    monkeypatch.setattr(entity_loader, "connect", connect)
    monkeypatch.setattr(entity_loader, "_PRESENT", {
        "sdm.member_denorm": {"MEME_CK", "MEME_ORIG_EFF_DT", "GRGR_CK", "CSPD_CAT", "CSPI_ID",
                              "PDPD_ID"},
        "sdm.member_address": set()})
    # The newest date, then the lowest group: whichever order the rows were stored in.
    assert entity_loader.load("members", {}).rows["GRGR_CK"].tolist() == [101]


def test_equal_counts_come_in_the_order_of_their_levels():
    grid = entity_loader.combination_sizes("claims", max_depth=3)
    for _, same in grid.groupby("n_claims", sort=False):
        keys = same[["GRGR_CK", "GRGR_NAME", "CSPI_ID", "PLDS_DESC", "PDPD_ID"]]
        assert keys.equals(keys.sort_values(list(keys.columns), kind="stable",
                                            na_position="first")), same
    assert grid["n_claims"].is_monotonic_decreasing


@pytest.mark.parametrize("ids", [["abc"], "1,2", [" x ", None, ""]])
def test_ids_with_none_usable_are_refused_not_read_as_everything(ids):
    with pytest.raises(ValueError, match="grgr_ck takes numbers"):
        entity_loader.where("members", {"grgr_ck": ids})
    with pytest.raises(ValueError, match="grgr_ck takes numbers"):
        entity_loader.load("claims", {"grgr_ck": ids})


@pytest.mark.parametrize("ids", [["", None], [], None, ["1", "x"]])
def test_ids_left_blank_or_partly_usable_are_as_before(ids):
    expected = "WHERE GRGR_CK IN (1)" if ids == ["1", "x"] else ""
    assert entity_loader.where("members", {"grgr_ck": ids}) == expected


def test_a_table_not_there_yet_is_asked_for_again(monkeypatch):
    answers = [pd.DataFrame({"COLUMN_NAME": []}), pd.DataFrame({"COLUMN_NAME": ["A", "B"]})]
    asked = []
    monkeypatch.setattr(entity_loader, "_PRESENT", {})
    monkeypatch.setattr(entity_loader, "read_sql",
                        lambda sql, conn: asked.append(sql) or answers[len(asked) - 1])
    assert entity_loader.present_columns("sdm.later") == set()
    assert entity_loader.present_columns("sdm.later") == {"A", "B"}
    assert entity_loader.present_columns("sdm.later") == {"A", "B"}
    assert len(asked) == 2


def test_trimming_keeps_null_and_leaves_numbers_alone():
    trim = entity_loader.trimmed
    as_text = trim(pd.Series(["A1534  ", None, " x"], dtype=object))
    assert as_text.tolist()[0] == "A1534" and as_text.tolist()[2] == "x"
    assert as_text.isna().tolist() == [False, True, False]
    assert not as_text.astype(object).isin(["None", "nan"]).any()
    assert trim(pd.Series([None, None], dtype=object)).isna().all()
    assert trim(pd.Series(["b ", None], dtype="str")).tolist()[0] == "b"
    numbers = pd.Series([1.5, None])
    assert trim(numbers) is numbers


def test_every_cached_analysis_keys_on_the_loader_version(monkeypatch):
    # A loader that returns other values must not be answered for from a cache
    # its predecessor filled.
    from sdg.analyzer.services import disk_cache

    monkeypatch.setattr(disk_cache, "_FINGERPRINT", None)
    first = disk_cache.data_fingerprint()
    assert f"|read:{entity_loader.READ_VERSION}" in first
    monkeypatch.setattr(disk_cache, "_FINGERPRINT", None)
    monkeypatch.setattr(entity_loader, "READ_VERSION", entity_loader.READ_VERSION + 1)
    assert disk_cache.data_fingerprint() != first


@pytest.mark.parametrize("stored", ["as_given", "reversed"])
def test_a_tie_on_every_scope_level_is_broken_by_what_else_is_read(tmp_path, monkeypatch, stored):
    rows = [{"MEME_CK": 1, "MEME_ORIG_EFF_DT": "2020-01-01", "GRGR_CK": 101, "CSPD_CAT": "MED",
             "CSPI_ID": "PLN1", "PDPD_ID": "PRD1", "SBSB_CK": sbsb, "MEME_REL": rel}
            for sbsb, rel in ((9, "S"), (7, "M"), (8, "D"))]
    if stored == "reversed":
        rows.reverse()
    path = tmp_path / "sdm.db"
    with sqlite3.connect(path) as conn:
        pd.DataFrame(rows).to_sql("member_denorm", conn, index=False)

    def connect():
        conn = sqlite3.connect(":memory:")
        conn.execute(f"ATTACH DATABASE '{path.as_posix()}' AS sdm")
        return conn

    monkeypatch.setattr(entity_loader, "connect", connect)
    monkeypatch.setattr(entity_loader, "_PRESENT", {
        "sdm.member_denorm": set(rows[0]), "sdm.member_address": set()})
    picked = entity_loader.load("members", {}).rows[["SBSB_CK", "MEME_REL"]].values.tolist()
    assert picked == [[7, "M"]]
