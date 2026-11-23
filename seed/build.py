"""Build the FACETS_QA and MDM_REF fixtures for the SDG proof of concept.

The data carries patterns planted on purpose. Every later build step is tested
against them, so this script is the fixture for the whole PoC. See
seed/PATTERNS.md for the list and seed/verify.py for the checks.

Usage:
    python seed/build.py --small      1 percent volumes, for fast test runs
    python seed/build.py --medium     25 percent volumes, the development default
    python seed/build.py              full volumes
    python seed/build.py --scale 0.5  any fraction
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdg.config import get_settings, not_a_demo_machine, refuse_protected  # noqa: E402
from seed import hierarchy as hier  # noqa: E402
from seed import networks as nets  # noqa: E402
from seed import reference_data as ref  # noqa: E402
from seed.npi import make_npi  # noqa: E402

SEED = 20260915
DATA_AS_OF = date(2026, 6, 30)
BATCH = 10_000

# Full volumes. Providers and products keep a floor at small scales so the
# low-cardinality columns still have enough distinct values to profile.
FULL_SUBSCRIBERS = 20_000
FULL_PROVIDERS = 300
PRODUCTS = 25
PLAN_CODES = 40
INACTIVE_PLAN_CODES = 3
PROVIDER_FLOOR = 50

# Members per subscriber; the weights average 2.31, giving about 46,000 members.
MEMBERS_PER_SUBSCRIBER = [1, 2, 3, 4, 5]
MEMBERS_PER_SUBSCRIBER_W = [0.28, 0.34, 0.22, 0.11, 0.05]

# Claims per member. 30 percent of members have none. The rest follow a
# log-normal long tail: median 5, 90th percentile 14, clamped at 40.
ZERO_CLAIM_SHARE = 0.30
CLAIM_LOGNORM_MU = math.log(5.3)
CLAIM_LOGNORM_SIGMA = 0.804
CLAIM_COUNT_MAX = 40


LINES_PER_CLAIM = [1, 2, 3, 4, 5]
LINES_PER_CLAIM_W = [0.10, 0.20, 0.35, 0.22, 0.13]

MEME_TERM_NULL_SHARE = 0.85
CLCL_PAID_NULL_SHARE = 0.20
RECD_GAP_DAYS = (0, 30)
PAID_GAP_DAYS = (5, 60)

SERVICE_CODE_POOL = 400

PROCEDURE_WORDS = [
    "Office visit", "Diagnostic imaging", "Laboratory panel", "Preventive screening",
    "Physical therapy", "Minor procedure", "Specialist consultation", "Immunization",
    "Emergency assessment", "Dental examination", "Radiology review", "Care management",
]
# CLCL_ID is a 12-digit business key. Multiplying the row index by a value
# coprime with 10**12 gives unique, non-sequential identifiers.
CLCL_STRIDE = 982_451_653
CLCL_OFFSET = 137


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run_sql_script(conn: pyodbc.Connection, path: Path) -> None:
    """Execute a .sql file, splitting on GO batch separators. Its guard is
    given the protected databases' names first (sdg.config.fill_protected)."""
    from sdg.config import fill_protected

    text = fill_protected(path.read_text(encoding="utf-8"))
    batches = [
        b.strip()
        for b in (line for line in _split_on_go(text))
        if b.strip()
    ]
    cur = conn.cursor()
    for batch in batches:
        cur.execute(batch)
        # Every statement's result, read to the end: the driver carries out a
        # batch's later statements only as their results are read, and a
        # failure in one of them is only raised then.
        while cur.nextset():
            pass
    conn.commit()


def _split_on_go(text: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip().upper() == "GO":
            out.append("\n".join(current))
            current = []
        else:
            current.append(line)
    out.append("\n".join(current))
    return out


def insert_many(
    conn: pyodbc.Connection,
    table: str,
    columns: list[str],
    rows: list[tuple],
    *,
    identity_insert: bool = False,
) -> None:
    if not rows:
        return
    cur = conn.cursor()
    cur.fast_executemany = True
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(columns)
    statement = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"
    if identity_insert:
        cur.execute(f"SET IDENTITY_INSERT {table} ON")
    for start in range(0, len(rows), BATCH):
        cur.executemany(statement, rows[start : start + BATCH])
    if identity_insert:
        cur.execute(f"SET IDENTITY_INSERT {table} OFF")
    conn.commit()


# --------------------------------------------------------------------------
# Row builders
# --------------------------------------------------------------------------


def build_plan_codes(rng: random.Random) -> list[tuple]:
    rows = []
    for i in range(1, PLAN_CODES + 1):
        code = f"PL{i:04d}"
        name = (
            f"{rng.choice(ref.PLAN_NAME_PREFIXES)} "
            f"{rng.choice(ref.PLAN_NAME_SUFFIXES)} {1000 + i}"
        )
        active = "N" if i > PLAN_CODES - INACTIVE_PLAN_CODES else "Y"
        rows.append((code, name, active))
    return rows


def build_products(rng: random.Random) -> list[tuple]:
    """Product identifiers shared between FACETS_QA and MDM_REF."""
    rows = []
    for i in range(1, PRODUCTS + 1):
        # A fixed share of products are dental so the mix is reproducible.
        kind = "D" if i % 5 == 0 else "M"
        rows.append((f"PD{i:06d}", kind))
    return rows


def build_providers(count: int, rng: random.Random) -> list[tuple]:
    rows = []
    for i in range(1, count + 1):
        specialty = rng.choice(ref.SPECIALTIES)
        rows.append(
            (
                f"PRV{i:09d}",
                make_npi(1_000_000_000 - i * 7919),
                specialty,
                rng.choice(ref.STATES),
                nets.entity(specialty, i),
            )
        )
    return rows


def random_letters(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(n))


def build_subscribers(
    count: int, rng: random.Random, subgroups_by_group: dict[int, list[int]],
    plans_by_type: dict[str, list[str]],
) -> list[tuple]:
    """One row per subscriber, placed in a group, a subgroup, and a plan.

    The group decides the plan: a retail group offering dental only never
    appears against a medical plan, which is what makes a hierarchy selection
    in the analyzer mean something.
    """
    rows = []
    span = (date(2024, 6, 30) - date(2018, 1, 1)).days
    for i in range(1, count + 1):
        grgr_ck, group = hier.assign_group(rng)
        sgsg_ck = rng.choice(subgroups_by_group[grgr_ck])
        if group["products"] == "both":
            product_type = rng.choices(["M", "D"], [0.78, 0.22])[0]
        else:
            product_type = "M" if group["products"] == "medical" else "D"
        cspi_id = rng.choice(plans_by_type[product_type])

        eff = date(2018, 1, 1) + timedelta(days=rng.randint(0, span))
        orig_eff = min(eff, hier.tenure_start(group, rng, DATA_AS_OF))
        rows.append(
            (
                i,
                f"{random_letters(rng, 3)}{i:06d}",
                rng.choice(ref.LAST_NAMES),
                rng.choice(ref.FIRST_NAMES),
                eff,
                orig_eff,
                "AC",
                f"EMP{i:07d}",
                grgr_ck,
                sgsg_ck,
                cspi_id,
            )
        )
    return rows


def build_members(
    subscribers: list[tuple],
    rng: random.Random,
    triples: list[tuple[str, str, str]],
) -> list[tuple]:
    """One row per member, keyed by a deterministic MEME_CK.

    Age, sex and marital status come from the subscriber's group, so the
    population inside each group has a shape of its own for the analyzer to
    find. A dependent child is young and single whatever the group looks like.
    """
    rows = []
    meme_ck = 0
    for subscriber in subscribers:
        sbsb_ck = subscriber[0]
        sbsb_last = subscriber[2]
        sbsb_eff = subscriber[4]
        grgr_ck = subscriber[8]
        group = hier.GROUPS[grgr_ck - 1]

        n_members = rng.choices(MEMBERS_PER_SUBSCRIBER, MEMBERS_PER_SUBSCRIBER_W)[0]
        zip_code, city, state = rng.choice(triples)
        for slot in range(n_members):
            meme_ck += 1
            relationship = "M" if slot == 0 else rng.choices(
                ref.RELATIONSHIP_CODES[1:], ref.RELATIONSHIP_WEIGHTS[1:]
            )[0]
            sex, marital, birth = hier.demographics(group, relationship, rng, DATA_AS_OF)
            # Coverage cannot start before the member exists. Drawing the two
            # independently gave six per cent of members a policy that predates
            # their birth, and claims with a negative age at service -- which is
            # what the claims analyzer reported before this was fixed.
            eff = max(sbsb_eff + timedelta(days=rng.randint(0, 365)), birth)
            if rng.random() < MEME_TERM_NULL_SHARE:
                term = None
            else:
                term = eff + timedelta(days=rng.randint(180, 1800))
            rows.append(
                (
                    meme_ck,
                    sbsb_ck,
                    # A member is identified the Facets way: the subscriber's
                    # ID and the member's two-digit suffix.
                    f"{subscriber[1]}{slot:02d}",
                    rng.choice(ref.FIRST_NAMES),
                    sbsb_last if relationship in ("M", "D") else rng.choice(ref.LAST_NAMES),
                    rng.choice("ABCDEFGHJKLMNPRSTW") if rng.random() < 0.6 else None,
                    birth,
                    sex,
                    marital,
                    hier.ssn(rng),
                    rng.choice(hier.MEMBER_STATUS_CODES),
                    relationship,
                    eff,
                    min(eff, subscriber[5] + timedelta(days=rng.randint(0, 120))),
                    term,
                    zip_code,
                    city,
                    state,
                    # The member's place within the subscriber: 0 for the
                    # subscriber, as FACETS numbers them.
                    slot,
                )
            )
    return rows


def draw_claim_counts(n_members: int, dental_flags: np.ndarray, gen: np.random.Generator) -> np.ndarray:
    """Claims per member: 30 percent zero, the rest a clamped log-normal tail."""
    has_claims = gen.random(n_members) >= ZERO_CLAIM_SHARE
    raw = gen.lognormal(CLAIM_LOGNORM_MU, CLAIM_LOGNORM_SIGMA, n_members)
    counts = np.clip(np.rint(raw), 1, CLAIM_COUNT_MAX).astype(np.int32)
    # Dental members carry about half as many claims.
    counts = np.where(dental_flags, (counts + 1) // 2, counts)
    return np.where(has_claims, counts, 0)


def build_service_code_pool(rng: random.Random) -> tuple[list[str], list[str]]:
    """Return (all codes, codes usable on dental claims)."""
    letters = ref.SHARED_SERVICE_LETTERS + ref.MEDICAL_ONLY_LETTERS
    codes: set[str] = set()
    while len(codes) < SERVICE_CODE_POOL:
        codes.add(f"{rng.choice(letters)}{rng.randint(0, 9999):04d}")
    all_codes = sorted(codes)
    dental_codes = [c for c in all_codes if c[0] not in ref.MEDICAL_ONLY_LETTERS]
    return all_codes, dental_codes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--small", action="store_true", help="1 percent volumes")
    group.add_argument("--medium", action="store_true", help="25 percent volumes")
    group.add_argument("--scale", type=float, help="custom fraction of full volumes")
    # No --keep: the fixture's DDL always drops and recreates its tables, so an
    # option claiming to keep them would be false. argparse refuses it.
    args = parser.parse_args(argv)

    scale = 1.0
    label = "full"
    if args.small:
        scale, label = 0.01, "small"
    elif args.medium:
        scale, label = 0.25, "medium"
    elif args.scale is not None:
        scale, label = args.scale, f"scale={args.scale}"

    started = time.time()
    settings = get_settings()
    # Refused before connecting: ddl_mdm.sql and ddl_facets.sql drop every
    # REF_* and CMC_* table in these databases.
    reason = not_a_demo_machine()
    if reason:
        raise SystemExit(f"This machine is {reason}: the demo build drops every table "
                         f"in {settings.ref_db} and {settings.source_db}. Nothing was changed.")
    for database in (settings.ref_db, settings.source_db):
        refuse_protected(database, "drop and rebuild the tables in")
    rng = random.Random(SEED)
    gen = np.random.default_rng(SEED)

    n_subscribers = max(1, round(FULL_SUBSCRIBERS * scale))
    n_providers = max(PROVIDER_FLOOR, round(FULL_PROVIDERS * scale))

    log(f"building {label} fixture: {n_subscribers:,} subscribers, {n_providers:,} providers")

    seed_dir = Path(__file__).resolve().parent

    # The databases first. Filling one that does not exist fails with a
    # connection error naming a database nobody was told to create, which is a
    # poor first thing to meet on a new machine.
    from bootstrap import ensure_databases, ensure_store

    ensure_databases(settings)
    ensure_store(settings)

    # ---- MDM_REF -------------------------------------------------------
    ref_conn = pyodbc.connect(settings.mssql.connection_string(settings.ref_db), autocommit=False)
    try:
        log(f"creating schema in {settings.ref_db}")
        run_sql_script(ref_conn, seed_dir / "ddl_mdm.sql")
        plan_rows = build_plan_codes(rng)
        product_rows = build_products(rng)
        insert_many(ref_conn, "dbo.REF_PLAN_CODE", ["PLAN_CD", "PLAN_NAME", "ACTIVE_FLAG"], plan_rows)
        insert_many(ref_conn, "dbo.REF_PRODUCT_CODE", ["PRODUCT_CD", "PRODUCT_TYPE"],
                    [(pid, kind) for pid, kind in product_rows])
        log(f"  REF_PLAN_CODE {len(plan_rows)} rows, REF_PRODUCT_CODE {len(product_rows)} rows")
    finally:
        ref_conn.close()

    # ---- FACETS_QA -----------------------------------------------------
    conn = pyodbc.connect(settings.mssql.connection_string(settings.source_db), autocommit=False)
    try:
        log(f"creating schema in {settings.source_db}")
        run_sql_script(conn, seed_dir / "ddl_facets.sql")

        insert_many(conn, "dbo.CMC_IDCD_DIAG_CODE",
                    ["IDCD_ID", "IDCD_DESC", "IDCD_CHAPTER"], ref.DIAGNOSES)

        providers = build_providers(n_providers, rng)
        insert_many(conn, "dbo.CMC_PRPR_PROV",
                    ["PRPR_ID", "PRPR_NPI", "PRPR_SPECIALTY", "PRPR_STATE", "PRPR_ENTITY"],
                    providers)
        insert_many(conn, "dbo.CMC_NWNW_NETWORK", ["NWNW_ID", "NWNW_NAME", "NWNW_MCTR_TYPE"],
                    nets.network_rows())
        relation_rows = nets.relations(providers, rng, DATA_AS_OF)
        insert_many(conn, "dbo.CMC_NWPR_RELATION",
                    ["PRPR_ID", "NWNW_ID", "NWPR_PFX", "NWPR_PCP_IND", "NWPR_EFF_DT",
                     "NWPR_TERM_DT", "AGAG_ID"], relation_rows)
        insert_many(conn, "dbo.CMC_PDPD_PRODUCT", ["PDPD_ID", "PDPD_TYPE"], product_rows)
        log(f"  providers {len(providers):,}, products {len(product_rows)}")

        groups = hier.group_rows(rng, DATA_AS_OF)
        insert_many(conn, "dbo.CMC_GRGR_GROUP",
                    ["GRGR_CK", "GRGR_ID", "GRGR_NAME", "GRGR_STATE", "GRGR_COUNTY",
                     "GRGR_STS", "GRGR_MCTR_TYPE", "GRGR_ORIG_EFF_DT", "GRGR_TERM_DT"],
                    groups, identity_insert=True)

        subgroups = hier.subgroup_rows(rng, DATA_AS_OF)
        insert_many(conn, "dbo.CMC_SGSG_SUBGROUP",
                    ["SGSG_CK", "GRGR_CK", "SGSG_ID", "SGSG_NAME", "CSCS_ID",
                     "SGSG_STATE", "SGSG_STS", "SGSG_ORIG_EFF_DT", "SGSG_TERM_DT"],
                    subgroups, identity_insert=True)

        insert_many(conn, "dbo.CMC_CSPD_CAT_DESC",
                    ["CSPD_CAT", "CSPD_CAT_DESC", "CSPD_TYPE"], hier.plan_category_rows())

        plans, plan_descriptions, product_descriptions = hier.plan_rows(
            product_rows, rng, DATA_AS_OF, ref.PLAN_NAME_PREFIXES, ref.PLAN_NAME_SUFFIXES
        )
        insert_many(conn, "dbo.CMC_PDDS_PROD_DESC", ["PDPD_ID", "PDDS_DESC"],
                    product_descriptions)
        product_type_by_id = dict(product_rows)
        prefix_by_plan, set_rows = nets.network_sets(plans, product_type_by_id, rng)
        insert_many(conn, "dbo.CMC_CSPI_CS_PLAN",
                    ["CSPI_ID", "CSPD_CAT", "PDPD_ID", "CSPI_EFF_DT", "CSPI_TERM_DT",
                     "CSPI_SEL_IND", "NWST_PFX"],
                    [(*plan, prefix_by_plan[plan[0]]) for plan in plans])
        insert_many(conn, "dbo.CMC_NWST_NET_SET",
                    ["NWST_PFX", "NWST_EFF_DT", "NWST_SEQ_NO", "NWST_TERM_DT", "NWST_PR_NW_STS",
                     "NWNW_ID", "NWPR_PFX", "PDVC_TIER"], set_rows)
        roster = nets.Roster(providers, relation_rows, set_rows, prefix_by_plan)
        insert_many(conn, "dbo.CMC_PLDS_PLAN_DESC", ["CSPI_ID", "PLDS_DESC"],
                    plan_descriptions)
        log(f"  groups {len(groups)}, subgroups {len(subgroups)}, plans {len(plans)}")

        subgroups_by_group: dict[int, list[int]] = {}
        for sgsg_ck, grgr_ck, *_rest in subgroups:
            subgroups_by_group.setdefault(grgr_ck, []).append(sgsg_ck)
        plans_by_type: dict[str, list[str]] = {"M": [], "D": []}
        for cspi_id, _cat, pdpd_id, *_rest in plans:
            plans_by_type[product_type_by_id[pdpd_id]].append(cspi_id)
        product_by_plan = {cspi_id: pdpd_id for cspi_id, _c, pdpd_id, *_r in plans}

        subscribers = build_subscribers(n_subscribers, rng, subgroups_by_group, plans_by_type)
        insert_many(conn, "dbo.CMC_SBSB_SUBSC",
                    ["SBSB_CK", "SBSB_ID", "SBSB_LAST_NAME", "SBSB_FIRST_NAME",
                     "SBSB_EFF_DT", "SBSB_ORIG_EFF_DT", "SBSB_MCTR_STS",
                     "SBSB_EMPLOY_ID", "GRGR_CK", "SGSG_CK", "CSPI_ID"],
                    subscribers, identity_insert=True)
        log(f"  subscribers {len(subscribers):,}")

        triples = ref.zip_triples()
        members = build_members(subscribers, rng, triples)
        insert_many(
            conn,
            "dbo.CMC_MEME_MEMBER",
            ["MEME_CK", "SBSB_CK", "MEME_ID", "MEME_FIRST_NAME", "MEME_LAST_NAME",
             "MEME_MID_INIT", "MEME_BIRTH_DT", "MEME_SEX", "MEME_MARITAL_STATUS",
             "MEME_SSN", "MEME_MCTR_STS", "MEME_REL", "MEME_EFF_DT",
             "MEME_ORIG_EFF_DT", "MEME_TERM_DT",
             "MEME_ZIP", "MEME_CITY", "MEME_STATE", "MEME_SFX"],
            members,
            identity_insert=True,
        )
        log(f"  members {len(members):,} across {len(triples)} ZIP triples")

        plan_by_subscriber = {s[0]: s[10] for s in subscribers}
        group_by_subscriber = {s[0]: s[8] for s in subscribers}
        pcps = []
        for member in members:
            cspi_id = plan_by_subscriber[member[1]]
            if product_type_by_id[product_by_plan[cspi_id]] != "M" or rng.random() > 0.8:
                continue
            chosen = roster.pcp(cspi_id, member[12], rng)
            if chosen:
                pcps.append((member[0], "MP", member[12], member[14],
                             group_by_subscriber[member[1]], chosen))
        insert_many(conn, "dbo.CMC_MEPR_PRIM_PROV",
                    ["MEME_CK", "MEPR_PCP_TYPE", "MEPR_EFF_DT", "MEPR_TERM_DT", "GRGR_CK",
                     "PRPR_ID"], pcps)
        log(f"  networks {len(nets.NETWORKS)}, network spans {len(relation_rows):,}, "
            f"PCPs {len(pcps):,}")

        product_by_subscriber = {s[0]: product_by_plan[s[10]] for s in subscribers}
        claims_written, lines_written = build_claims(
            conn, members, providers, product_rows, rng, gen,
            product_by_subscriber, product_type_by_id,
            plan_by_subscriber, roster,
        )
        log(f"  claims {claims_written:,}, claim lines {lines_written:,}")

        # Only the two views. They read the FACETS tables and are what an
        # extract selects through, so they belong in this database. Everything
        # else ddl_sdm.sql describes is the analysis store's and was created
        # in SDM_DENORM above -- putting it here as well would leave a second
        # set of tables nothing reads.
        log("creating the extract views")
        from bootstrap import refresh_source_views

        conn.commit()
        refresh_source_views(settings)
        members_seen = conn.cursor().execute(
            "SELECT COUNT(*) FROM sdm.member_denorm"
        ).fetchone()[0]
        lines_seen = conn.cursor().execute(
            "SELECT COUNT(*) FROM sdm.claims_denorm"
        ).fetchone()[0]
        log(f"  sdm.member_denorm {members_seen:,} rows, sdm.claims_denorm {lines_seen:,} rows")
    finally:
        conn.close()

    elapsed = time.time() - started
    log(f"done in {elapsed:.1f}s")
    return 0


def build_claims(
    conn: pyodbc.Connection,
    members: list[tuple],
    providers: list[tuple],
    products: list[tuple],
    rng: random.Random,
    gen: np.random.Generator,
    product_by_subscriber: dict[int, str],
    product_type_by_id: dict[str, str],
    plan_by_subscriber: dict[int, str],
    roster: "nets.Roster",
) -> tuple[int, int]:
    """Generate claims and lines member by member, flushing in batches.

    Whether a member is dental now follows the plan their subscriber is
    enrolled on rather than a loose draw, so the claims agree with the
    enrollment the analyzer reads.
    """
    all_codes, dental_codes = build_service_code_pool(rng)

    n_members = len(members)
    member_products = [product_by_subscriber[m[1]] for m in members]
    dental_flags = np.array(
        [product_type_by_id[pid] == "D" for pid in member_products], dtype=bool
    )
    claim_counts = draw_claim_counts(n_members, dental_flags, gen)

    claim_columns = ["CLCL_ID", "MEME_CK", "PRPR_ID", "PDPD_ID", "CLCL_CUR_STS",
                     "CLCL_CL_TYPE", "CLCL_NTWK_IND", "CLCL_IDCD_ID",
                     "CLCL_SVC_DT", "CLCL_RECD_DT", "CLCL_PAID_DT", "CLCL_TOT_CHG",
                     "NWNW_ID"]
    line_columns = ["CLCL_ID", "CDML_SEQ_NO", "CDML_SVC_CD", "CDML_CHG_AMT", "CDML_UNITS"]

    claim_rows: list[tuple] = []
    line_rows: list[tuple] = []
    claims_written = 0
    lines_written = 0
    claim_index = 0

    def flush() -> None:
        nonlocal claim_rows, line_rows, claims_written, lines_written
        insert_many(conn, "dbo.CMC_CLCL_CLAIM", claim_columns, claim_rows)
        insert_many(conn, "dbo.CMC_CDML_CL_LINE", line_columns, line_rows)
        claims_written += len(claim_rows)
        lines_written += len(line_rows)
        claim_rows = []
        line_rows = []

    for i, member in enumerate(members):
        n_claims = int(claim_counts[i])
        if n_claims == 0:
            continue
        meme_ck = member[0]
        eff: date = member[12]
        term: date | None = member[14]
        is_dental = bool(dental_flags[i])
        product_id = member_products[i]
        code_pool = dental_codes if is_dental else all_codes

        window_end = min(term, DATA_AS_OF) if term else DATA_AS_OF
        window_days = max(0, (window_end - eff).days)

        for _ in range(n_claims):
            claim_index += 1
            clcl_id = f"{(claim_index * CLCL_STRIDE + CLCL_OFFSET) % 10**12:012d}"
            svc = eff + timedelta(days=rng.randint(0, window_days))
            recd = svc + timedelta(days=rng.randint(*RECD_GAP_DAYS))
            paid = None if rng.random() < CLCL_PAID_NULL_SHARE else (
                recd + timedelta(days=rng.randint(*PAID_GAP_DAYS))
            )
            status = rng.choices(ref.CLAIM_STATUS_CODES, ref.CLAIM_STATUS_WEIGHTS)[0]

            n_lines = rng.choices(LINES_PER_CLAIM, LINES_PER_CLAIM_W)[0]
            total = Decimal("0.00")
            for seq in range(1, n_lines + 1):
                amount = Decimal(str(round(min(25_000.0, max(5.0, rng.lognormvariate(4.5, 0.9))), 2)))
                total += amount
                line_rows.append(
                    (clcl_id, seq, rng.choice(code_pool), amount,
                     rng.choices([1, 2, 3, 4, 5, 10], [0.55, 0.20, 0.10, 0.07, 0.05, 0.03])[0])
                )
            # A dental claim is filed as dental and carries a dental diagnosis;
            # everything else draws from the weighted book, so the top codes
            # the analyzer ranks are the ones that actually dominate.
            if is_dental:
                claim_type = "D"
                diagnosis = rng.choice(ref.DENTAL_DIAGNOSES)
            else:
                claim_type = rng.choices(["P", "I"], [0.82, 0.18])[0]
                diagnosis = rng.choices(
                    [code for code, _d, _c in ref.DIAGNOSES], ref.DIAGNOSIS_WEIGHTS
                )[0]
            # In network when the provider is in a network of the member's
            # plan on the service date; then the claim names that network.
            provider, network, nwnw_id = roster.claim_provider(
                plan_by_subscriber[member[1]], "D" if is_dental else "M", svc, rng)
            claim_rows.append(
                (clcl_id, meme_ck, provider, product_id,
                 status, claim_type, network, diagnosis,
                 svc, recd, paid, total, nwnw_id)
            )

        if len(claim_rows) >= BATCH:
            flush()

    flush()

    procedures = sorted(set(all_codes) | set(dental_codes))
    insert_many(
        conn, "dbo.CMC_IPCD_PROC_CODE", ["IPCD_ID", "IPCD_DESC"],
        [(code, f"{PROCEDURE_WORDS[ord(code[0]) % len(PROCEDURE_WORDS)]}, "
                f"level {int(code[1:]) % 5 + 1}") for code in procedures],
    )
    return claims_written, lines_written


if __name__ == "__main__":
    sys.exit(main())
