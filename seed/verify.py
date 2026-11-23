"""Confirm every pattern the seed script plants, one check per pattern.

Each check prints PASS or FAIL with the measured value. Later build steps are
tested against these same patterns, so a failure here invalidates those tests.

Usage:
    python seed/verify.py
    python seed/verify.py --json work/seed-verify.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdg.config import get_settings  # noqa: E402
from seed.npi import is_valid_npi  # noqa: E402


@dataclass
class Check:
    name: str
    step: str
    passed: bool
    detail: str
    measured: dict = field(default_factory=dict)


def scalar(cur: pyodbc.Cursor, sql: str) -> object:
    row = cur.execute(sql).fetchone()
    return None if row is None else row[0]


def near(value: float, target: float, tolerance: float) -> bool:
    return value is not None and abs(value - target) <= tolerance


def near_share(value: float, target: float, n: int, tolerance: float) -> bool:
    """Compare an observed proportion against its planted value.

    The seed draws each share at random, so a small fixture lands near the
    target rather than on it. The allowance is the stated tolerance or three
    standard errors, whichever is wider.
    """
    if value is None or not n:
        return False
    standard_error = (target * (1 - target) / n) ** 0.5
    return abs(value - target) <= max(tolerance, 3 * standard_error)


def run_checks(cur: pyodbc.Cursor, ref_cur: pyodbc.Cursor) -> list[Check]:
    checks: list[Check] = []

    def add(name, step, passed, detail, **measured):
        checks.append(Check(name, step, bool(passed), detail, measured))

    # --- volumes -------------------------------------------------------
    counts = {
        table: scalar(cur, f"SELECT COUNT(*) FROM dbo.{table}")
        for table in (
            "CMC_SBSB_SUBSC", "CMC_MEME_MEMBER", "CMC_PRPR_PROV",
            "CMC_PDPD_PRODUCT", "CMC_CLCL_CLAIM", "CMC_CDML_CL_LINE",
            "CMC_CLCL_AUDIT",
        )
    }
    add("row counts", "-", all(v > 0 for v in counts.values()),
        ", ".join(f"{k}={v:,}" for k, v in counts.items()), **counts)

    # --- Step 4: inferred foreign key ----------------------------------
    orphans = scalar(cur, """
        SELECT COUNT(*) FROM dbo.CMC_CLCL_CLAIM c
        WHERE NOT EXISTS (SELECT 1 FROM dbo.CMC_MEME_MEMBER m WHERE m.MEME_CK = c.MEME_CK)
    """)
    declared = scalar(cur, """
        SELECT COUNT(*) FROM sys.foreign_keys fk
        JOIN sys.tables t ON t.object_id = fk.parent_object_id
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.columns c ON c.object_id = fkc.parent_object_id AND c.column_id = fkc.parent_column_id
        WHERE t.name = 'CMC_CLCL_CLAIM' AND c.name = 'MEME_CK'
    """)
    add("CLCL.MEME_CK is an undeclared foreign key", "4",
        orphans == 0 and declared == 0,
        f"orphan claims={orphans}, declared FK constraints={declared}",
        orphans=orphans, declared_fks=declared)

    # --- Step 4: structural columns ------------------------------------
    identity_cols = [r[0] for r in cur.execute("""
        SELECT c.name FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id
        WHERE c.is_identity = 1 AND t.name LIKE 'CMC_%' ORDER BY c.name
    """).fetchall()]
    computed_cols = [r[0] for r in cur.execute("""
        SELECT c.name FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id
        WHERE c.is_computed = 1 AND t.name LIKE 'CMC_%' ORDER BY c.name
    """).fetchall()]
    default_cols = [r[0] for r in cur.execute("""
        SELECT c.name FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id
        WHERE c.default_object_id <> 0 AND t.name LIKE 'CMC_%' ORDER BY c.name
    """).fetchall()]
    add("identity, computed, and defaulted columns exist", "4",
        set(identity_cols) >= {"SBSB_CK", "MEME_CK"}
        and "CLCL_ROW_HASH" in computed_cols
        and "CLCL_CREATED_TS" in default_cols,
        f"identity={identity_cols}, computed={computed_cols}, default={default_cols}",
        identity=identity_cols, computed=computed_cols, defaults=default_cols)

    # --- Step 5: claims per member -------------------------------------
    ratio = cur.execute("""
        WITH per_member AS (
            SELECT m.MEME_CK, COUNT(c.CLCL_ID) AS n
            FROM dbo.CMC_MEME_MEMBER m
            LEFT JOIN dbo.CMC_CLCL_CLAIM c ON c.MEME_CK = m.MEME_CK
            GROUP BY m.MEME_CK
        )
        SELECT
            CAST(SUM(CASE WHEN n = 0 THEN 1.0 ELSE 0 END) / COUNT(*) AS float) AS zero_share,
            MAX(n) AS max_n,
            AVG(CAST(n AS float)) AS mean_n
        FROM per_member
    """).fetchone()
    median_nonzero = scalar(cur, """
        WITH per_member AS (
            SELECT m.MEME_CK, COUNT(c.CLCL_ID) AS n
            FROM dbo.CMC_MEME_MEMBER m
            LEFT JOIN dbo.CMC_CLCL_CLAIM c ON c.MEME_CK = m.MEME_CK
            GROUP BY m.MEME_CK
        )
        SELECT DISTINCT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY n) OVER ()
        FROM per_member WHERE n > 0
    """)
    p90_nonzero = scalar(cur, """
        WITH per_member AS (
            SELECT m.MEME_CK, COUNT(c.CLCL_ID) AS n
            FROM dbo.CMC_MEME_MEMBER m
            LEFT JOIN dbo.CMC_CLCL_CLAIM c ON c.MEME_CK = m.MEME_CK
            GROUP BY m.MEME_CK
        )
        SELECT DISTINCT PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY n) OVER ()
        FROM per_member WHERE n > 0
    """)
    zero_share, max_n, mean_n = float(ratio[0]), int(ratio[1]), float(ratio[2])
    add("claims per member: 30 percent zero, median 5, p90 14, max ~40", "5",
        near_share(zero_share, 0.30, counts["CMC_MEME_MEMBER"], 0.02)
        and near(float(median_nonzero), 5, 0.5)
        and near(float(p90_nonzero), 14, 2.0) and 25 <= max_n <= 45,
        f"zero_share={zero_share:.3f}, median(nonzero)={median_nonzero}, "
        f"p90(nonzero)={p90_nonzero}, max={max_n}, mean={mean_n:.2f}",
        zero_share=zero_share, median_nonzero=float(median_nonzero),
        p90_nonzero=float(p90_nonzero), max=max_n, mean=mean_n)

    # --- Step 5: dental members carry about half the claims -------------
    dental = cur.execute("""
        WITH member_kind AS (
            SELECT c.MEME_CK,
                   MAX(CASE WHEN p.PDPD_TYPE = 'D' THEN 1 ELSE 0 END) AS is_dental,
                   COUNT(*) AS n
            FROM dbo.CMC_CLCL_CLAIM c
            JOIN dbo.CMC_PDPD_PRODUCT p ON p.PDPD_ID = c.PDPD_ID
            GROUP BY c.MEME_CK
        )
        SELECT
            AVG(CASE WHEN is_dental = 1 THEN CAST(n AS float) END) AS dental_mean,
            AVG(CASE WHEN is_dental = 0 THEN CAST(n AS float) END) AS medical_mean
        FROM member_kind
    """).fetchone()
    dental_mean, medical_mean = float(dental[0]), float(dental[1])
    ratio_dm = dental_mean / medical_mean
    add("dental members carry about half the claims of medical", "5",
        0.40 <= ratio_dm <= 0.65,
        f"dental_mean={dental_mean:.2f}, medical_mean={medical_mean:.2f}, ratio={ratio_dm:.2f}",
        dental_mean=dental_mean, medical_mean=medical_mean, ratio=ratio_dm)

    # --- Step 7: claim total equals the line sum ------------------------
    mismatched = scalar(cur, """
        SELECT COUNT(*) FROM dbo.CMC_CLCL_CLAIM c
        WHERE c.CLCL_TOT_CHG <> (
            SELECT SUM(l.CDML_CHG_AMT) FROM dbo.CMC_CDML_CL_LINE l WHERE l.CLCL_ID = c.CLCL_ID
        )
    """)
    add("CLCL_TOT_CHG equals the sum of its claim lines", "7", mismatched == 0,
        f"mismatched claims={mismatched}", mismatched=mismatched)

    # --- Step 5: dental never carries an M service code -----------------
    dental_m = scalar(cur, """
        SELECT COUNT(*)
        FROM dbo.CMC_CDML_CL_LINE l
        JOIN dbo.CMC_CLCL_CLAIM c ON c.CLCL_ID = l.CLCL_ID
        JOIN dbo.CMC_PDPD_PRODUCT p ON p.PDPD_ID = c.PDPD_ID
        WHERE p.PDPD_TYPE = 'D' AND l.CDML_SVC_CD LIKE 'M%'
    """)
    add("dental products never carry an M service code", "5", dental_m == 0,
        f"violations={dental_m}", violations=dental_m)

    # --- Step 5: ZIP determines city and state --------------------------
    fd = cur.execute("""
        SELECT COUNT(DISTINCT MEME_ZIP),
               COUNT(DISTINCT CONCAT(MEME_ZIP, '|', MEME_CITY)),
               COUNT(DISTINCT CONCAT(MEME_ZIP, '|', MEME_STATE))
        FROM dbo.CMC_MEME_MEMBER
    """).fetchone()
    add("MEME_ZIP determines MEME_CITY and MEME_STATE", "5",
        fd[0] == fd[1] == fd[2],
        f"distinct zip={fd[0]}, zip+city={fd[1]}, zip+state={fd[2]}",
        distinct_zip=fd[0], distinct_zip_city=fd[1], distinct_zip_state=fd[2])

    # --- Step 5: date ordering -----------------------------------------
    svc_out = scalar(cur, """
        SELECT COUNT(*) FROM dbo.CMC_CLCL_CLAIM c
        JOIN dbo.CMC_MEME_MEMBER m ON m.MEME_CK = c.MEME_CK
        WHERE c.CLCL_SVC_DT < m.MEME_EFF_DT
           OR (m.MEME_TERM_DT IS NOT NULL AND c.CLCL_SVC_DT > m.MEME_TERM_DT)
    """)
    add("service date falls inside member coverage", "5", svc_out == 0,
        f"claims outside coverage={svc_out}", outside=svc_out)

    gaps = cur.execute("""
        SELECT
            SUM(CASE WHEN DATEDIFF(day, CLCL_SVC_DT, CLCL_RECD_DT) BETWEEN 0 AND 30 THEN 1 ELSE 0 END),
            COUNT(*),
            SUM(CASE WHEN CLCL_PAID_DT IS NOT NULL
                      AND DATEDIFF(day, CLCL_RECD_DT, CLCL_PAID_DT) BETWEEN 5 AND 60 THEN 1 ELSE 0 END),
            SUM(CASE WHEN CLCL_PAID_DT IS NOT NULL THEN 1 ELSE 0 END)
        FROM dbo.CMC_CLCL_CLAIM
    """).fetchone()
    add("received 0-30 days after service, paid 5-60 days after received", "5",
        gaps[0] == gaps[1] and gaps[2] == gaps[3],
        f"recd in range {gaps[0]}/{gaps[1]}, paid in range {gaps[2]}/{gaps[3]}",
        recd_in_range=gaps[0], claims=gaps[1], paid_in_range=gaps[2], paid_not_null=gaps[3])

    # --- Step 7: claim status weights -----------------------------------
    status_rows = cur.execute("""
        SELECT CLCL_CUR_STS, COUNT(*) FROM dbo.CMC_CLCL_CLAIM
        GROUP BY CLCL_CUR_STS ORDER BY COUNT(*) DESC
    """).fetchall()
    total_claims = sum(r[1] for r in status_rows)
    top_share = status_rows[0][1] / total_claims if total_claims else 0
    add("CLCL_CUR_STS has six values with '02' near 70 percent", "7",
        len(status_rows) == 6 and status_rows[0][0].strip() == "02"
        and near_share(top_share, 0.70, total_claims, 0.02),
        f"values={len(status_rows)}, top={status_rows[0][0].strip()} at {top_share:.3f}",
        distinct=len(status_rows), top_value=status_rows[0][0].strip(), top_share=top_share)

    # --- Step 5: null rates ---------------------------------------------
    nulls = cur.execute("""
        SELECT
            (SELECT CAST(SUM(CASE WHEN MEME_TERM_DT IS NULL THEN 1.0 ELSE 0 END) / COUNT(*) AS float)
               FROM dbo.CMC_MEME_MEMBER),
            (SELECT CAST(SUM(CASE WHEN CLCL_PAID_DT IS NULL THEN 1.0 ELSE 0 END) / COUNT(*) AS float)
               FROM dbo.CMC_CLCL_CLAIM)
    """).fetchone()
    n_members, n_claims = counts["CMC_MEME_MEMBER"], counts["CMC_CLCL_CLAIM"]
    add("null rates: MEME_TERM_DT 85 percent, CLCL_PAID_DT 20 percent", "5",
        near_share(nulls[0], 0.85, n_members, 0.01) and near_share(nulls[1], 0.20, n_claims, 0.01),
        f"MEME_TERM_DT={nulls[0]:.3f}, CLCL_PAID_DT={nulls[1]:.3f}",
        meme_term_null=nulls[0], clcl_paid_null=nulls[1])

    # --- Step 5/6: identifier formats -----------------------------------
    formats = {
        "SBSB_ID": ("CMC_SBSB_SUBSC", "[A-Z][A-Z][A-Z][0-9][0-9][0-9][0-9][0-9][0-9]"),
        "MEME_ID": ("CMC_MEME_MEMBER", "[A-Z][A-Z][A-Z][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"),
        "CLCL_ID": ("CMC_CLCL_CLAIM", "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"),
        "CDML_SVC_CD": ("CMC_CDML_CL_LINE", "[A-Z][0-9][0-9][0-9][0-9]"),
        "PRPR_ID": ("CMC_PRPR_PROV", "[A-Z][A-Z][A-Z][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"),
    }
    bad_formats = {}
    for column, (table, like) in formats.items():
        bad = scalar(cur, f"SELECT COUNT(*) FROM dbo.{table} WHERE {column} NOT LIKE '{like}'")
        if bad:
            bad_formats[column] = bad
    add("business key formats hold on every row", "5/6", not bad_formats,
        f"violations={bad_formats or 'none'}", violations=bad_formats)

    # --- Step 5/6: NPI check digit --------------------------------------
    npis = [r[0].strip() for r in cur.execute("SELECT PRPR_NPI FROM dbo.CMC_PRPR_PROV").fetchall()]
    bad_npi = [n for n in npis if not is_valid_npi(n)]
    add("every provider NPI passes the check-digit test", "5/6", not bad_npi,
        f"checked={len(npis)}, invalid={len(bad_npi)}",
        checked=len(npis), invalid=len(bad_npi))

    # --- writer: trigger fired ------------------------------------------
    # The trigger guarantees an audit row per claim inserted. It does not
    # remove them when a claim goes, so a generation run followed by a cleanup
    # leaves audit rows behind on purpose; those are counted, not failed.
    audit = scalar(cur, "SELECT COUNT(*) FROM dbo.CMC_CLCL_AUDIT")
    unaudited = scalar(cur, """
        SELECT COUNT(*) FROM dbo.CMC_CLCL_CLAIM c
        WHERE NOT EXISTS (SELECT 1 FROM dbo.CMC_CLCL_AUDIT a WHERE a.CLCL_ID = c.CLCL_ID)
    """)
    orphaned = scalar(cur, """
        SELECT COUNT(*) FROM dbo.CMC_CLCL_AUDIT a
        WHERE NOT EXISTS (SELECT 1 FROM dbo.CMC_CLCL_CLAIM c WHERE c.CLCL_ID = a.CLCL_ID)
    """)
    add("the claim trigger wrote an audit row for every claim", "9",
        audit > 0 and unaudited == 0,
        f"audit rows={audit:,}, claims={counts['CMC_CLCL_CLAIM']:,}, "
        f"claims with no audit row={unaudited}"
        + (f", audit rows left by cleaned-up runs={orphaned:,}" if orphaned else ""),
        audit_rows=audit, unaudited=unaudited, orphaned=orphaned)

    # --- MDM_REF ---------------------------------------------------------
    plan_total = scalar(ref_cur, "SELECT COUNT(*) FROM dbo.REF_PLAN_CODE")
    plan_inactive = scalar(ref_cur, "SELECT COUNT(*) FROM dbo.REF_PLAN_CODE WHERE ACTIVE_FLAG = 'N'")
    add("REF_PLAN_CODE holds 40 codes, 3 inactive", "8",
        plan_total == 40 and plan_inactive == 3,
        f"total={plan_total}, inactive={plan_inactive}",
        total=plan_total, inactive=plan_inactive)

    facets_products = {r[0].strip() for r in cur.execute("SELECT PDPD_ID FROM dbo.CMC_PDPD_PRODUCT")}
    ref_products = {r[0].strip() for r in ref_cur.execute("SELECT PRODUCT_CD FROM dbo.REF_PRODUCT_CODE")}
    add("REF_PRODUCT_CODE carries the same identifiers as CMC_PDPD_PRODUCT", "8",
        facets_products == ref_products,
        f"facets={len(facets_products)}, ref={len(ref_products)}, "
        f"symmetric difference={len(facets_products ^ ref_products)}",
        facets=len(facets_products), ref=len(ref_products))

    # ---- the hierarchy, and the populations that differ across it ---------
    # The analyzer clusters members inside a hierarchy selection. These checks
    # say the hierarchy is there to select on, and that the groups underneath
    # it hold genuinely different populations -- without which a discovered
    # pattern would be an arbitrary cut of one undifferentiated cloud.

    levels = {
        name: scalar(cur, f"SELECT COUNT(*) FROM dbo.{name}")
        for name in ("CMC_GRGR_GROUP", "CMC_SGSG_SUBGROUP", "CMC_CSPD_CAT_DESC",
                     "CMC_CSPI_CS_PLAN", "CMC_PLDS_PLAN_DESC", "CMC_PDDS_PROD_DESC")
    }
    add("the enrollment hierarchy is populated at every level", "analyzer",
        all(count and count > 0 for count in levels.values()),
        ", ".join(f"{name.split('_')[1]}={count}" for name, count in levels.items()),
        **{name.lower(): count for name, count in levels.items()})

    orphans = scalar(cur, """
        SELECT COUNT(*) FROM dbo.CMC_SBSB_SUBSC s
        WHERE NOT EXISTS (SELECT 1 FROM dbo.CMC_GRGR_GROUP g WHERE g.GRGR_CK = s.GRGR_CK)
           OR NOT EXISTS (SELECT 1 FROM dbo.CMC_CSPI_CS_PLAN p WHERE p.CSPI_ID = s.CSPI_ID)
    """)
    add("every subscriber sits in a group and on a plan", "analyzer",
        orphans == 0, f"subscribers with no group or no plan={orphans}", orphans=orphans)

    # The oldest and youngest workforces have to be far enough apart that
    # clustering separates them rather than splitting one population in half.
    ages = cur.execute("""
        SELECT g.GRGR_NAME,
               AVG(CAST(DATEDIFF(day, m.MEME_BIRTH_DT, '2026-06-30') / 365.25 AS float))
        FROM dbo.CMC_MEME_MEMBER m
        JOIN dbo.CMC_SBSB_SUBSC s ON s.SBSB_CK = m.SBSB_CK
        JOIN dbo.CMC_GRGR_GROUP g ON g.GRGR_CK = s.GRGR_CK
        WHERE m.MEME_REL <> 'D'
        GROUP BY g.GRGR_NAME
    """).fetchall()
    by_age = sorted(ages, key=lambda row: row[1])
    spread = (by_age[-1][1] - by_age[0][1]) if by_age else 0.0
    add("groups hold populations of visibly different ages", "analyzer",
        spread >= 12,
        f"youngest {by_age[0][0]} at {by_age[0][1]:.1f}, oldest {by_age[-1][0]} at "
        f"{by_age[-1][1]:.1f}, spread {spread:.1f} years" if by_age else "no groups",
        spread_years=round(spread, 2))

    sexes = cur.execute("""
        SELECT g.GRGR_NAME,
               SUM(CASE WHEN m.MEME_SEX = 'F' THEN 1.0 ELSE 0 END) / COUNT(*)
        FROM dbo.CMC_MEME_MEMBER m
        JOIN dbo.CMC_SBSB_SUBSC s ON s.SBSB_CK = m.SBSB_CK
        JOIN dbo.CMC_GRGR_GROUP g ON g.GRGR_CK = s.GRGR_CK
        WHERE m.MEME_REL <> 'D'
        GROUP BY g.GRGR_NAME
    """).fetchall()
    by_sex = sorted(sexes, key=lambda row: row[1])
    sex_spread = (by_sex[-1][1] - by_sex[0][1]) if by_sex else 0.0
    add("groups differ in their sex balance", "analyzer",
        sex_spread >= 0.30,
        f"{by_sex[0][0]} at {by_sex[0][1]:.0%} female, {by_sex[-1][0]} at "
        f"{by_sex[-1][1]:.0%}, spread {sex_spread:.0%}" if by_sex else "no groups",
        sex_spread=round(float(sex_spread), 3))

    before_birth = scalar(cur, """
        SELECT COUNT(*) FROM dbo.CMC_MEME_MEMBER WHERE MEME_EFF_DT < MEME_BIRTH_DT
    """)
    negative_age = scalar(cur, """
        SELECT COUNT(*)
        FROM dbo.CMC_CLCL_CLAIM c
        JOIN dbo.CMC_MEME_MEMBER m ON m.MEME_CK = c.MEME_CK
        WHERE c.CLCL_SVC_DT < m.MEME_BIRTH_DT
    """)
    add("nobody is covered, or treated, before they were born", "analyzer",
        before_birth == 0 and negative_age == 0,
        f"coverage before birth={before_birth}, claims before birth={negative_age}",
        coverage_before_birth=before_birth, claims_before_birth=negative_age)

    mismatched = scalar(cur, """
        SELECT COUNT(*)
        FROM dbo.CMC_CLCL_CLAIM c
        JOIN dbo.CMC_MEME_MEMBER m ON m.MEME_CK = c.MEME_CK
        JOIN dbo.CMC_SBSB_SUBSC s ON s.SBSB_CK = m.SBSB_CK
        JOIN dbo.CMC_CSPI_CS_PLAN p ON p.CSPI_ID = s.CSPI_ID
        WHERE c.PDPD_ID <> p.PDPD_ID
    """)
    add("a claim carries the product its member is enrolled on", "analyzer",
        mismatched == 0, f"claims against another product={mismatched}",
        mismatched=mismatched)

    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the planted seed patterns.")
    parser.add_argument("--json", type=Path, help="also write the results to this path")
    args = parser.parse_args(argv)

    settings = get_settings()
    conn = pyodbc.connect(settings.mssql.connection_string(settings.source_db))
    ref_conn = pyodbc.connect(settings.mssql.connection_string(settings.ref_db))
    try:
        checks = run_checks(conn.cursor(), ref_conn.cursor())
    finally:
        conn.close()
        ref_conn.close()

    width = max(len(c.name) for c in checks)
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"[{status}] step {check.step:<3} {check.name:<{width}}  {check.detail}")

    failed = [c for c in checks if not c.passed]
    print()
    print(f"{len(checks) - len(failed)}/{len(checks)} patterns verified")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([c.__dict__ for c in checks], indent=2, default=str), encoding="utf-8"
        )
        print(f"wrote {args.json}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
