"""A FACETS shaped by the customer's data dictionary, and their SDM platform store.

SDMT_FACETS_DD holds the FACETS tables sdg/packs/health/extract/FACETS.sql
reads, with the columns and keys the data dictionary gives them
(tests/fixtures/facets_dictionary_tables.json) and a small consistent book:
eligibility spans past, present and future, medical and dental, professional
and hospital claims, providers in networks, PCPs, authorizations.
SDMT_STORE_DD is empty (a new deployment) or the customer's DDL with
scripts 01 and 03 run on it. Only
databases named SDMT_*_DD are touched.
"""
import datetime
import json
import re
from pathlib import Path

import pyodbc

from sdg.config import get_settings

REPO = Path(__file__).resolve().parent.parent
FDB, SDB = "SDMT_FACETS_DD", "SDMT_STORE_DD"
DICTIONARY = REPO / "tests" / "fixtures" / "facets_dictionary_tables.json"
cs = get_settings().mssql.connection_string


def connect(db, autocommit=True):
    return pyodbc.connect(cs(db), autocommit=autocommit)


def recreate(db):
    assert db.startswith("SDMT_") and db.endswith("_DD")
    with connect("master") as m:
        m.execute(f"IF DB_ID('{db}') IS NOT NULL BEGIN ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{db}]; END")
        m.execute(f"CREATE DATABASE [{db}]")


def drop(db):
    assert db.startswith("SDMT_") and db.endswith("_DD")
    with connect("master") as m:
        m.execute(f"IF DB_ID('{db}') IS NOT NULL BEGIN ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{db}]; END")


def sqltype(column):
    t = column["type"]
    a = column["attributes"]
    if t in ("char", "varchar", "varbinary"):
        return f"{t}({int(a)})"
    if t == "numeric":
        return f"numeric({int(a)},0)"
    if t == "timestamp":
        return "binary(8)"
    return t


def build_facets():
    sql = (REPO / "sdg/packs/health/extract/FACETS.sql").read_text(encoding="utf-8")
    tables = sorted(set(re.findall(r"dbo\.(CMC_\w+)", sql)))
    dictionary = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    missing = [t for t in tables if t not in dictionary]
    if missing:
        raise AssertionError(f"FACETS.sql reads tables the data dictionary does not have: {missing}")
    recreate(FDB)
    with connect(FDB) as f:
        for t in tables:
            cols = dictionary[t]
            pk = [c["column"] for c in cols if c["primary_key"]]
            defs = [f"[{c['column']}] {sqltype(c)} {'NOT NULL' if c['column'] in pk else 'NULL'}"
                    for c in cols]
            if pk:
                defs.append(f"PRIMARY KEY ({', '.join('[' + p + ']' for p in pk)})")
            f.execute(f"CREATE TABLE dbo.{t} (" + ", ".join(defs) + ")")
    return tables


def ins(cur, table, **vals):
    cols = list(vals)
    cur.execute(
        f"INSERT INTO dbo.{table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [vals[k] for k in cols],
    )


def data():
    with connect(FDB, autocommit=False) as f:
        cur = f.cursor()
        D = lambda s: s  # dates as ISO strings  # noqa: E731
        # Groups, subgroups, classes
        ins(cur, "CMC_GRGR_GROUP", GRGR_CK=1, GRGR_ID="G0000001", GRGR_NAME="Acme Corp", GRGR_STATE="TX",
            GRGR_COUNTY="Travis", GRGR_STS="AC", GRGR_ORIG_EFF_DT=D("2015-01-01"), GRGR_TERM_DT=D("9999-12-31"),
            GRGR_MCTR_TYPE="LG", PAGR_CK=100, CSCS_ID="C001")
        ins(cur, "CMC_GRGR_GROUP", GRGR_CK=2, GRGR_ID="G0000002", GRGR_NAME="Beta LLC", GRGR_STATE="CA",
            GRGR_COUNTY="Alameda", GRGR_STS="AC", GRGR_ORIG_EFF_DT=D("2016-01-01"), GRGR_MCTR_TYPE="SG",
            PAGR_CK=200, CSCS_ID="C002")
        for sg, g, sid, cls in [(11, 1, "S001", "C001"), (12, 1, "S002", "C001"), (21, 2, "S001", "C002")]:
            ins(cur, "CMC_SGSG_SUB_GROUP", SGSG_CK=sg, GRGR_CK=g, SGSG_ID=sid, SGSG_NAME=f"Subgroup {sg}",
                CSCS_ID=cls, SGSG_STATE="TX", SGSG_STS="AC", SGSG_ORIG_EFF_DT=D("2015-01-01"))
        ins(cur, "CMC_CSCS_CLASS", GRGR_CK=1, CSCS_ID="C001", TPCT_PFX="TP01", CSCS_DESC="Class 1")
        ins(cur, "CMC_CSCS_CLASS", GRGR_CK=2, CSCS_ID="C002", TPCT_PFX="TP02", CSCS_DESC="Class 2")
        ins(cur, "CMC_CSPD_DESC", CSPD_CAT="M", CSPD_CAT_DESC="Medical", CSPD_TYPE="M")
        ins(cur, "CMC_CSPD_DESC", CSPD_CAT="D", CSPD_CAT_DESC="Dental", CSPD_TYPE="D")
        # Products (MED00001 has two versions with different lines of business)
        ins(cur, "CMC_PDPD_PRODUCT", PDPD_ID="MED00001", PDPD_EFF_DT=D("2018-01-01"), PDPD_TERM_DT=D("2023-12-31"),
            LOBD_ID="L001", PDPD_RISK_IND="N", PDPD_MCTR_CCAT="HMO1")
        ins(cur, "CMC_PDPD_PRODUCT", PDPD_ID="MED00001", PDPD_EFF_DT=D("2024-01-01"), PDPD_TERM_DT=None,
            LOBD_ID="L002", PDPD_RISK_IND="Y", PDPD_MCTR_CCAT="HMO2")
        ins(cur, "CMC_PDPD_PRODUCT", PDPD_ID="DEN00001", PDPD_EFF_DT=D("2018-01-01"), LOBD_ID="L001",
            PDPD_RISK_IND="N", PDPD_MCTR_CCAT="DEN")
        ins(cur, "CMC_PDPD_PRODUCT", PDPD_ID="MED00002", PDPD_EFF_DT=D("2018-01-01"), LOBD_ID="L001",
            PDPD_RISK_IND="N", PDPD_MCTR_CCAT="PPO")
        for p, desc, typ in [("MED00001", "HMO Gold", "H"), ("DEN00001", "Dental Basic", "D"), ("MED00002", "PPO Silver", "P")]:
            ins(cur, "CMC_PDDS_PROD_DESC", PDPD_ID=p, PDDS_DESC=desc, PDDS_PROD_TYPE=typ, PDDS_MCTR_BCAT="BC01",
                PDDS_UM_IND="Y", PDDS_OON_IND="N", PDDS_APP_TYPE="A", PDDS_PREM_IND="Y")
        ins(cur, "CMC_LOBD_LINE_BUS", LOBD_ID="L001", PYPY_ID="PY000001", LOBD_NAME="Commercial")
        ins(cur, "CMC_LOBD_LINE_BUS", LOBD_ID="L002", PYPY_ID="PY000002", LOBD_NAME="Commercial 2")
        ins(cur, "CMC_PYPY_PAYOR", PYPY_ID="PY000001", PYPY_EFF_DT=D("2018-01-01"), PYPY_TERM_DT=D("2022-12-31"),
            PYPY_PAYOR_NAME="Payer A (old)", PYPY_TAX_ID="900000001", PYPY_CITY="Austin", PYPY_STATE="TX")
        ins(cur, "CMC_PYPY_PAYOR", PYPY_ID="PY000001", PYPY_EFF_DT=D("2023-01-01"), PYPY_TERM_DT=None,
            PYPY_PAYOR_NAME="Payer A", PYPY_TAX_ID="900000001", PYPY_NATL_PLAN_ID="NPLAN1", PYPY_CITY="Austin",
            PYPY_STATE="TX")
        ins(cur, "CMC_PYPY_PAYOR", PYPY_ID="PY000002", PYPY_EFF_DT=D("2018-01-01"), PYPY_PAYOR_NAME="Payer B",
            PYPY_CITY="Dallas", PYPY_STATE="TX")
        # Class/plan rows. PLANX999 is referenced by nobody (must be left out).
        for g, cls, cat, cspi, eff, term, pdpd, nwst, sel in [
            (1, "C001", "M", "PLANM001", "2018-01-01", "2023-12-31", "MED00001", "NS01", "Y"),
            (1, "C001", "M", "PLANM001", "2024-01-01", None, "MED00001", "NS01", "N"),
            (1, "C001", "D", "PLAND001", "2018-01-01", None, "DEN00001", "NS02", "Y"),
            (2, "C002", "M", "PLANM002", "2018-01-01", None, "MED00002", "NS01", "Y"),
            (2, "C002", "M", "PLANX999", "2018-01-01", None, "MED00002", "NS09", "Y"),
        ]:
            ins(cur, "CMC_CSPI_CS_PLAN", GRGR_CK=g, CSCS_ID=cls, CSPD_CAT=cat, CSPI_ID=cspi, CSPI_EFF_DT=eff,
                CSPI_TERM_DT=term, PDPD_ID=pdpd, NWST_PFX=nwst, CSPI_PDPD_BEG_MMDD=101, CSPI_SEL_IND=sel)
        for cspi in ["PLANM001", "PLAND001", "PLANM002", "PLANX999"]:
            ins(cur, "CMC_PLDS_PLAN_DESC", CSPI_ID=cspi, PLDS_DESC=f"Plan {cspi}")
        # Network sets and networks
        for pfx, seq, net, npfx, tier, sts in [("NS01", 1, "NET1", "NP01", 1, "I"), ("NS01", 2, "NET2", "NP02", 2, "P"),
                                               ("NS02", 1, "NET1", "NP01", 1, "I"), ("NS09", 1, "NET9", "NP09", 1, "I")]:
            ins(cur, "CMC_NWST_NET_SET", NWST_PFX=pfx, NWST_EFF_DT="2018-01-01", NWST_SEQ_NO=seq, NWNW_ID=net,
                NWPR_PFX=npfx, PDVC_TIER=tier, NWST_PR_NW_STS=sts)
        for net in ["NET1", "NET2", "NET9"]:
            ins(cur, "CMC_NWNW_NETWORK", NWNW_ID=net, NWNW_NAME=f"Network {net}", NWNW_MCTR_TYPE="HMO")
        # Subscribers and addresses
        for sb, g, sid in [(1001, 1, "SUB000001"), (1002, 1, "SUB000002"), (2001, 2, "SUB000003")]:
            ins(cur, "CMC_SBSB_SUBSC", SBSB_CK=sb, GRGR_CK=g, SBSB_ID=sid, SBSB_LAST_NAME=f"Last{sb}",
                SBSB_FIRST_NAME=f"First{sb}", SBSB_MID_INIT="Q", SBAD_TYPE_HOME="H", SBSB_ORIG_EFF_DT="2015-01-01",
                SBSB_MCTR_STS="AC", SBSB_EMPLOY_ID=f"E{sb}")
        for sb, typ, city, email in [(1001, "H", "Austin", "home1001@example.com"), (1001, "M", "Round Rock", None),
                                     (1002, "H", "Austin", None), (2001, "H", "Oakland", "x@example.com")]:
            ins(cur, "CMC_SBAD_ADDR", SBSB_CK=sb, SBAD_TYPE=typ, GRGR_CK=1, SBAD_ADDR1="1 Main St", SBAD_CITY=city,
                SBAD_STATE="TX", SBAD_ZIP="78701", SBAD_EMAIL=email, SBAD_PHONE="5125550100")
        for sb, sgsg, eff, term, trsn in [(1001, 11, "2015-01-01", "2023-12-31", None), (1001, 11, "2024-01-01", None, "RNW"),
                                          (1002, 11, "2015-01-01", None, None), (2001, 21, "2015-01-01", None, None)]:
            ins(cur, "CMC_SBSG_RELATION", SBSB_CK=sb, SBSG_EFF_DT=eff, SGSG_CK=sgsg, SBSG_TERM_DT=term,
                SBSG_MCTR_TRSN=trsn, GRGR_CK=1 if sb < 2000 else 2)
        # Members
        for me, sb, g, rel, sfx, home, mail, ssn, sex in [
            (1, 1001, 1, "M", 0, "H", "M", "123456789", "F"),
            (2, 1001, 1, "S", 1, " ", " ", "         ", "M"),
            (3, 1002, 1, "M", 0, "H", None, "234567890", "M"),
            (4, 2001, 2, "M", 0, "H", None, "345678901", "F"),
            (5, 2001, 2, "D", 2, "H", None, None, "F"),
            (6, 1002, 1, "D", 2, "H", None, "45678901A", "F"),
        ]:
            ins(cur, "CMC_MEME_MEMBER", MEME_CK=me, SBSB_CK=sb, GRGR_CK=g, MEME_REL=rel, MEME_SFX=sfx,
                MEME_LAST_NAME=f"Mlast{me}", MEME_FIRST_NAME=f"Mfirst{me}", MEME_MID_INIT="A", MEME_SEX=sex,
                MEME_BIRTH_DT="1980-05-05", MEME_SSN=ssn, MEME_MCTR_STS="AC", MEME_ORIG_EFF_DT="2015-01-01",
                MEME_MARITAL_STATUS="M", MEME_MEDCD_NO="MCD1" if me == 4 else None, MEME_HICN=None,
                SBAD_TYPE_HOME=home, SBAD_TYPE_MAIL=mail)
        # Eligibility. Today is the run date (2026-10).
        for me, cat, eff, term, ind, g, sg, cls, cspi, pdpd in [
            (1, "M", "2018-01-01", "2023-12-31", "Y", 1, 11, "C001", "PLANM001", "MED00001"),
            (1, "M", "2024-01-01", None, "Y", 1, 11, "C001", "PLANM001", "MED00001"),
            (1, "D", "2020-01-01", None, "Y", 1, 11, "C001", "PLAND001", "DEN00001"),
            (2, "M", "2024-01-01", "9999-12-31", "Y", 1, 11, "C001", "PLANM001", "MED00001"),
            (3, "M", "2018-01-01", "2025-12-31", "Y", 1, 11, "C001", "PLANM001", "MED00001"),
            (3, "M", "2026-01-01", None, "N", 1, 11, "C001", "PLANM001", "MED00001"),
            (4, "M", "2018-01-01", None, "Y", 2, 21, "C002", "PLANM002", "MED00002"),
            (4, "M", "2017-01-01", "2016-12-31", "Y", 2, 21, "C002", "PLANM002", "MED00002"),  # voided
            (5, "M", "2020-01-01", None, "N", 2, 21, "C002", "PLANM002", "MED00002"),
            (6, "M", "2019-01-01", "2020-12-31", "Y", 1, 11, "C001", "PLANM001", "MED00001"),
        ]:
            ins(cur, "CMC_MEPE_PRCS_ELIG", MEME_CK=me, CSPD_CAT=cat, MEPE_EFF_DT=eff, MEPE_TERM_DT=term,
                MEPE_ELIG_IND=ind, GRGR_CK=g, SGSG_CK=sg, CSCS_ID=cls, CSPI_ID=cspi, PDPD_ID=pdpd, MEPE_FI="F",
                MEPE_PLAN_ENTRY_DT=eff, MEPE_CREATE_DTM="2015-01-01", MEPE_SOURCE="O", MEPE_SYS_INEL_CD="    ")
        ins(cur, "CMC_MAME_MASTER", MAME_CK=501, MAME_MCTR_RACE="WHT", MAME_MCTR_ETHN="NHL")
        ins(cur, "CMC_MAME_MASTER", MAME_CK=504, MAME_MCTR_RACE="ASN", MAME_MCTR_ETHN="NHL")
        ins(cur, "CMC_MALK_LINK", MAME_CK=501, MEME_CK=1, MALK_CREATE_DTM="2020-01-01")
        ins(cur, "CMC_MALK_LINK", MAME_CK=504, MEME_CK=4, MALK_CREATE_DTM="2020-01-01")
        ins(cur, "CMC_MEPR_PRIM_PROV", MEME_CK=1, MEPR_PCP_TYPE="MP", MEPR_EFF_DT="2024-01-01", GRGR_CK=1,
            PRPR_ID="PR0000000002")
        ins(cur, "CMC_MEPR_PRIM_PROV", MEME_CK=4, MEPR_PCP_TYPE="MP", MEPR_EFF_DT="2018-01-01",
            MEPR_TERM_DT="2023-12-31", GRGR_CK=2, PRPR_ID="PR0000000003")
        ins(cur, "CMC_MEPR_PRIM_PROV", MEME_CK=3, MEPR_PCP_TYPE="MP", MEPR_EFF_DT="2018-01-01",
            MEPR_TERM_DT="2025-12-31", GRGR_CK=1, PRPR_ID="PR0000000002")
        ins(cur, "CMC_MCRE_RELAT_ENT", MCRE_ID="MCRE00001", MCRE_NAME="Other Carrier", MCRE_CITY="Reno",
            MCRE_STATE="NV")
        ins(cur, "CMC_MECB_COB", MEME_CK=1, MECB_INSUR_TYPE="C", MECB_INSUR_ORDER="S", MECB_MCTR_STYP="SP",
            MECB_EFF_DT="2024-01-01", MECB_POLICY_ID="POL123", MCRE_ID="MCRE00001")
        # Providers
        provs = [
            ("PR0000000001", "P", "Dr One", "CP01", "AD01", "CR01", "111111111", "1111111111", "FP"),
            ("PR0000000002", "P", "Dr Two", "CP02", "AD02", " ", "222222222", "2222222222", "IM"),
            ("PR0000000003", "P", "Dr Three", "CP03", "AD02", " ", "333333333", "3333333333", "IM"),
            ("GR0000000001", "G", "Group One", " ", "AD03", " ", "444444444", "4444444444", "GP"),
            ("FA0000000001", "F", "Hospital One", " ", "AD04", " ", "555555555", "5555555555", "HOS"),
            ("FA0000000002", "F", "Hospital Two", " ", "AD04", " ", "555555555", "5555555556", "HOS"),
            ("PR0000000009", "P", "Dr Nine", "CP09", "AD02", " ", "999999999", "9999999999", "FP"),
            ("PR0000000008", "P", "Dr Eight", "CP09", "AD02", " ", "999999999", "8888888888", "FP"),
        ]
        for pid, ent, name, prcp, prad, prcr, mctn, npi, spec in provs:
            ins(cur, "CMC_PRPR_PROV", PRPR_ID=pid, PRPR_ENTITY=ent, PRPR_NAME=name, PRCP_ID=prcp, PRAD_ID=prad,
                PRCR_ID=prcr, MCTN_ID=mctn, PRPR_NPI=npi, PRCF_MCTR_SPEC=spec, PRAD_TYPE_PRIM="PRA",
                PRAD_TYPE_CHECK="REM" if pid == "PR0000000001" else " ", PRPR_STS="AC",
                PRPR_TAXONOMY_CD="207Q00000X",
                PRPR_TERM_DT="2022-06-30" if pid == "PR0000000008" else None)
        for prcp in ["CP01", "CP02", "CP03", "CP09"]:
            ins(cur, "CMC_PRCP_COMM_PRAC", PRCP_ID=prcp, PRCP_LAST_NAME=f"Last{prcp}", PRCP_FIRST_NAME="First",
                PRCP_MID_INIT="M", PRCP_TITLE="MD")
        ins(cur, "CMC_PRFA_FACILITY", PRPR_ID="FA0000000001", PRFA_NAME="Hospital One Inc")
        ins(cur, "CMC_PRFA_FACILITY", PRPR_ID="FA0000000002", PRFA_NAME="Hospital Two Inc")
        for mctn, typ in [("111111111", "E"), ("111111111", "S"), ("222222222", "S"), ("333333333", "S"),
                          ("444444444", "E"), ("555555555", "E"), ("999999999", "S")]:
            ins(cur, "CMC_MCTN_TAX_NAME", MCTN_ID=mctn, MCTN_TYPE=typ, MCTN_NAME=f"Tax {mctn} {typ}")
        ins(cur, "CMC_MCTI_TAX_INFO", MCTI_ENTY_ID="PR0000000001", MCTI_TYPE="P", MCTI_EFF_DT="2015-01-01",
            MCTN_ID="111111111", MCTN_TYPE="S")
        for prad, typ, eff, term in [("AD01", "PRA", "2015-01-01", "2022-12-31"), ("AD01", "PRA", "2023-01-01", None),
                                     ("AD01", "REM", "2015-01-01", None), ("AD02", "PRA", "2015-01-01", None),
                                     ("AD03", "PRA", "2015-01-01", None), ("AD04", "PRA", "2015-01-01", None)]:
            ins(cur, "CMC_PRAD_ADDRESS", PRAD_ID=prad, PRAD_TYPE=typ, PRAD_EFF_DT=eff, PRAD_TERM_DT=term,
                PRAD_ADDR1="100 Clinic Way", PRAD_CITY="Austin", PRAD_STATE="TX", PRAD_ZIP="78701",
                PRAD_PRACTICE_IND="Y")
        ins(cur, "CMC_PRER_RELATION", PRPR_ID="PR0000000001", PRER_PRPR_ENTITY="G", PRER_EFF_DT="2015-01-01",
            PRER_PRPR_ID="GR0000000001")
        ins(cur, "CMC_PRDS_DATE", PRPR_ID="PR0000000001", PRDS_EFF_DT="2015-01-01", PRDS_PAY_CL_IND="G",
            PRDS_PAY_CAP_IND="N")
        ins(cur, "CMC_PRDS_DATE", PRPR_ID="PR0000000002", PRDS_EFF_DT="2015-01-01", PRDS_PAY_CL_IND="P",
            PRDS_PAY_CAP_IND="N")
        ins(cur, "CMC_PRAF_FAC_AFFIL", PRCR_ID="CR01", PRPR_ID="FA0000000002", PRAF_FROM_DT="2015-01-01",
            PRAF_PRIM_IND="Y", PRAF_STS="AC")
        for pid, net, npfx, pcp in [("PR0000000001", "NET1", "NP01", "N"), ("PR0000000002", "NET1", "NP01", "Y"),
                                    ("PR0000000003", "NET1", "NP01", "N"), ("PR0000000001", "NET2", "NP02", "N"),
                                    ("PR0000000009", "NET9", "NP09", "N"), ("PR0000000008", "NET1", "NP01", "N")]:
            ins(cur, "CMC_NWPR_RELATION", PRPR_ID=pid, NWNW_ID=net, NWPR_PFX=npfx, NWPR_PCP_IND=pcp,
                NWPR_EFF_DT="2018-01-01", AGAG_ID="AG01", NWPR_ACC_PAT_IND="Y", NWPR_MIN_AGE=0, NWPR_MAX_AGE=99,
                NWPR_SEX="B")
        ins(cur, "CMC_AGAG_AGREEMENT", AGAG_ID="AG01", AGAG_EFF_DT="2015-01-01", AGAG_TERM_DT="2020-12-31",
            AGAG_MCTR_TYPE="FFS", AGAG_DESC="Old agreement", AGSE_PFX="A001")
        ins(cur, "CMC_AGAG_AGREEMENT", AGAG_ID="AG01", AGAG_EFF_DT="2021-01-01", AGAG_MCTR_TYPE="FFS",
            AGAG_DESC="Current agreement", AGSE_PFX="A001")
        # Codes
        for dx, typ, eff, desc in [("E119", "0", "2015-10-01", "Type 2 diabetes"), ("E119", "9", "2010-01-01", "Old"),
                                   ("I10", "0", "2015-10-01", "Hypertension"), ("R079", "0", "2015-10-01", "Chest pain"),
                                   ("J189", "0", "2015-10-01", "Pneumonia")]:
            ins(cur, "CMC_IDCD_DIAG_CD", IDCD_ID=dx, IDCD_TYPE=typ, IDCD_EFF_DT=eff, IDCD_DESC=desc)
        for px, desc in [("9921325", "Office visit est + mod 25"), ("99214", "Office visit est"),
                         ("0BH17EZ", "ICD-10-PCS procedure")]:
            ins(cur, "CMC_IPCD_PROC_CD", IPCD_ID=px, IPCD_DESC=desc, IPCD_TYPE="C")
        for ps, desc, setg in [("11", "Office", "O"), ("21", "Inpatient hospital", "I")]:
            ins(cur, "CMC_PSCD_POS_DESC", PSCD_ID=ps, PSCD_DESC=desc, PSCD_POS_IND="Y", TPCT_MCTR_TCAT="P",
                TPCT_MCTR_SETG=setg)
        # Claims. Service dates are relative to the run date: the extract keeps
        # the last 24 months. CL3 has no lines; CL4 is of a member no longer
        # eligible; CL5 is of an eligible member but 30 months old: all three
        # are out of scope.
        today = datetime.date.today()
        recent = (today - datetime.timedelta(days=100)).isoformat()
        recent2 = (today - datetime.timedelta(days=60)).isoformat()
        old = (today - datetime.timedelta(days=915)).isoformat()
        claims = [
            ("CL0000000001", 1, 1001, 1, "C001", "M", "PLANM001", "MED00001", "PR0000000001", "M", "F",
             "PR0000000002", "PR0000000002", "GR0000000001", recent, "NET1"),
            ("CL0000000002", 4, 2001, 2, "C002", "M", "PLANM002", "MED00002", "FA0000000001", "H", "F",
             None, None, None, recent2, "NET1"),
            ("CL0000000003", 5, 2001, 2, "C002", "M", "PLANX999", "MED00002", "PR0000000001", "M", "F",
             None, None, None, "2024-06-01", None),
            ("CL0000000004", 6, 1002, 1, "C001", "M", "PLANM001", "MED00001", "PR0000000002", "M", "F",
             None, None, None, "2020-06-01", "NET1"),
            ("CL0000000005", 1, 1001, 1, "C001", "M", "PLANM001", "MED00001", "PR0000000008", "M", "F",
             None, None, None, old, "NET1"),
        ]
        for (cid, me, sb, g, cls, cat, cspi, pdpd, prpr, typ, sex, ref, pcp, payee, svc, net) in claims:
            ins(cur, "CMC_CLCL_CLAIM", CLCL_ID=cid, MEME_CK=me, SBSB_CK=sb, GRGR_CK=g, CSCS_ID=cls, CSPD_CAT=cat,
                CSPI_ID=cspi, PDPD_ID=pdpd, PRPR_ID=prpr, CLCL_CL_TYPE=typ, CLCL_CL_SUB_TYPE="A" if typ == "M" else "H",
                CLCL_CUR_STS="02", CLST_MCTR_REAS="RS01", CLCL_NTWK_IND="I", CLCL_INPUT_DT=svc, CLCL_RECD_DT=svc,
                CLCL_PAID_DT=svc, CLCL_LOW_SVC_DT=svc, CLCL_HIGH_SVC_DT=svc, CLCL_TOT_CHG=250.00,
                CLCL_TOT_PAYABLE=180.00, CLCL_ME_AGE=43, MEME_SEX=sex, MEME_REL="M", MEPE_FI="F", SGSG_CK=11,
                PDDS_PROD_TYPE="H", CLCL_PRPR_ID_REF=ref, CLCL_PRPR_ID_PCP=pcp, CLCL_PCP_IND="N",
                CLCL_PAYEE_PR_ID=payee, NWNW_ID=net, NWPR_PFX="NP01", AGAG_ID="AG01", PRAD_TYPE="PRA",
                CLCL_EXT_AUTH_NO="EXT1" if cid.endswith("1") else None,
                CLCL_ID_ADJ_FROM="CL0000000099" if cid.endswith("2") else " ")
        lines = [
            ("CL0000000001", 1, "9921325", "E119", "11", None, "UM0000001", 1, None, None),
            ("CL0000000001", 2, "99214", "I10", "11", None, None, None, "UM0000002", 1),
            ("CL0000000002", 1, " ", "J189", "21", "0450", None, None, None, None),
            ("CL0000000002", 2, " ", "J189", "21", "0120", None, None, None, None),
            ("CL0000000004", 1, "99214", "R079", "11", None, None, None, None, None),
            ("CL0000000005", 1, "99214", "R079", "11", None, None, None, None, None),
        ]
        for cid, seq, ipcd, dx, pos, rc, auth, aseq, ref, rseq in lines:
            ins(cur, "CMC_CDML_CL_LINE", CLCL_ID=cid, CDML_SEQ_NO=seq, IPCD_ID=ipcd, IDCD_ID=dx, PSCD_ID=pos,
                RCRC_ID=rc, LOBD_ID="L001", PRPR_ID="PR0000000001", MEME_CK=1, CDML_UNITS=1, CDML_UNITS_ALLOW=1,
                CDML_CHG_AMT=125.00, CDML_ALLOW=100.00, CDML_PAID_AMT=80.00, CDML_DED_AMT=0, CDML_COPAY_AMT=20.00,
                CDML_COINS_AMT=0, CDML_DISALL_AMT=25.00, CDML_DISC_AMT=0, CDML_TOT_PA_LIAB=20.00,
                CDML_FROM_DT="2024-03-01", CDML_TO_DT="2024-03-01", CDML_CUR_STS="02", CDML_PRE_AUTH_IND="N",
                CDML_UMAUTH_ID=auth, CDML_AUTHSV_SEQ_NO=aseq, CDML_UMREF_ID=ref, CDML_REFSV_SEQ_NO=rseq,
                SESE_ID="OV01", SESE_RULE="R01", SEPY_PFX="SP01", PRCF_MCTR_SPEC="FP" if cid.endswith("1") else " ",
                CDML_CLMD_TYPE2="02" if cid.endswith("1") else None, CDML_EXT_LINE_NO=seq)
        for cid, typ, dx in [("CL0000000001", "01", "E119"), ("CL0000000001", "02", "I10"),
                             ("CL0000000002", "01", "J189"), ("CL0000000002", "AD", "R079"),
                             ("CL0000000003", "01", "E119")]:
            ins(cur, "CMC_CLMD_DIAG", CLCL_ID=cid, CLMD_TYPE=typ, IDCD_ID=dx, IDCD_ID_SUB=dx, MEME_CK=1,
                CLMD_LOCK_TOKEN=1, CLMD_POA_IND="Y")
        ins(cur, "CMC_CLHP_HOSP", CLCL_ID="CL0000000002", CLHP_FAC_TYPE="11", CLHP_BILL_CLASS="1",
            CLHP_FREQUENCY="1", CLHP_ADM_TYP="1", CLHP_ADM_DT="2024-05-01", CLHP_DC_DT="2024-05-03",
            CLHP_PRPR_ID_ADM="PR0000000001", CLHP_PRPR_ID_OPER="PR0000000003", CLHP_ICD_QUAL_IND="0")
        ins(cur, "CMC_CLMF_MULT_FUNC", CLCL_ID="CL0000000002", CLMF_PRPR_ID_FAC="FA0000000001")
        ins(cur, "CMC_CLSD_SUPP_DATA", CLCL_ID="CL0000000001", CLSD_PRPR_ID_REND_NVL="PR0000000001")
        ins(cur, "CMC_CLED_EDI_DATA", CLCL_ID="CL0000000001", CLED_HIPAA_VERSION="005010X222A1")
        ins(cur, "CMC_CLCB_CL_COB", CLCL_ID="CL0000000001", CLCB_COB_TYPE="C", CLCB_COB_AMT=10.00)
        ins(cur, "CMC_CDSD_SUPP_DATA", CLCL_ID="CL0000000001", CDML_SEQ_NO=1, CDSD_PRPR_ID_REND="PR0000000001",
            CDSD_NDC_CODE="00002771001")
        ins(cur, "CMC_CDCB_LI_COB", CLCL_ID="CL0000000001", CDML_SEQ_NO=1, CDCB_COB_TYPE="C", CDCB_COB_AMT=5.00)
        ins(cur, "CMC_CLHI_PROC", CLCL_ID="CL0000000002", CLHI_TYPE="P1", IPCD_ID="0BH17EZ", CLHI_IP_DT="2024-05-01")
        ins(cur, "CMC_CLHC_COND_CODE", CLCL_ID="CL0000000002", CLHC_SEQ_NO=1, CLHC_COND_CD="01", MEME_CK=4)
        ins(cur, "CMC_CLHO_OCC_CODE", CLCL_ID="CL0000000002", CLHO_SEQ_NO=1, CLHO_OCC_CODE="11",
            CLHO_OCC_FROM_DT="2024-05-01", MEME_CK=4)
        ins(cur, "CMC_CLVC_VAL_CODE", CLCL_ID="CL0000000002", CLVC_NUMBER="01", CLVC_LETTER="A", CLVC_CODE="80",
            CLVC_AMT=2.00, CLVC_VALUE=2, MEME_CK=4)
        ins(cur, "CMC_CLVC_VAL_CODE", CLCL_ID="CL0000000002", CLVC_NUMBER="01", CLVC_LETTER="B", CLVC_CODE="01",
            CLVC_AMT=500.00, MEME_CK=4)
        ins(cur, "CMC_UMSV_SERVICES", UMUM_REF_ID="UM0000001", UMSV_SEQ_NO=1, UMSV_FROM_DT="2024-02-01",
            UMSV_TO_DT="2024-04-30", UMSV_PRPR_ID_REQ="PR0000000002", UMSV_PRPR_ID_SVC="PR0000000001",
            UMSV_PSCD_ID_AUTH="11", IPCD_ID="99214", SESE_ID="OV01", UMSV_UNITS_AUTH=3, UMVT_STS="AP")
        ins(cur, "CMC_UMSV_SERVICES", UMUM_REF_ID="UM0000002", UMSV_SEQ_NO=1, UMSV_FROM_DT="2024-02-01",
            UMSV_TO_DT="2024-04-30", UMSV_PRPR_ID_REQ="PR0000000002", UMSV_PRPR_ID_SVC="PR0000000001",
            UMSV_PSCD_ID_AUTH="11", UMVT_STS="AP")
        # Benefits
        for pdpd, sepy, dede, ltlt in [("MED00001", "SP01", "DD01", "LT01"), ("DEN00001", "SP02", "DD02", None),
                                       ("MED00002", "SP01", "DD01", None)]:
            ins(cur, "CMC_PDVC_VAR_COMP", PDPD_ID=pdpd, PDVC_TIER=1, PDVC_TYPE="A", PDVC_SEQ_NO=1,
                PDVC_EFF_DT="2018-01-01", SEPY_PFX=sepy, DEDE_PFX=dede, LTLT_PFX=ltlt, PDVC_PR_IN="Y",
                PDVC_LOBD_PTR="1")
        # A second medical PDVC row priced under SP03, which no claim line used.
        ins(cur, "CMC_PDVC_VAR_COMP", PDPD_ID="MED00001", PDVC_TIER=2, PDVC_TYPE="A", PDVC_SEQ_NO=1,
            PDVC_EFF_DT="2018-01-01", SEPY_PFX="SP03", DEDE_PFX="DD01", PDVC_PR_IN="N", PDVC_LOBD_PTR="1")
        ins(cur, "CMC_SEPY_SE_PYMT", SEPY_PFX="SP03", SEPY_EFF_DT="2018-01-01", SESE_ID="OV01", SESE_RULE="R01")
        for sepy, sese, rule in [("SP01", "OV01", "R01"), ("SP01", "IP01", "R02"), ("SP02", "DN01", "R03")]:
            ins(cur, "CMC_SEPY_SE_PYMT", SEPY_PFX=sepy, SEPY_EFF_DT="2018-01-01", SESE_ID=sese, SESE_RULE=rule)
            ins(cur, "CMC_SESE_SERVICE", SESE_ID=sese, SESE_RULE=rule, SESE_RULE_TYPE="C", SESE_MIN_AGE=0,
                SESE_MAX_AGE=99)
            ins(cur, "CMC_SEDS_SE_DESC", SESE_ID=sese, SEDS_DESC=f"Service {sese}", SEDS_TYPE="M")
        for sese, rule, tier in [("OV01", "R01", 1), ("OV01", "R01", 2), ("IP01", "R02", 1)]:
            ins(cur, "CMC_SETR_TIER", SESE_ID=sese, SESE_RULE=rule, SETR_TIER_NO=tier, SETR_COPAY_AMT=20.00,
                ACAC_ACC_NO=1)
        ins(cur, "CMC_PDBC_PROD_COMP", PDPD_ID="MED00001", PDBC_TYPE="SEDF", PDBC_EFF_DT="2018-01-01",
            PDBC_PFX="SD01")
        ins(cur, "CMC_SEDF_SE_DEFN", PDBC_PFX="SD01", SEDF_EFF_DT="2018-01-01", SESE_ID="OV01",
            SEDF_UM_REQ_IND="N", SEDF_TERM_DT="2019-12-31")
        ins(cur, "CMC_SEDF_SE_DEFN", PDBC_PFX="SD01", SEDF_EFF_DT="2020-01-01", SESE_ID="OV01",
            SEDF_UM_REQ_IND="Y")
        for dede in ["DD01", "DD02"]:
            ins(cur, "CMC_DEDE_DEDUCT", DEDE_PFX=dede, ACAC_ACC_NO=1, DEDE_DESC=f"Deductible {dede}",
                DEDE_MEME_AMT=500.00, DEDE_FAM_AMT=1000.00)
        ins(cur, "CMC_LTLT_LIMIT", LTLT_PFX="LT01", ACAC_ACC_NO=2, LTLT_DESC="Visit limit", LTLT_AMT1=20.00)
        ins(cur, "CMC_LTSE_SE_IX", LTLT_PFX="LT01", ACAC_ACC_NO=2, SESE_ID="OV01", LTSE_WT_CTR=1)
        ins(cur, "CMC_TPCT_CPT_CONV", TPCT_PFX="TP01", TPCT_IPCD_LOW="99201", TPCT_IPCD_HIGH="99215",
            TPCT_MCTR_SETG="O", TPCT_MCTR_TCAT="P", TPCT_MODIFIER="  ", SESE_ID="OV01")
        ins(cur, "CMC_TPCT_CPT_CONV", TPCT_PFX="TP09", TPCT_IPCD_LOW="99201", TPCT_IPCD_HIGH="99215",
            TPCT_MCTR_SETG="O", TPCT_MCTR_TCAT="P", TPCT_MODIFIER="  ", SESE_ID="OV01")
        ins(cur, "CMC_RCCT_CONV", TPCT_PFX="TP02", RCCT_RCRC_ID_LOW="0100", RCCT_RCRC_ID_HIGH="0199",
            RCCT_MCTR_SETG="I", TPCT_MCTR_TCAT="H", SESE_ID="IP01")
        ins(cur, "CMC_SECT_CONV", TPCT_PFX="TP01", SESE_ID="OV01")
        f.commit()


def build_empty_store():
    """A new deployment: the SDM platform database with nothing in it yet."""
    recreate(SDB)


