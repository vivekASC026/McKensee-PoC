-- 04_denorm_837_extract.sql: fill the 837 support tables from FACETS.
--
-- Run after 03_denorm_837_tables.sql, in the customer's SDM platform database.
-- Reads FACETS tables as dbo.CMC_*. If FACETS is a different database on the
-- same server, replace "dbo.CMC_" with "<FacetsDb>.dbo.CMC_" throughout (or
-- create synonyms with those names). Written for schema sdm; for another
-- schema replace "sdm." throughout.
--
-- Full refresh: each section TRUNCATEs only its own new sdm table and loads it
-- again. sdm.member_denorm and sdm.claims_denorm are read, never changed.
--
-- Each section runs in one transaction, so a section that fails is rolled
-- back and its table keeps the rows it had. A failed section also stops every
-- section after it (they read what it fills, so they would load from a stale
-- or partial scope): the next section reports it and sets NOEXEC ON, and no
-- row counts are printed. Fix the cause, reconnect (NOEXEC lasts for the
-- session) and run the whole script again. Run with sqlcmd -b to stop at the
-- first error as well.
--
-- Scope: only what the existing denorm tables reference -- their members,
-- claims, plans and products -- plus the providers those members and claims
-- touch and the providers in the plans' networks (the pool new claims draw
-- from). Temporary tables (#scope_*) carry the scope between batches, so run
-- the whole script in one session (SSMS window or one sqlcmd call).
--
-- Dates: Facets term dates may be NULL or a high date; every overlap test
-- treats NULL as 9999-12-31. Term dates are taken as inclusive.
--
-- Two code values are assumptions, not dictionary facts (see
-- docs/facets-837-sources.md): PDBC_TYPE = 'SEDF' for the service-definition
-- component, and IPCD_ID positions 6-7 holding the first modifier.

-- Never in a production database. SDM writes their names (SDG_PROTECTED_DATABASES)
-- in place of {{PROTECTED_DATABASES}} when it runs this script; run by hand, write
-- them there yourself (every one: Find and Replace all), separated by commas,
-- or the script stops.
-- The first batch
-- stops the script there with an error; the second keeps every batch after
-- it from running (SET NOEXEC ON lasts for the session: reconnect after).
IF N'{{PROTECTED_DATABASES}}' LIKE N'{{%'
    THROW 50000, N'This script names no production database yet: SDM names them when it runs it; run by hand, replace the placeholder in its first lines with their names, separated by commas. Nothing was changed.', 1;
IF (SELECT CASE WHEN p.v LIKE N'{{%' OR CHARINDEX(N',' + UPPER(DB_NAME()) + N',', N',' + UPPER(REPLACE(p.v, N' ', N'')) + N',') > 0 THEN 1 ELSE 0 END FROM (SELECT N'{{PROTECTED_DATABASES}}' AS v) AS p) = 1
    THROW 50000, N'This script runs in the SDM platform database, never in a production database. Nothing was changed. Choose the SDM platform database and run it again.', 1;
GO
IF (SELECT CASE WHEN p.v LIKE N'{{%' OR CHARINDEX(N',' + UPPER(DB_NAME()) + N',', N',' + UPPER(REPLACE(p.v, N' ', N'')) + N',') > 0 THEN 1 ELSE 0 END FROM (SELECT N'{{PROTECTED_DATABASES}}' AS v) AS p) = 1
    SET NOEXEC ON;
GO

SET NOCOUNT ON;
SET XACT_ABORT ON;
-- The sections finished so far. Each section checks that the one before it
-- finished, and records itself in the same transaction as its load.
IF OBJECT_ID('tempdb..#extract_step') IS NOT NULL DROP TABLE #extract_step;
CREATE TABLE #extract_step (step int NOT NULL);
INSERT INTO #extract_step (step) VALUES (0);
GO

-- ===========================================================================
-- 0. Scope
-- ===========================================================================
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 0
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
IF OBJECT_ID('tempdb..#scope_member') IS NOT NULL DROP TABLE #scope_member;
CREATE TABLE #scope_member (MEME_CK int NOT NULL PRIMARY KEY);
INSERT INTO #scope_member (MEME_CK)
SELECT MEME_CK FROM sdm.member_denorm WHERE MEME_CK IS NOT NULL
UNION
SELECT MEME_CK FROM sdm.claims_denorm WHERE MEME_CK IS NOT NULL;

IF OBJECT_ID('tempdb..#scope_claim') IS NOT NULL DROP TABLE #scope_claim;
CREATE TABLE #scope_claim (CLCL_ID char(12) NOT NULL PRIMARY KEY);
INSERT INTO #scope_claim (CLCL_ID)
SELECT DISTINCT CLCL_ID FROM sdm.claims_denorm WHERE CLCL_ID IS NOT NULL;
INSERT INTO #extract_step (step) VALUES (1);
COMMIT TRANSACTION;
GO

-- ===========================================================================
-- 1. Member
-- ===========================================================================

-- ### sdm.member_elig
-- Every eligibility span of the members in scope. Carries CSCS_ID, the key
-- member_denorm lacks for CMC_CSPI_CS_PLAN / CMC_CSCS_CLASS.
-- Pick the span for a service date: MEPE_EFF_DT <= svc_dt AND
-- ISNULL(MEPE_TERM_DT, '99991231') >= svc_dt AND MEPE_ELIG_IND = 'Y'.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 1
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.member_elig;
INSERT INTO sdm.member_elig WITH (TABLOCK)
    (MEME_CK, CSPD_CAT, MEPE_EFF_DT, MEPE_TERM_DT, MEPE_ELIG_IND, GRGR_CK, SGSG_CK,
     CSCS_ID, CSPI_ID, PDPD_ID, MEPE_FI, MEPE_PLAN_ENTRY_DT)
SELECT e.MEME_CK, e.CSPD_CAT, e.MEPE_EFF_DT, e.MEPE_TERM_DT, e.MEPE_ELIG_IND, e.GRGR_CK, e.SGSG_CK,
       e.CSCS_ID, e.CSPI_ID, e.PDPD_ID, e.MEPE_FI, e.MEPE_PLAN_ENTRY_DT
FROM dbo.CMC_MEPE_PRCS_ELIG e
JOIN #scope_member s ON s.MEME_CK = e.MEME_CK;
INSERT INTO #extract_step (step) VALUES (2);
COMMIT TRANSACTION;
GO

-- ### sdm.subscriber_denorm
-- 2000B / 2010BA: subscriber name, group number (SBR03) and home address.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 2
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.subscriber_denorm;
INSERT INTO sdm.subscriber_denorm WITH (TABLOCK)
    (SBSB_CK, GRGR_CK, GRGR_ID, SBSB_ID, SBSB_LAST_NAME, SBSB_FIRST_NAME, SBSB_MID_INIT,
     SBAD_TYPE_HOME, SBAD_ADDR1, SBAD_ADDR2, SBAD_CITY, SBAD_STATE, SBAD_ZIP, SBAD_COUNTY, SBAD_CTRY_CD)
SELECT sb.SBSB_CK, sb.GRGR_CK, g.GRGR_ID, sb.SBSB_ID, sb.SBSB_LAST_NAME, sb.SBSB_FIRST_NAME, sb.SBSB_MID_INIT,
       sb.SBAD_TYPE_HOME, a.SBAD_ADDR1, a.SBAD_ADDR2, a.SBAD_CITY, a.SBAD_STATE, a.SBAD_ZIP, a.SBAD_COUNTY, a.SBAD_CTRY_CD
FROM dbo.CMC_SBSB_SUBSC sb
JOIN (SELECT DISTINCT m.SBSB_CK
      FROM dbo.CMC_MEME_MEMBER m
      JOIN #scope_member s ON s.MEME_CK = m.MEME_CK) ss ON ss.SBSB_CK = sb.SBSB_CK
LEFT JOIN dbo.CMC_GRGR_GROUP g ON g.GRGR_CK = sb.GRGR_CK
LEFT JOIN dbo.CMC_SBAD_ADDR a ON a.SBSB_CK = sb.SBSB_CK AND a.SBAD_TYPE = sb.SBAD_TYPE_HOME;
INSERT INTO #extract_step (step) VALUES (3);
COMMIT TRANSACTION;
GO

-- ### sdm.member_address
-- 2010CA patient address (2010BA when the member is the subscriber). Facets
-- rule from the dictionary: a member with no home or mailing address type of
-- its own uses the subscriber's home address. CMC_SBAD_ADDR has no dates, so
-- this is the current address.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 3
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.member_address;
INSERT INTO sdm.member_address WITH (TABLOCK)
    (MEME_CK, ADDR_ROLE, SBSB_CK, SBAD_TYPE, FROM_SUBSCRIBER,
     SBAD_ADDR1, SBAD_ADDR2, SBAD_CITY, SBAD_STATE, SBAD_ZIP, SBAD_COUNTY, SBAD_CTRY_CD, SBAD_PHONE)
SELECT m.MEME_CK, r.ADDR_ROLE, m.SBSB_CK, a.SBAD_TYPE, r.FROM_SUBSCRIBER,
       a.SBAD_ADDR1, a.SBAD_ADDR2, a.SBAD_CITY, a.SBAD_STATE, a.SBAD_ZIP, a.SBAD_COUNTY, a.SBAD_CTRY_CD, a.SBAD_PHONE
FROM dbo.CMC_MEME_MEMBER m
JOIN #scope_member s ON s.MEME_CK = m.MEME_CK
JOIN dbo.CMC_SBSB_SUBSC sb ON sb.SBSB_CK = m.SBSB_CK
CROSS APPLY (VALUES
    ('HOME', COALESCE(NULLIF(RTRIM(m.SBAD_TYPE_HOME), ''), sb.SBAD_TYPE_HOME),
             CASE WHEN NULLIF(RTRIM(m.SBAD_TYPE_HOME), '') IS NULL THEN 'Y' ELSE 'N' END),
    ('MAIL', COALESCE(NULLIF(RTRIM(m.SBAD_TYPE_MAIL), ''), sb.SBAD_TYPE_HOME),
             CASE WHEN NULLIF(RTRIM(m.SBAD_TYPE_MAIL), '') IS NULL THEN 'Y' ELSE 'N' END)
) r (ADDR_ROLE, ADDR_TYPE, FROM_SUBSCRIBER)
JOIN dbo.CMC_SBAD_ADDR a ON a.SBSB_CK = m.SBSB_CK AND a.SBAD_TYPE = r.ADDR_TYPE;
INSERT INTO #extract_step (step) VALUES (4);
COMMIT TRANSACTION;
GO

-- ### sdm.member_pcp
-- PCP history. PCP on svc_dt: MEPR_EFF_DT <= svc_dt AND
-- ISNULL(MEPR_TERM_DT, '99991231') >= svc_dt (per MEPR_PCP_TYPE).
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 4
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.member_pcp;
INSERT INTO sdm.member_pcp WITH (TABLOCK)
    (MEME_CK, MEPR_PCP_TYPE, MEPR_EFF_DT, MEPR_TERM_DT, GRGR_CK, PRPR_ID, PRPR_NPI, PRPR_ENTITY)
SELECT pc.MEME_CK, pc.MEPR_PCP_TYPE, pc.MEPR_EFF_DT, pc.MEPR_TERM_DT, pc.GRGR_CK, pc.PRPR_ID, p.PRPR_NPI, p.PRPR_ENTITY
FROM dbo.CMC_MEPR_PRIM_PROV pc
JOIN #scope_member s ON s.MEME_CK = pc.MEME_CK
LEFT JOIN dbo.CMC_PRPR_PROV p ON p.PRPR_ID = pc.PRPR_ID;
INSERT INTO #extract_step (step) VALUES (5);
COMMIT TRANSACTION;
GO

-- ### sdm.member_cob
-- Other coverage (2000B SBR01, 2320, 2330A, 2330B) with the other carrier.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 5
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.member_cob;
INSERT INTO sdm.member_cob WITH (TABLOCK)
    (MEME_CK, MECB_INSUR_TYPE, MECB_INSUR_ORDER, MECB_MCTR_STYP, MECB_EFF_DT, MECB_TERM_DT,
     MECB_POLICY_ID, MECB_PRIM_LAST_NM, MECB_PRIM_FIRST_NM, MECB_PRIM_ID, MECB_MCTR_MSP,
     MCRE_ID, MCRE_NAME, MCRE_ADDR1, MCRE_ADDR2, MCRE_CITY, MCRE_STATE, MCRE_ZIP)
SELECT cb.MEME_CK, cb.MECB_INSUR_TYPE, cb.MECB_INSUR_ORDER, cb.MECB_MCTR_STYP, cb.MECB_EFF_DT, cb.MECB_TERM_DT,
       cb.MECB_POLICY_ID, cb.MECB_PRIM_LAST_NM, cb.MECB_PRIM_FIRST_NM, cb.MECB_PRIM_ID, cb.MECB_MCTR_MSP,
       cb.MCRE_ID, re.MCRE_NAME, re.MCRE_ADDR1, re.MCRE_ADDR2, re.MCRE_CITY, re.MCRE_STATE, re.MCRE_ZIP
FROM dbo.CMC_MECB_COB cb
JOIN #scope_member s ON s.MEME_CK = cb.MEME_CK
LEFT JOIN dbo.CMC_MCRE_RELAT_ENT re ON re.MCRE_ID = cb.MCRE_ID;
INSERT INTO #extract_step (step) VALUES (6);
COMMIT TRANSACTION;
GO

-- ===========================================================================
-- 2. Plan and benefit
-- ===========================================================================
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 6
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
IF OBJECT_ID('tempdb..#scope_plan') IS NOT NULL DROP TABLE #scope_plan;
CREATE TABLE #scope_plan (GRGR_CK int NOT NULL, CSCS_ID char(4) NOT NULL, CSPD_CAT char(1) NOT NULL,
                          CSPI_ID char(8) NOT NULL, PRIMARY KEY (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID));
INSERT INTO #scope_plan (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID)
SELECT GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID FROM sdm.member_elig
WHERE GRGR_CK IS NOT NULL AND CSCS_ID IS NOT NULL AND CSPI_ID IS NOT NULL
UNION
SELECT cl.GRGR_CK, cl.CSCS_ID, cl.CSPD_CAT, cl.CSPI_ID
FROM dbo.CMC_CLCL_CLAIM cl
JOIN #scope_claim k ON k.CLCL_ID = cl.CLCL_ID
WHERE cl.GRGR_CK IS NOT NULL AND cl.CSCS_ID IS NOT NULL AND cl.CSPD_CAT IS NOT NULL AND cl.CSPI_ID IS NOT NULL;
INSERT INTO #extract_step (step) VALUES (7);
COMMIT TRANSACTION;
GO

-- ### sdm.plan_denorm
-- Class/plan rows with the product, network set (NWST_PFX), service conversion
-- prefix (TPCT_PFX), line of business and payer (2010BB). The product and payer
-- rows are the latest ones overlapping the plan row's dates.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 7
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.plan_denorm;
INSERT INTO sdm.plan_denorm WITH (TABLOCK)
    (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID, CSPI_EFF_DT, CSPI_TERM_DT, PDPD_ID, NWST_PFX, CSPI_PDPD_BEG_MMDD,
     PLDS_DESC, TPCT_PFX, LOBD_ID, PDDS_DESC, PDDS_PROD_TYPE, PDDS_MCTR_BCAT, PDDS_UM_IND, PDDS_OON_IND,
     PYPY_ID, PYPY_PAYOR_NAME, PYPY_NATL_PLAN_ID, PYPY_NAIC_CODE, PYPY_TAX_ID,
     PYPY_ADDR1, PYPY_ADDR2, PYPY_CITY, PYPY_STATE, PYPY_ZIP)
SELECT p.GRGR_CK, p.CSCS_ID, p.CSPD_CAT, p.CSPI_ID, p.CSPI_EFF_DT, p.CSPI_TERM_DT, p.PDPD_ID, p.NWST_PFX, p.CSPI_PDPD_BEG_MMDD,
       pl.PLDS_DESC, cs.TPCT_PFX, pd.LOBD_ID, ds.PDDS_DESC, ds.PDDS_PROD_TYPE, ds.PDDS_MCTR_BCAT, ds.PDDS_UM_IND, ds.PDDS_OON_IND,
       lb.PYPY_ID, py.PYPY_PAYOR_NAME, py.PYPY_NATL_PLAN_ID, py.PYPY_NAIC_CODE, py.PYPY_TAX_ID,
       py.PYPY_ADDR1, py.PYPY_ADDR2, py.PYPY_CITY, py.PYPY_STATE, py.PYPY_ZIP
FROM dbo.CMC_CSPI_CS_PLAN p
JOIN #scope_plan k
  ON k.GRGR_CK = p.GRGR_CK AND k.CSCS_ID = p.CSCS_ID AND k.CSPD_CAT = p.CSPD_CAT AND k.CSPI_ID = p.CSPI_ID
-- One row from each description table, whatever its key is in the customer's
-- release (the dictionary keys both on the id alone): several rows per id
-- must not multiply plans or break plan_denorm's key.
OUTER APPLY (SELECT TOP 1 x.PLDS_DESC FROM dbo.CMC_PLDS_PLAN_DESC x
             WHERE x.CSPI_ID = p.CSPI_ID ORDER BY x.PLDS_DESC) pl
LEFT JOIN dbo.CMC_CSCS_CLASS cs ON cs.GRGR_CK = p.GRGR_CK AND cs.CSCS_ID = p.CSCS_ID
OUTER APPLY (SELECT TOP 1 x.LOBD_ID
             FROM dbo.CMC_PDPD_PRODUCT x
             WHERE x.PDPD_ID = p.PDPD_ID
               AND x.PDPD_EFF_DT <= ISNULL(p.CSPI_TERM_DT, '99991231')
               AND ISNULL(x.PDPD_TERM_DT, '99991231') >= p.CSPI_EFF_DT
             ORDER BY x.PDPD_EFF_DT DESC) pd
OUTER APPLY (SELECT TOP 1 x.PDDS_DESC, x.PDDS_PROD_TYPE, x.PDDS_MCTR_BCAT, x.PDDS_UM_IND, x.PDDS_OON_IND
             FROM dbo.CMC_PDDS_PROD_DESC x
             WHERE x.PDPD_ID = p.PDPD_ID ORDER BY x.PDDS_DESC) ds
LEFT JOIN dbo.CMC_LOBD_LINE_BUS lb ON lb.LOBD_ID = pd.LOBD_ID
OUTER APPLY (SELECT TOP 1 y.PYPY_PAYOR_NAME, y.PYPY_NATL_PLAN_ID, y.PYPY_NAIC_CODE, y.PYPY_TAX_ID,
                    y.PYPY_ADDR1, y.PYPY_ADDR2, y.PYPY_CITY, y.PYPY_STATE, y.PYPY_ZIP
             FROM dbo.CMC_PYPY_PAYOR y
             WHERE y.PYPY_ID = lb.PYPY_ID
               AND y.PYPY_EFF_DT <= ISNULL(p.CSPI_TERM_DT, '99991231')
               AND ISNULL(y.PYPY_TERM_DT, '99991231') >= p.CSPI_EFF_DT
             ORDER BY y.PYPY_EFF_DT DESC) py;
INSERT INTO #extract_step (step) VALUES (8);
COMMIT TRANSACTION;
GO

-- ### sdm.plan_network
-- The networks of each plan's network set, with in-network/participating
-- status and the tier that selects the cost-share row in plan_benefit_denorm.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 8
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.plan_network;
INSERT INTO sdm.plan_network WITH (TABLOCK)
    (NWST_PFX, NWST_EFF_DT, NWST_SEQ_NO, NWST_TERM_DT, NWNW_ID, NWPR_PFX, NWST_PR_NW_STS, PDVC_TIER,
     NWNW_NAME, NWNW_MCTR_TYPE)
SELECT ns.NWST_PFX, ns.NWST_EFF_DT, ns.NWST_SEQ_NO, ns.NWST_TERM_DT, ns.NWNW_ID, ns.NWPR_PFX, ns.NWST_PR_NW_STS, ns.PDVC_TIER,
       n.NWNW_NAME, n.NWNW_MCTR_TYPE
FROM dbo.CMC_NWST_NET_SET ns
JOIN (SELECT DISTINCT NWST_PFX FROM sdm.plan_denorm) k ON k.NWST_PFX = ns.NWST_PFX
LEFT JOIN dbo.CMC_NWNW_NETWORK n ON n.NWNW_ID = ns.NWNW_ID;
INSERT INTO #extract_step (step) VALUES (9);
COMMIT TRANSACTION;
GO

IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 9
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
IF OBJECT_ID('tempdb..#scope_pdpd') IS NOT NULL DROP TABLE #scope_pdpd;
CREATE TABLE #scope_pdpd (PDPD_ID char(8) NOT NULL PRIMARY KEY);
INSERT INTO #scope_pdpd (PDPD_ID)
SELECT PDPD_ID FROM sdm.plan_denorm WHERE PDPD_ID IS NOT NULL
UNION
SELECT PDPD_ID FROM sdm.member_elig WHERE PDPD_ID IS NOT NULL
UNION
SELECT PDPD_ID FROM sdm.claims_denorm WHERE PDPD_ID IS NOT NULL;
INSERT INTO #extract_step (step) VALUES (10);
COMMIT TRANSACTION;
GO

-- ### sdm.plan_benefit_denorm
-- What a product covers and at what cost share, per provider tier:
--   PDVC (product x tier x conditions) -> SEPY (covered Service IDs + rule)
--   -> SESE (rule type, age/sex) -> SETR (copay, coinsurance, deductible
--   accumulator) and SEDF (referral / pre-authorization requirement).
-- BEN_EFF_DT / BEN_TERM_DT = overlap of the PDVC and SEPY windows
-- ('9999-12-31' when both are open). SEDF comes from the product's
-- service-definition component; PDBC_TYPE = 'SEDF' is assumed -- check
-- CMC_PDPT_DESC on the customer's database.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 10
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.plan_benefit_denorm;
INSERT INTO sdm.plan_benefit_denorm WITH (TABLOCK)
    (PDPD_ID, PDVC_TIER, PDVC_TYPE, PDVC_SEQ_NO, PDVC_EFF_DT, PDVC_TERM_DT,
     PDVC_PR_PCP, PDVC_PR_IN, PDVC_PR_PAR, PDVC_PR_NONPAR, PDVC_PC_NR, PDVC_PC_OBT, PDVC_PC_VIOL,
     PDVC_REF_NR, PDVC_REF_OBT, PDVC_REF_VIOL, PDVC_LOBD_PTR, SEPY_PFX, DEDE_PFX, LTLT_PFX,
     SEPY_EFF_DT, SEPY_TERM_DT, BEN_EFF_DT, BEN_TERM_DT, SESE_ID, SESE_RULE, SEDS_DESC, SEDS_TYPE,
     SESE_RULE_TYPE, SESE_CALC_IND, SESE_VALID_SEX, SESE_MIN_AGE, SESE_MAX_AGE, SESE_COV_TYPE,
     SETR_TIER_NO, SETR_ALLOW_AMT, SETR_ALLOW_CTR, SETR_COPAY_AMT, SETR_COIN_PCT, ACAC_ACC_NO,
     SEDF_UM_REQ_IND, SEDF_UM_BP_REF_IND, SEDF_CAP_IND)
SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT,
       v.PDVC_PR_PCP, v.PDVC_PR_IN, v.PDVC_PR_PAR, v.PDVC_PR_NONPAR, v.PDVC_PC_NR, v.PDVC_PC_OBT, v.PDVC_PC_VIOL,
       v.PDVC_REF_NR, v.PDVC_REF_OBT, v.PDVC_REF_VIOL, v.PDVC_LOBD_PTR, v.SEPY_PFX, v.DEDE_PFX, v.LTLT_PFX,
       sp.SEPY_EFF_DT, sp.SEPY_TERM_DT, b.BEN_EFF_DT, b.BEN_TERM_DT, sp.SESE_ID, sp.SESE_RULE, sd.SEDS_DESC, sd.SEDS_TYPE,
       se.SESE_RULE_TYPE, se.SESE_CALC_IND, se.SESE_VALID_SEX, se.SESE_MIN_AGE, se.SESE_MAX_AGE, se.SESE_COV_TYPE,
       tr.SETR_TIER_NO, tr.SETR_ALLOW_AMT, tr.SETR_ALLOW_CTR, tr.SETR_COPAY_AMT, tr.SETR_COIN_PCT, tr.ACAC_ACC_NO,
       df.SEDF_UM_REQ_IND, df.SEDF_UM_BP_REF_IND, df.SEDF_CAP_IND
FROM dbo.CMC_PDVC_VAR_COMP v
JOIN #scope_pdpd k ON k.PDPD_ID = v.PDPD_ID
JOIN dbo.CMC_SEPY_SE_PYMT sp
  ON sp.SEPY_PFX = v.SEPY_PFX
 AND sp.SEPY_EFF_DT <= ISNULL(v.PDVC_TERM_DT, '99991231')
 AND ISNULL(sp.SEPY_TERM_DT, '99991231') >= v.PDVC_EFF_DT
CROSS APPLY (SELECT CASE WHEN sp.SEPY_EFF_DT > v.PDVC_EFF_DT THEN sp.SEPY_EFF_DT ELSE v.PDVC_EFF_DT END AS BEN_EFF_DT,
                    CASE WHEN ISNULL(sp.SEPY_TERM_DT, '99991231') < ISNULL(v.PDVC_TERM_DT, '99991231')
                         THEN ISNULL(sp.SEPY_TERM_DT, '99991231') ELSE ISNULL(v.PDVC_TERM_DT, '99991231') END AS BEN_TERM_DT) b
LEFT JOIN dbo.CMC_SESE_SERVICE se ON se.SESE_ID = sp.SESE_ID AND se.SESE_RULE = sp.SESE_RULE
LEFT JOIN dbo.CMC_SEDS_SE_DESC sd ON sd.SESE_ID = sp.SESE_ID
LEFT JOIN dbo.CMC_SETR_TIER tr ON tr.SESE_ID = sp.SESE_ID AND tr.SESE_RULE = sp.SESE_RULE
OUTER APPLY (SELECT TOP 1 f.SEDF_UM_REQ_IND, f.SEDF_UM_BP_REF_IND, f.SEDF_CAP_IND
             FROM dbo.CMC_PDBC_PROD_COMP pc
             JOIN dbo.CMC_SEDF_SE_DEFN f ON f.PDBC_PFX = pc.PDBC_PFX AND f.SESE_ID = sp.SESE_ID
             WHERE pc.PDPD_ID = v.PDPD_ID
               AND pc.PDBC_TYPE = 'SEDF'
               AND pc.PDBC_EFF_DT <= b.BEN_EFF_DT AND ISNULL(pc.PDBC_TERM_DT, '99991231') >= b.BEN_EFF_DT
               AND f.SEDF_EFF_DT <= b.BEN_EFF_DT AND ISNULL(f.SEDF_TERM_DT, '99991231') >= b.BEN_EFF_DT
             ORDER BY f.SEDF_EFF_DT DESC) df;
INSERT INTO #extract_step (step) VALUES (11);
COMMIT TRANSACTION;
GO

-- ### sdm.plan_accumulator
-- Deductibles (DEDE) and limits / stop-loss (LTLT, with the Service IDs they
-- count from LTSE) reached from each product variable component.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 11
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.plan_accumulator;
INSERT INTO sdm.plan_accumulator WITH (TABLOCK)
    (PDPD_ID, PDVC_TIER, PDVC_TYPE, PDVC_SEQ_NO, PDVC_EFF_DT, PDVC_TERM_DT, ACCUM_KIND, ACCUM_PFX,
     ACAC_ACC_NO, ACCUM_DESC, DEDE_RULE, DEDE_PERIOD_IND, DEDE_MEME_AMT, DEDE_FAM_AMT)
SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT, 'DED', d.DEDE_PFX,
       d.ACAC_ACC_NO, d.DEDE_DESC, d.DEDE_RULE, d.DEDE_PERIOD_IND, d.DEDE_MEME_AMT, d.DEDE_FAM_AMT
FROM dbo.CMC_PDVC_VAR_COMP v
JOIN #scope_pdpd k ON k.PDPD_ID = v.PDPD_ID
JOIN dbo.CMC_DEDE_DEDUCT d ON d.DEDE_PFX = v.DEDE_PFX;

INSERT INTO sdm.plan_accumulator WITH (TABLOCK)
    (PDPD_ID, PDVC_TIER, PDVC_TYPE, PDVC_SEQ_NO, PDVC_EFF_DT, PDVC_TERM_DT, ACCUM_KIND, ACCUM_PFX,
     ACAC_ACC_NO, ACCUM_DESC, LTLT_CAT, LTLT_LEVEL, LTLT_PERIOD_IND, LTLT_RULE, LTLT_IX_IND, LTLT_IX_TYPE,
     LTLT_AMT1, SESE_ID, LTSE_WT_CTR)
SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT, 'LMT', l.LTLT_PFX,
       l.ACAC_ACC_NO, l.LTLT_DESC, l.LTLT_CAT, l.LTLT_LEVEL, l.LTLT_PERIOD_IND, l.LTLT_RULE, l.LTLT_IX_IND, l.LTLT_IX_TYPE,
       l.LTLT_AMT1, x.SESE_ID, x.LTSE_WT_CTR
FROM dbo.CMC_PDVC_VAR_COMP v
JOIN #scope_pdpd k ON k.PDPD_ID = v.PDPD_ID
JOIN dbo.CMC_LTLT_LIMIT l ON l.LTLT_PFX = v.LTLT_PFX
LEFT JOIN dbo.CMC_LTSE_SE_IX x ON x.LTLT_PFX = l.LTLT_PFX AND x.ACAC_ACC_NO = l.ACAC_ACC_NO;
INSERT INTO #extract_step (step) VALUES (12);
COMMIT TRANSACTION;
GO

-- ### sdm.service_code_map
-- Procedure / revenue code + setting -> Service ID, for the plans' service
-- conversion prefixes (CMC_CSCS_CLASS.TPCT_PFX). With place_of_service this
-- tells which procedures, at which POS, fall under a covered Service ID.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 12
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.service_code_map;
INSERT INTO sdm.service_code_map WITH (TABLOCK)
    (TPCT_PFX, MAP_KIND, TPCT_MCTR_TCAT, MCTR_SETG, CODE_LOW, CODE_HIGH, TPCT_MODIFIER, SESE_ID, CSPD_CAT, RPTC_RPT_CAT)
SELECT tp.TPCT_PFX, 'PROC', tp.TPCT_MCTR_TCAT, tp.TPCT_MCTR_SETG, tp.TPCT_IPCD_LOW, tp.TPCT_IPCD_HIGH, tp.TPCT_MODIFIER,
       tp.SESE_ID, tp.CSPD_CAT, tp.RPTC_RPT_CAT
FROM dbo.CMC_TPCT_CPT_CONV tp
JOIN (SELECT DISTINCT TPCT_PFX FROM sdm.plan_denorm WHERE TPCT_PFX IS NOT NULL) k ON k.TPCT_PFX = tp.TPCT_PFX;

INSERT INTO sdm.service_code_map WITH (TABLOCK)
    (TPCT_PFX, MAP_KIND, TPCT_MCTR_TCAT, MCTR_SETG, CODE_LOW, CODE_HIGH, SESE_ID, CSPD_CAT, RPTC_RPT_CAT)
SELECT rc.TPCT_PFX, 'REV', rc.TPCT_MCTR_TCAT, rc.RCCT_MCTR_SETG, rc.RCCT_RCRC_ID_LOW, rc.RCCT_RCRC_ID_HIGH,
       rc.SESE_ID, rc.CSPD_CAT, rc.RPTC_RPT_CAT
FROM dbo.CMC_RCCT_CONV rc
JOIN (SELECT DISTINCT TPCT_PFX FROM sdm.plan_denorm WHERE TPCT_PFX IS NOT NULL) k ON k.TPCT_PFX = rc.TPCT_PFX;

INSERT INTO sdm.service_code_map WITH (TABLOCK)
    (TPCT_PFX, MAP_KIND, SESE_ID, CSPD_CAT, RPTC_RPT_CAT)
SELECT sc.TPCT_PFX, 'SVC', sc.SESE_ID, sc.CSPD_CAT, sc.RPTC_RPT_CAT
FROM dbo.CMC_SECT_CONV sc
JOIN (SELECT DISTINCT TPCT_PFX FROM sdm.plan_denorm WHERE TPCT_PFX IS NOT NULL) k ON k.TPCT_PFX = sc.TPCT_PFX;
INSERT INTO #extract_step (step) VALUES (13);
COMMIT TRANSACTION;
GO

-- ### sdm.place_of_service
-- All places of service with their setting (small reference table).
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 13
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.place_of_service;
INSERT INTO sdm.place_of_service WITH (TABLOCK)
    (PSCD_ID, PSCD_DESC, PSCD_POS_IND, TPCT_MCTR_TCAT, TPCT_MCTR_SETG)
SELECT ps.PSCD_ID, ps.PSCD_DESC, ps.PSCD_POS_IND, ps.TPCT_MCTR_TCAT, ps.TPCT_MCTR_SETG
FROM dbo.CMC_PSCD_POS_DESC ps;
INSERT INTO #extract_step (step) VALUES (14);
COMMIT TRANSACTION;
GO

-- ===========================================================================
-- 3. Claims
-- ===========================================================================

-- ### sdm.claims_header_ext_denorm
-- One row per claim: 2300 fields, institutional (UB) header, claim-level
-- providers (attending, operating, service facility, rendering, payee), EDI
-- and claim-level COB. CLM_FREQUENCY for 837P is derived: '7' when the claim
-- adjusts another, else '1' (assumption; voids are not identifiable).
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 14
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.claims_header_ext_denorm;
INSERT INTO sdm.claims_header_ext_denorm WITH (TABLOCK)
    (CLCL_ID, MEME_CK, CSCS_ID, PRPR_ID, PRAD_TYPE, CLCL_PAYEE_PR_ID, NWPR_PFX, AGAG_ID,
     CLCL_PA_ACCT_NO, CLCL_PAY_PR_IND, CLCL_REL_INFO_IND, CLCL_MED_ASSGN_IND,
     CLCL_ACD_IND, CLCL_ACD_STATE, CLCL_ACD_DT, CLCL_CURR_ILL_DT, CLCL_SIMI_ILL_DT,
     CLCL_UNABL_FROM_DT, CLCL_UNABL_TO_DT, CLCL_RELHP_FROM_DT, CLCL_RELHP_TO_DT,
     CLCL_OUT_LAB_IND, CLCL_ID_ADJ_FROM, CLCL_MECD_RESUB_NO, CLCL_OTHER_BN_IND, CLCL_EXT_REF_IND, CLM_FREQUENCY,
     CLHP_FAC_TYPE, CLHP_BILL_CLASS, CLHP_FREQUENCY, CLHP_ADM_TYP, CLHP_ADM_SOURCE, CLHP_DC_STAT,
     CLHP_ADM_DT, CLHP_ADM_HOUR_CD, CLHP_DC_DT, CLHP_DC_HOUR_CD, CLHP_STAMENT_FR_DT, CLHP_STAMENT_TO_DT,
     CLHP_MED_REC_NO, AGRG_ID, CLHP_INPUT_AGRG_ID,
     CLHP_PRPR_ID_ADM, CLHP_PRPR_AD_NPI, CLHP_PRPR_ID_OPER, CLHP_PRPR_OP_NPI, CLHP_PRPR_ID_OTH1, CLHP_PRPR_O1_NPI,
     CLHP_ICD_QUAL_IND, CLHP_BIRTH_WGT,
     CLMF_PRPR_ID_FAC, CLMF_PRPR_FA_NPI, CLMF_PRPR_RF_NPI, CLMF_INPUT_TXNM_CD, CLMF_EXT_REFRAL_NO, CLMF_EXT_PREATH_NO,
     CLMF_ICD_IND_PROC, CLSD_PRPR_ID_REND_NVL, CLSD_PRPR_RE_NPI_NVL,
     CLED_HIPAA_VERSION, CLED_EXT_REF, CLED_TRAD_PARTNER,
     CLCB_COB_TYPE, CLCB_COB_AMT, CLCB_COB_ALLOW, CLCB_COB_DED_AMT, CLCB_COB_COPAY_AMT, CLCB_COB_COINS_AMT, CLCB_COB_REAS_CD)
SELECT cl.CLCL_ID, cl.MEME_CK, cl.CSCS_ID, cl.PRPR_ID, cl.PRAD_TYPE, cl.CLCL_PAYEE_PR_ID, cl.NWPR_PFX, cl.AGAG_ID,
       cl.CLCL_PA_ACCT_NO, cl.CLCL_PAY_PR_IND, cl.CLCL_REL_INFO_IND, cl.CLCL_MED_ASSGN_IND,
       cl.CLCL_ACD_IND, cl.CLCL_ACD_STATE, cl.CLCL_ACD_DT, cl.CLCL_CURR_ILL_DT, cl.CLCL_SIMI_ILL_DT,
       cl.CLCL_UNABL_FROM_DT, cl.CLCL_UNABL_TO_DT, cl.CLCL_RELHP_FROM_DT, cl.CLCL_RELHP_TO_DT,
       cl.CLCL_OUT_LAB_IND, cl.CLCL_ID_ADJ_FROM, cl.CLCL_MECD_RESUB_NO, cl.CLCL_OTHER_BN_IND, cl.CLCL_EXT_REF_IND,
       CASE WHEN NULLIF(RTRIM(cl.CLCL_ID_ADJ_FROM), '') IS NOT NULL THEN '7' ELSE '1' END,
       hp.CLHP_FAC_TYPE, hp.CLHP_BILL_CLASS, hp.CLHP_FREQUENCY, hp.CLHP_ADM_TYP, hp.CLHP_ADM_SOURCE, hp.CLHP_DC_STAT,
       hp.CLHP_ADM_DT, hp.CLHP_ADM_HOUR_CD, hp.CLHP_DC_DT, hp.CLHP_DC_HOUR_CD, hp.CLHP_STAMENT_FR_DT, hp.CLHP_STAMENT_TO_DT,
       hp.CLHP_MED_REC_NO, hp.AGRG_ID, hp.CLHP_INPUT_AGRG_ID,
       hp.CLHP_PRPR_ID_ADM, hp.CLHP_PRPR_AD_NPI, hp.CLHP_PRPR_ID_OPER, hp.CLHP_PRPR_OP_NPI, hp.CLHP_PRPR_ID_OTH1, hp.CLHP_PRPR_O1_NPI,
       hp.CLHP_ICD_QUAL_IND, hp.CLHP_BIRTH_WGT,
       mf.CLMF_PRPR_ID_FAC, mf.CLMF_PRPR_FA_NPI, mf.CLMF_PRPR_RF_NPI, mf.CLMF_INPUT_TXNM_CD, mf.CLMF_EXT_REFRAL_NO, mf.CLMF_EXT_PREATH_NO,
       mf.CLMF_ICD_IND_PROC, sd.CLSD_PRPR_ID_REND_NVL, sd.CLSD_PRPR_RE_NPI_NVL,
       ed.CLED_HIPAA_VERSION, ed.CLED_EXT_REF, ed.CLED_TRAD_PARTNER,
       cb.CLCB_COB_TYPE, cb.CLCB_COB_AMT, cb.CLCB_COB_ALLOW, cb.CLCB_COB_DED_AMT, cb.CLCB_COB_COPAY_AMT, cb.CLCB_COB_COINS_AMT, cb.CLCB_COB_REAS_CD
FROM dbo.CMC_CLCL_CLAIM cl
JOIN #scope_claim k ON k.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLHP_HOSP hp ON hp.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLMF_MULT_FUNC mf ON mf.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLSD_SUPP_DATA sd ON sd.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLED_EDI_DATA ed ON ed.CLCL_ID = cl.CLCL_ID
-- One COB row per claim, the lowest COB type, should a release key the table
-- per COB type rather than on CLCL_ID alone (the dictionary's key).
OUTER APPLY (SELECT TOP 1 x.CLCB_COB_TYPE, x.CLCB_COB_AMT, x.CLCB_COB_ALLOW, x.CLCB_COB_DED_AMT,
                    x.CLCB_COB_COPAY_AMT, x.CLCB_COB_COINS_AMT, x.CLCB_COB_REAS_CD
             FROM dbo.CMC_CLCB_CL_COB x
             WHERE x.CLCL_ID = cl.CLCL_ID ORDER BY x.CLCB_COB_TYPE) cb;
INSERT INTO #extract_step (step) VALUES (15);
COMMIT TRANSACTION;
GO

-- ### sdm.claims_detail_denorm
-- One row per claim line: 2400 fields claims_denorm lacks. PROC_CODE and
-- IPCD_MOD1 split CDML_CL_LINE.IPCD_ID (char 7) into code and first modifier
-- (assumption: IPCD holds "procedure codes including those with modifiers").
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 15
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.claims_detail_denorm;
INSERT INTO sdm.claims_detail_denorm WITH (TABLOCK)
    (CLCL_ID, CDML_SEQ_NO, CDML_EXT_LINE_NO, PROC_CODE, IPCD_MOD1, CDML_IPCD_MOD2, CDML_IPCD_MOD3, CDML_IPCD_MOD4,
     IDCD_ID, IDCD_ID_REL, CDML_CLMD_TYPE2, CDML_CLMD_TYPE3, CDML_CLMD_TYPE4, CDML_CLMD_TYPE5,
     SESE_ID, SESE_RULE, SEPY_PFX, DEDE_PFX, LTLT_PFX, PDVC_LOBD_PTR, CDML_ROOM_TYPE, CDML_HCPCS_AMT,
     CDML_REF_IND, CDML_PC_IND, CDML_UMREF_ID, CDML_REFSV_SEQ_NO, CDML_UMAUTH_ID, CDML_AUTHSV_SEQ_NO,
     CDML_CL_NTWK_IND, CDML_DISALL_EXCD,
     CDSD_PRPR_ID_REND, CDSD_PRPR_RE_NPI, CDSD_NDC_CODE, CDSD_NDC_UNITS, CDSD_NDC_MCTR_TYPE,
     CDSD_SERV_LOC_ZIP, CDSD_MED_AMB_ZIP,
     CDCB_COB_TYPE, CDCB_COB_AMT, CDCB_COB_ALLOW, CDCB_COB_DED_AMT, CDCB_COB_COPAY_AMT, CDCB_COB_COINS_AMT, CDCB_COB_REAS_CD)
SELECT d.CLCL_ID, d.CDML_SEQ_NO, d.CDML_EXT_LINE_NO,
       CAST(LEFT(d.IPCD_ID, 5) AS char(5)), NULLIF(SUBSTRING(d.IPCD_ID, 6, 2), ''),
       d.CDML_IPCD_MOD2, d.CDML_IPCD_MOD3, d.CDML_IPCD_MOD4,
       d.IDCD_ID, d.IDCD_ID_REL, d.CDML_CLMD_TYPE2, d.CDML_CLMD_TYPE3, d.CDML_CLMD_TYPE4, d.CDML_CLMD_TYPE5,
       d.SESE_ID, d.SESE_RULE, d.SEPY_PFX, d.DEDE_PFX, d.LTLT_PFX, d.PDVC_LOBD_PTR, d.CDML_ROOM_TYPE, d.CDML_HCPCS_AMT,
       d.CDML_REF_IND, d.CDML_PC_IND, d.CDML_UMREF_ID, d.CDML_REFSV_SEQ_NO, d.CDML_UMAUTH_ID, d.CDML_AUTHSV_SEQ_NO,
       d.CDML_CL_NTWK_IND, d.CDML_DISALL_EXCD,
       sd.CDSD_PRPR_ID_REND, sd.CDSD_PRPR_RE_NPI, sd.CDSD_NDC_CODE, sd.CDSD_NDC_UNITS, sd.CDSD_NDC_MCTR_TYPE,
       sd.CDSD_SERV_LOC_ZIP, sd.CDSD_MED_AMB_ZIP,
       cb.CDCB_COB_TYPE, cb.CDCB_COB_AMT, cb.CDCB_COB_ALLOW, cb.CDCB_COB_DED_AMT, cb.CDCB_COB_COPAY_AMT, cb.CDCB_COB_COINS_AMT, cb.CDCB_COB_REAS_CD
FROM dbo.CMC_CDML_CL_LINE d
JOIN #scope_claim k ON k.CLCL_ID = d.CLCL_ID
LEFT JOIN dbo.CMC_CDSD_SUPP_DATA sd ON sd.CLCL_ID = d.CLCL_ID AND sd.CDML_SEQ_NO = d.CDML_SEQ_NO
-- One COB row per line, the lowest COB type (as for the claim above).
OUTER APPLY (SELECT TOP 1 x.CDCB_COB_TYPE, x.CDCB_COB_AMT, x.CDCB_COB_ALLOW, x.CDCB_COB_DED_AMT,
                    x.CDCB_COB_COPAY_AMT, x.CDCB_COB_COINS_AMT, x.CDCB_COB_REAS_CD
             FROM dbo.CMC_CDCB_LI_COB x
             WHERE x.CLCL_ID = d.CLCL_ID AND x.CDML_SEQ_NO = d.CDML_SEQ_NO
             ORDER BY x.CDCB_COB_TYPE) cb;
INSERT INTO #extract_step (step) VALUES (16);
COMMIT TRANSACTION;
GO

-- ### sdm.claims_diag
-- Every diagnosis of every claim in scope (HI segments).
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 16
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.claims_diag;
INSERT INTO sdm.claims_diag WITH (TABLOCK)
    (CLCL_ID, CLMD_TYPE, IDCD_ID, CLMD_POA_IND, IDCD_ID_SUB)
SELECT dg.CLCL_ID, dg.CLMD_TYPE, dg.IDCD_ID, dg.CLMD_POA_IND, dg.IDCD_ID_SUB
FROM dbo.CMC_CLMD_DIAG dg
JOIN #scope_claim k ON k.CLCL_ID = dg.CLCL_ID;
INSERT INTO #extract_step (step) VALUES (17);
COMMIT TRANSACTION;
GO

-- ### sdm.claims_icd_proc
-- Institutional claim-level procedures (837I HI*BBR / BBQ).
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 17
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.claims_icd_proc;
INSERT INTO sdm.claims_icd_proc WITH (TABLOCK)
    (CLCL_ID, CLHI_TYPE, IPCD_ID, CLHI_IP_DT)
SELECT hi.CLCL_ID, hi.CLHI_TYPE, hi.IPCD_ID, hi.CLHI_IP_DT
FROM dbo.CMC_CLHI_PROC hi
JOIN #scope_claim k ON k.CLCL_ID = hi.CLCL_ID;
INSERT INTO #extract_step (step) VALUES (18);
COMMIT TRANSACTION;
GO

-- ### sdm.claims_ub_code
-- UB condition (HI*BG), occurrence (HI*BH / BI) and value (HI*BE) codes.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 18
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.claims_ub_code;
INSERT INTO sdm.claims_ub_code WITH (TABLOCK)
    (CLCL_ID, CODE_KIND, CODE_SEQ, UB_CODE)
SELECT hc.CLCL_ID, 'COND', hc.CLHC_SEQ_NO, hc.CLHC_COND_CD
FROM dbo.CMC_CLHC_COND_CODE hc
JOIN #scope_claim k ON k.CLCL_ID = hc.CLCL_ID;

INSERT INTO sdm.claims_ub_code WITH (TABLOCK)
    (CLCL_ID, CODE_KIND, CODE_SEQ, UB_CODE, FROM_DT, TO_DT)
SELECT ho.CLCL_ID, 'OCC', ho.CLHO_SEQ_NO, ho.CLHO_OCC_CODE, ho.CLHO_OCC_FROM_DT, ho.CLHO_OCC_TO_DT
FROM dbo.CMC_CLHO_OCC_CODE ho
JOIN #scope_claim k ON k.CLCL_ID = ho.CLCL_ID;

INSERT INTO sdm.claims_ub_code WITH (TABLOCK)
    (CLCL_ID, CODE_KIND, CODE_SEQ, UB_CODE, VALUE_AMT, VALUE_UNITS)
SELECT vc.CLCL_ID, 'VAL',
       ROW_NUMBER() OVER (PARTITION BY vc.CLCL_ID ORDER BY vc.CLVC_NUMBER, vc.CLVC_LETTER),
       vc.CLVC_CODE, vc.CLVC_AMT, vc.CLVC_VALUE
FROM dbo.CMC_CLVC_VAL_CODE vc
JOIN #scope_claim k ON k.CLCL_ID = vc.CLCL_ID;
INSERT INTO #extract_step (step) VALUES (19);
COMMIT TRANSACTION;
GO

-- ### sdm.claims_auth
-- The authorized / referred service each line points to (REF*G1 / REF*9F),
-- so generated lines stay within the authorized dates, provider, POS, units.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 19
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.claims_auth;
INSERT INTO sdm.claims_auth WITH (TABLOCK)
    (CLCL_ID, CDML_SEQ_NO, AUTH_KIND, UMUM_REF_ID, UMSV_SEQ_NO, UMSV_FROM_DT, UMSV_TO_DT,
     UMSV_PRPR_ID_REQ, UMSV_PRPR_ID_SVC, UMSV_PRPR_ID_FAC, UMSV_PSCD_ID_AUTH, IPCD_ID, SESE_ID,
     UMSV_UNITS_AUTH, UMVT_STS, UMSV_AUTH_DT)
SELECT d.CLCL_ID, d.CDML_SEQ_NO, 'AUTH', u.UMUM_REF_ID, u.UMSV_SEQ_NO, u.UMSV_FROM_DT, u.UMSV_TO_DT,
       u.UMSV_PRPR_ID_REQ, u.UMSV_PRPR_ID_SVC, u.UMSV_PRPR_ID_FAC, u.UMSV_PSCD_ID_AUTH, u.IPCD_ID, u.SESE_ID,
       u.UMSV_UNITS_AUTH, u.UMVT_STS, u.UMSV_AUTH_DT
FROM dbo.CMC_CDML_CL_LINE d
JOIN #scope_claim k ON k.CLCL_ID = d.CLCL_ID
JOIN dbo.CMC_UMSV_SERVICES u ON u.UMUM_REF_ID = d.CDML_UMAUTH_ID AND u.UMSV_SEQ_NO = d.CDML_AUTHSV_SEQ_NO;

INSERT INTO sdm.claims_auth WITH (TABLOCK)
    (CLCL_ID, CDML_SEQ_NO, AUTH_KIND, UMUM_REF_ID, UMSV_SEQ_NO, UMSV_FROM_DT, UMSV_TO_DT,
     UMSV_PRPR_ID_REQ, UMSV_PRPR_ID_SVC, UMSV_PRPR_ID_FAC, UMSV_PSCD_ID_AUTH, IPCD_ID, SESE_ID,
     UMSV_UNITS_AUTH, UMVT_STS, UMSV_AUTH_DT)
SELECT d.CLCL_ID, d.CDML_SEQ_NO, 'REF', u.UMUM_REF_ID, u.UMSV_SEQ_NO, u.UMSV_FROM_DT, u.UMSV_TO_DT,
       u.UMSV_PRPR_ID_REQ, u.UMSV_PRPR_ID_SVC, u.UMSV_PRPR_ID_FAC, u.UMSV_PSCD_ID_AUTH, u.IPCD_ID, u.SESE_ID,
       u.UMSV_UNITS_AUTH, u.UMVT_STS, u.UMSV_AUTH_DT
FROM dbo.CMC_CDML_CL_LINE d
JOIN #scope_claim k ON k.CLCL_ID = d.CLCL_ID
JOIN dbo.CMC_UMSV_SERVICES u ON u.UMUM_REF_ID = d.CDML_UMREF_ID AND u.UMSV_SEQ_NO = d.CDML_REFSV_SEQ_NO;
INSERT INTO #extract_step (step) VALUES (20);
COMMIT TRANSACTION;
GO

-- ===========================================================================
-- 4. Provider
-- ===========================================================================
-- Providers in scope: every provider the claims and members reference, plus
-- the roster of the plans' networks (the pool generated claims draw from).
-- The roster part can be large; drop that UNION branch to keep only
-- providers already seen on claims and PCP assignments.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 20
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
IF OBJECT_ID('tempdb..#scope_prov') IS NOT NULL DROP TABLE #scope_prov;
CREATE TABLE #scope_prov (PRPR_ID char(12) NOT NULL PRIMARY KEY);
INSERT INTO #scope_prov (PRPR_ID)
SELECT x.PRPR_ID
FROM (
    SELECT PRPR_ID FROM sdm.claims_denorm
    UNION SELECT CLCL_PRPR_ID_REF FROM sdm.claims_denorm
    UNION SELECT CLCL_PRPR_ID_PCP FROM sdm.claims_denorm
    UNION SELECT CLCL_PAYEE_PR_ID FROM sdm.claims_header_ext_denorm
    UNION SELECT CLHP_PRPR_ID_ADM FROM sdm.claims_header_ext_denorm
    UNION SELECT CLHP_PRPR_ID_OPER FROM sdm.claims_header_ext_denorm
    UNION SELECT CLHP_PRPR_ID_OTH1 FROM sdm.claims_header_ext_denorm
    UNION SELECT CLMF_PRPR_ID_FAC FROM sdm.claims_header_ext_denorm
    UNION SELECT CLSD_PRPR_ID_REND_NVL FROM sdm.claims_header_ext_denorm
    UNION SELECT CDSD_PRPR_ID_REND FROM sdm.claims_detail_denorm
    UNION SELECT UMSV_PRPR_ID_REQ FROM sdm.claims_auth
    UNION SELECT UMSV_PRPR_ID_SVC FROM sdm.claims_auth
    UNION SELECT UMSV_PRPR_ID_FAC FROM sdm.claims_auth
    UNION SELECT PRPR_ID FROM sdm.member_pcp
    UNION SELECT nw.PRPR_ID
          FROM dbo.CMC_NWPR_RELATION nw
          JOIN sdm.plan_network pn ON pn.NWNW_ID = nw.NWNW_ID AND pn.NWPR_PFX = nw.NWPR_PFX
) x (PRPR_ID)
WHERE NULLIF(RTRIM(x.PRPR_ID), '') IS NOT NULL;
INSERT INTO #extract_step (step) VALUES (21);
COMMIT TRANSACTION;
GO

-- ### sdm.provider_relation
-- PRER: practitioner/facility -> group (G), IPA (I) or sponsor (S). For a
--   practitioner billing through a group, the group is 837P 2010AA and the
--   practitioner 2310B.
-- PRDS: claims payee (2010AB). The payee is the provider itself (P) or the
--   PRER_PRPR_ID of the relationship whose entity equals PRDS_PAY_CL_IND
--   (dictionary note), in force when the payee row starts.
-- PRAF: practitioner -> affiliated facility (through PRCR_ID), for 837I
--   attending / operating physicians at the billing facility.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 21
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.provider_relation;
INSERT INTO sdm.provider_relation WITH (TABLOCK)
    (PRPR_ID, REL_SOURCE, REL_ENTITY, REL_EFF_DT, REL_TERM_DT, REL_PRPR_ID,
     REL_PRPR_ENTITY, REL_PRPR_NAME, REL_PRPR_NPI, REL_PRPR_TAXONOMY_CD, REL_MCTN_ID)
SELECT r.PRPR_ID, 'PRER', r.PRER_PRPR_ENTITY, r.PRER_EFF_DT, r.PRER_TERM_DT, r.PRER_PRPR_ID,
       rp.PRPR_ENTITY, rp.PRPR_NAME, rp.PRPR_NPI, rp.PRPR_TAXONOMY_CD, rp.MCTN_ID
FROM dbo.CMC_PRER_RELATION r
JOIN #scope_prov k ON k.PRPR_ID = r.PRPR_ID
LEFT JOIN dbo.CMC_PRPR_PROV rp ON rp.PRPR_ID = r.PRER_PRPR_ID;

INSERT INTO sdm.provider_relation WITH (TABLOCK)
    (PRPR_ID, REL_SOURCE, REL_ENTITY, REL_EFF_DT, REL_TERM_DT, REL_PRPR_ID,
     REL_PRPR_ENTITY, REL_PRPR_NAME, REL_PRPR_NPI, REL_PRPR_TAXONOMY_CD, REL_MCTN_ID)
SELECT ds.PRPR_ID, 'PRDS', ds.PRDS_PAY_CL_IND, ds.PRDS_EFF_DT, ds.PRDS_TERM_DT, py.PAYEE_ID,
       rp.PRPR_ENTITY, rp.PRPR_NAME, rp.PRPR_NPI, rp.PRPR_TAXONOMY_CD, rp.MCTN_ID
FROM dbo.CMC_PRDS_DATE ds
JOIN #scope_prov k ON k.PRPR_ID = ds.PRPR_ID
OUTER APPLY (SELECT CASE WHEN ds.PRDS_PAY_CL_IND = 'P' THEN ds.PRPR_ID
                         ELSE (SELECT TOP 1 r.PRER_PRPR_ID
                               FROM dbo.CMC_PRER_RELATION r
                               WHERE r.PRPR_ID = ds.PRPR_ID
                                 AND r.PRER_PRPR_ENTITY = ds.PRDS_PAY_CL_IND
                                 AND r.PRER_EFF_DT <= ISNULL(ds.PRDS_TERM_DT, '99991231')
                                 AND ISNULL(r.PRER_TERM_DT, '99991231') >= ds.PRDS_EFF_DT
                               ORDER BY r.PRER_EFF_DT DESC) END AS PAYEE_ID) py
LEFT JOIN dbo.CMC_PRPR_PROV rp ON rp.PRPR_ID = py.PAYEE_ID;

INSERT INTO sdm.provider_relation WITH (TABLOCK)
    (PRPR_ID, REL_SOURCE, REL_ENTITY, REL_EFF_DT, REL_TERM_DT, REL_PRPR_ID,
     REL_PRPR_ENTITY, REL_PRPR_NAME, REL_PRPR_NPI, REL_PRPR_TAXONOMY_CD, REL_MCTN_ID, PRAF_PRIM_IND, PRAF_STS)
SELECT p.PRPR_ID, 'PRAF', 'F', af.PRAF_FROM_DT, af.PRAF_TO_DT, af.PRPR_ID,
       fp.PRPR_ENTITY, fp.PRPR_NAME, fp.PRPR_NPI, fp.PRPR_TAXONOMY_CD, fp.MCTN_ID, af.PRAF_PRIM_IND, af.PRAF_STS
FROM dbo.CMC_PRPR_PROV p
JOIN #scope_prov k ON k.PRPR_ID = p.PRPR_ID
JOIN dbo.CMC_PRAF_FAC_AFFIL af ON af.PRCR_ID = p.PRCR_ID
LEFT JOIN dbo.CMC_PRPR_PROV fp ON fp.PRPR_ID = af.PRPR_ID
WHERE p.PRPR_ENTITY <> 'F'
  AND NULLIF(RTRIM(p.PRCR_ID), '') IS NOT NULL;

-- Related providers (groups, IPAs, payees, facilities) join the scope so
-- their own name, NPI, tax ID and addresses are extracted too.
INSERT INTO #scope_prov (PRPR_ID)
SELECT DISTINCT r.REL_PRPR_ID
FROM sdm.provider_relation r
WHERE NULLIF(RTRIM(r.REL_PRPR_ID), '') IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM #scope_prov k WHERE k.PRPR_ID = r.REL_PRPR_ID);
INSERT INTO #extract_step (step) VALUES (22);
COMMIT TRANSACTION;
GO

-- ### sdm.provider_denorm
-- One row per provider. Person names come from the common practitioner
-- (PRPR_ENTITY = 'P'); facility name from PRFA. Tax ID type (EI / SY) from
-- MCTN; where one MCTN_ID has rows of both types, the one the provider's
-- CMC_MCTI_TAX_INFO row names wins.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 22
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.provider_denorm;
INSERT INTO sdm.provider_denorm WITH (TABLOCK)
    (PRPR_ID, PRPR_ENTITY, NM1_ENTITY_TYPE, PRPR_NAME, PRCP_ID, PRCP_LAST_NAME, PRCP_FIRST_NAME, PRCP_MID_INIT,
     PRCP_TITLE, PRFA_NAME, PRPR_NPI, PRPR_TAXONOMY_CD, PRPR_MCTR_TYPE, PRCF_MCTR_SPEC, PRCF_MCTR_SPEC2,
     PRPR_MCTR_PRTY, TPCT_MCTR_TCAT, PRAD_ID, PRAD_TYPE_PRIM, PRAD_TYPE_CHECK, MCTN_ID, MCTN_TYPE, MCTN_NAME,
     PRPR_STS, PRPR_TERM_DT, PRPR_PREAUTH_IND, PRCR_ID)
SELECT p.PRPR_ID, p.PRPR_ENTITY, CASE WHEN p.PRPR_ENTITY = 'P' THEN '1' ELSE '2' END, p.PRPR_NAME,
       p.PRCP_ID, cp.PRCP_LAST_NAME, cp.PRCP_FIRST_NAME, cp.PRCP_MID_INIT,
       cp.PRCP_TITLE, fa.PRFA_NAME, p.PRPR_NPI, p.PRPR_TAXONOMY_CD, p.PRPR_MCTR_TYPE, p.PRCF_MCTR_SPEC, p.PRCF_MCTR_SPEC2,
       p.PRPR_MCTR_PRTY, p.TPCT_MCTR_TCAT, p.PRAD_ID, p.PRAD_TYPE_PRIM, p.PRAD_TYPE_CHECK, p.MCTN_ID, tn.MCTN_TYPE, tn.MCTN_NAME,
       p.PRPR_STS, p.PRPR_TERM_DT, p.PRPR_PREAUTH_IND, p.PRCR_ID
FROM dbo.CMC_PRPR_PROV p
JOIN #scope_prov k ON k.PRPR_ID = p.PRPR_ID
LEFT JOIN dbo.CMC_PRCP_COMM_PRAC cp ON cp.PRCP_ID = p.PRCP_ID AND p.PRPR_ENTITY = 'P'
LEFT JOIN dbo.CMC_PRFA_FACILITY fa ON fa.PRPR_ID = p.PRPR_ID
OUTER APPLY (SELECT TOP 1 t.MCTN_TYPE, t.MCTN_NAME
             FROM dbo.CMC_MCTN_TAX_NAME t
             WHERE t.MCTN_ID = p.MCTN_ID
             ORDER BY CASE WHEN EXISTS (SELECT 1 FROM dbo.CMC_MCTI_TAX_INFO ti
                                        WHERE ti.MCTI_ENTY_ID = p.PRPR_ID
                                          AND ti.MCTN_ID = t.MCTN_ID AND ti.MCTN_TYPE = t.MCTN_TYPE)
                           THEN 0 ELSE 1 END,
                      t.MCTN_TYPE) tn;
INSERT INTO #extract_step (step) VALUES (23);
COMMIT TRANSACTION;
GO

-- ### sdm.provider_address
-- Every dated address of each provider's address owner (PRAD_ID), flagged as
-- primary practice (2010AA, 2310C) and remittance (2010AB; Facets defaults the
-- remit type to the primary). Address on svc_dt: PRAD_EFF_DT <= svc_dt AND
-- ISNULL(PRAD_TERM_DT, '99991231') >= svc_dt.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 23
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.provider_address;
INSERT INTO sdm.provider_address WITH (TABLOCK)
    (PRPR_ID, PRAD_ID, PRAD_TYPE, PRAD_EFF_DT, PRAD_TERM_DT, IS_PRIMARY, IS_REMIT, PRAD_PRACTICE_IND, PRAD_TYPE_MAIL,
     PRAD_ADDR1, PRAD_ADDR2, PRAD_CITY, PRAD_STATE, PRAD_ZIP, PRAD_COUNTY, PRAD_CTRY_CD, PRAD_PHONE)
SELECT p.PRPR_ID, a.PRAD_ID, a.PRAD_TYPE, a.PRAD_EFF_DT, a.PRAD_TERM_DT,
       CASE WHEN a.PRAD_TYPE = p.PRAD_TYPE_PRIM THEN 'Y' ELSE 'N' END,
       CASE WHEN a.PRAD_TYPE = COALESCE(NULLIF(RTRIM(p.PRAD_TYPE_CHECK), ''), p.PRAD_TYPE_PRIM) THEN 'Y' ELSE 'N' END,
       a.PRAD_PRACTICE_IND, a.PRAD_TYPE_MAIL,
       a.PRAD_ADDR1, a.PRAD_ADDR2, a.PRAD_CITY, a.PRAD_STATE, a.PRAD_ZIP, a.PRAD_COUNTY, a.PRAD_CTRY_CD, a.PRAD_PHONE
FROM dbo.CMC_PRPR_PROV p
JOIN #scope_prov k ON k.PRPR_ID = p.PRPR_ID
JOIN dbo.CMC_PRAD_ADDRESS a ON a.PRAD_ID = p.PRAD_ID;
INSERT INTO #extract_step (step) VALUES (24);
COMMIT TRANSACTION;
GO

-- ### sdm.provider_network
-- Network participation with the network and the agreement (contract). The
-- agreement row is the latest one overlapping the participation dates.
-- In network for a member on svc_dt: a row whose NWNW_ID + NWPR_PFX is in the
-- member plan's plan_network rows, with NWPR_EFF_DT <= svc_dt AND
-- ISNULL(NWPR_TERM_DT, '99991231') >= svc_dt.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 24
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
BEGIN TRANSACTION;
TRUNCATE TABLE sdm.provider_network;
INSERT INTO sdm.provider_network WITH (TABLOCK)
    (PRPR_ID, NWNW_ID, NWPR_PFX, NWPR_PCP_IND, NWPR_EFF_DT, NWPR_TERM_DT, AGAG_ID,
     NWPR_ACC_PAT_IND, NWPR_MIN_AGE, NWPR_MAX_AGE, NWPR_SEX, NWNW_NAME, NWNW_MCTR_TYPE,
     AGAG_EFF_DT, AGAG_TERM_DT, AGAG_MCTR_TYPE, AGAG_DESC)
SELECT nw.PRPR_ID, nw.NWNW_ID, nw.NWPR_PFX, nw.NWPR_PCP_IND, nw.NWPR_EFF_DT, nw.NWPR_TERM_DT, nw.AGAG_ID,
       nw.NWPR_ACC_PAT_IND, nw.NWPR_MIN_AGE, nw.NWPR_MAX_AGE, nw.NWPR_SEX, n.NWNW_NAME, n.NWNW_MCTR_TYPE,
       ag.AGAG_EFF_DT, ag.AGAG_TERM_DT, ag.AGAG_MCTR_TYPE, ag.AGAG_DESC
FROM dbo.CMC_NWPR_RELATION nw
JOIN #scope_prov k ON k.PRPR_ID = nw.PRPR_ID
LEFT JOIN dbo.CMC_NWNW_NETWORK n ON n.NWNW_ID = nw.NWNW_ID
OUTER APPLY (SELECT TOP 1 g.AGAG_EFF_DT, g.AGAG_TERM_DT, g.AGAG_MCTR_TYPE, g.AGAG_DESC
             FROM dbo.CMC_AGAG_AGREEMENT g
             WHERE g.AGAG_ID = nw.AGAG_ID
               AND g.AGAG_EFF_DT <= ISNULL(nw.NWPR_TERM_DT, '99991231')
               AND ISNULL(g.AGAG_TERM_DT, '99991231') >= nw.NWPR_EFF_DT
             ORDER BY g.AGAG_EFF_DT DESC) ag;
INSERT INTO #extract_step (step) VALUES (25);
COMMIT TRANSACTION;
GO

-- Row counts, for a quick check after the load.
IF ISNULL((SELECT MAX(step) FROM #extract_step), -1) <> 25
BEGIN RAISERROR(N'An earlier section of this extract failed (see the error above), so this section and every later one were not run. Fix the cause, reconnect and run the whole script again.', 16, 1); SET NOEXEC ON; RETURN; END;
SELECT 'member_elig' AS tbl, COUNT(*) AS n FROM sdm.member_elig
UNION ALL SELECT 'subscriber_denorm', COUNT(*) FROM sdm.subscriber_denorm
UNION ALL SELECT 'member_address', COUNT(*) FROM sdm.member_address
UNION ALL SELECT 'member_pcp', COUNT(*) FROM sdm.member_pcp
UNION ALL SELECT 'member_cob', COUNT(*) FROM sdm.member_cob
UNION ALL SELECT 'plan_denorm', COUNT(*) FROM sdm.plan_denorm
UNION ALL SELECT 'plan_network', COUNT(*) FROM sdm.plan_network
UNION ALL SELECT 'plan_benefit_denorm', COUNT(*) FROM sdm.plan_benefit_denorm
UNION ALL SELECT 'plan_accumulator', COUNT(*) FROM sdm.plan_accumulator
UNION ALL SELECT 'service_code_map', COUNT(*) FROM sdm.service_code_map
UNION ALL SELECT 'place_of_service', COUNT(*) FROM sdm.place_of_service
UNION ALL SELECT 'claims_header_ext_denorm', COUNT(*) FROM sdm.claims_header_ext_denorm
UNION ALL SELECT 'claims_detail_denorm', COUNT(*) FROM sdm.claims_detail_denorm
UNION ALL SELECT 'claims_diag', COUNT(*) FROM sdm.claims_diag
UNION ALL SELECT 'claims_icd_proc', COUNT(*) FROM sdm.claims_icd_proc
UNION ALL SELECT 'claims_ub_code', COUNT(*) FROM sdm.claims_ub_code
UNION ALL SELECT 'claims_auth', COUNT(*) FROM sdm.claims_auth
UNION ALL SELECT 'provider_relation', COUNT(*) FROM sdm.provider_relation
UNION ALL SELECT 'provider_denorm', COUNT(*) FROM sdm.provider_denorm
UNION ALL SELECT 'provider_address', COUNT(*) FROM sdm.provider_address
UNION ALL SELECT 'provider_network', COUNT(*) FROM sdm.provider_network;
GO
