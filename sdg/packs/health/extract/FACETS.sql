-- FACETS: fill the customer's SDM platform denorm tables from FACETS.
--
-- What it fills (schema sdm on the denorm connection):
--   sdm.member_denorm and sdm.claims_denorm (the customer's own tables), and
--   the 21 EDI 837 support tables of sdg/packs/health/sql/03_denorm_837_tables.sql:
--     Member   : member_elig, subscriber_denorm, member_address, member_pcp, member_cob
--     Plan     : plan_denorm, plan_network, plan_benefit_denorm, plan_accumulator,
--                service_code_map, place_of_service
--     Claims   : claims_header_ext_denorm, claims_detail_denorm, claims_diag,
--                claims_icd_proc, claims_ub_code, claims_auth
--     Provider : provider_relation, provider_denorm, provider_address, provider_network
--
-- Two servers, two connections. Every SELECT below runs on the SOURCE
-- connection (the FACETS server and database) and reads only FACETS tables
-- (dbo.CMC_*). The tool inserts the rows into the table of the same name on
-- the DENORM connection (a different server), matching by column name. So no
-- SELECT here names an sdm table, a temporary table or a linked server: each
-- block is one statement, and the scope is rebuilt inside each statement as
-- CTEs over FACETS.
--
-- How to run (from the repository root, with the source and denorm
-- connections configured):
--     .venv\Scripts\python.exe -m sdg.cli.main extract FACETS
-- One table only:   ... extract FACETS --table member_denorm
-- Each table is replaced in one transaction (delete, then insert); a failure
-- leaves that table as it was. The target tables must exist: the customer's
-- member_denorm / claims_denorm, plus 03_denorm_837_tables.sql.
--
-- SCOPE: CURRENT AND ACTIVE ONLY. "Today" is the date the extract runs
-- (CAST(GETDATE() AS date)). "In force today" means EFF_DT <= today AND
-- ISNULL(TERM_DT, '9999-12-31') >= today (term dates are inclusive; NULL is
-- open). Nothing expired, terminated or future is extracted.
--   members   members with an eligible span (MEPE_ELIG_IND = 'Y') in force
--             today in CMC_MEPE_PRCS_ELIG.
--   eligibility, PCP, COB
--             only the rows in force today, of those members.
--   claims    claims of those members with CLCL_LOW_SVC_DT in the last 24
--             months (on or after today minus 24 months) and at least one
--             line in CMC_CDML_CL_LINE (claims_denorm is one row per line).
--   plans     the class/plan rows (CMC_CSPI_CS_PLAN) in force today whose key
--             (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID) an in-scope eligibility
--             span names. Products: the PDPD_IDs of those plans and spans
--             that have a CMC_PDPD_PRODUCT row in force today. Network sets,
--             product components, service payment rows, service definitions
--             and payers: only rows in force today.
--   providers active providers only (ISNULL(PRPR_TERM_DT, '9999-12-31') >=
--             today) that a claim in scope (or its institutional /
--             supplemental / line data or linked authorization) names, that
--             are a PCP in force today of a member in scope, or that are on
--             the roster (CMC_NWPR_RELATION row in force today) of a network
--             in a network set in force today. Plus, for provider_denorm,
--             provider_address and provider_network, their active related
--             groups / IPAs (PRER in force today) and affiliated facilities
--             (PRAF in force today), one level. Relationships, payee rows,
--             addresses, network participation and agreements: only rows in
--             force today.
--   reference place_of_service is the whole CMC_PSCD_POS_DESC table.
--
-- ASSUMPTIONS TO CONFIRM AT THE CUSTOMER (code values are not in the FACETS
-- data dictionary; check them against the data before relying on the result):
--   1. MEPE_ELIG_IND = 'Y' means eligible.
--   2. member_denorm: one row per member and product category, the eligible
--      span in force today (the latest-starting one if two overlap); the plan
--      (CSPI), product (PDPD) and subscriber subgroup (SBSG) rows are those in
--      force today.
--   3. CMC_CLMD_DIAG.CLMD_TYPE '01' is the primary diagnosis and 'AD' the
--      admitting one. Check with
--          SELECT CLMD_TYPE, COUNT(*) FROM dbo.CMC_CLMD_DIAG GROUP BY CLMD_TYPE;
--      When a claim has no '01' row, the line's own diagnosis (CDML IDCD_ID)
--      is used as IDCD_ID_PRIMARY.
--   4. claims_denorm takes every claim status (CLCL_CUR_STS), including
--      adjusted-away originals. Add a status filter if the customer's table
--      held only final claims.
--   5. member_denorm.CSCS_ID is the subgroup's class (CMC_SGSG_SUB_GROUP), as in
--      the demo view; the member's eligibility class is in sdm.member_elig.
--   6. Race and ethnicity come from the master member record
--      (CMC_MALK_LINK -> CMC_MAME_MASTER.MAME_MCTR_RACE / MAME_MCTR_ETHN):
--      the dictionary has no MEME_MCTR_RACE_NVL / MEME_MCTR_ETHN_NVL on
--      CMC_MEME_MEMBER. If the customer's FACETS release has them, use those.
--   7. CSPI_HIOS_ID_NVL is not in the dictionary for CMC_CSPI_CS_PLAN and is
--      left NULL. Fill it from the customer's release if the column exists.
--   8. member_denorm.EMAIL is withheld (NULL): see IDENTIFYING FIELDS below.
--   9. From 04 / docs/facets-837-sources.md: PDBC_TYPE = 'SEDF' marks the
--      service-definition component; IPCD_ID positions 1-5 are the procedure
--      code and 6-7 the first modifier; PRPR_ENTITY 'P' is a practitioner and
--      'F' a facility; PRDS_PAY_CL_IND 'P' means the provider pays itself.
--  10. An active provider is one whose PRPR_TERM_DT is empty or not yet
--      passed. If the customer also marks inactive providers in PRPR_STS,
--      add that code to the scope_prov CTEs.
--  11. CMC_CSPI_CS_PLAN.CSPI_PDPD_BEG_MMDD is in the dictionary but not in
--      every release; plan_denorm leaves it NULL. Likewise CLCB_COB_REAS_CD
--      (claims_header_ext_denorm) and CDCB_COB_REAS_CD (claims_detail_denorm).
--
-- IDENTIFYING FIELDS ARE WITHHELD. Patterns need shapes, not identities, so
-- this extract writes NULL for: member and subscriber names and middle
-- initials, SSN (MEME_SSN), Medicaid number (MEME_MEDCD_NO), HICN, employee
-- ID (SBSB_EMPLOY_ID), email, street address lines, phone numbers, and the
-- other coverage's policy ID and policyholder's name and ID (member_cob).
-- A file written from a pattern draws a generated value for each of these
-- (pack.json files.person_elements, files.head_names).
-- SUBSCRIBER ID AS A SURROGATE. SBSB_ID (the ID on a member's card; some
-- installations derive it from the SSN) is read here but written to the
-- denorm database as a 9-character surrogate: an HMAC-SHA256 of the ID keyed
-- with SDG_EXTRACT_KEY (pack.json extract.surrogates). The same ID gets the
-- same surrogate in every table and every refresh under one key, so
-- households and member IDs stay consistent. Keep the key private and do not
-- change it between refreshes.
-- KEPT, as patterns read them: birth date (ages, age bands and the DOB a
-- pattern generates; user decision 2026-11-12) and the home ZIP, city, state
-- and county (a pattern's geography). Provider, practitioner, payer and
-- carrier names and addresses are organizations' and stay. Hold the denorm
-- database to the access controls a birth date with a ZIP code needs.


-- table: member_denorm
-- Grain: one row per member and product category (MEME_CK, CSPD_CAT) with an
-- eligible span in force today. A member with medical and dental coverage has
-- two rows. Group, class/plan and product come from that span; the dated
-- CSPI, PDPD and SBSG rows are the ones in force today.
WITH cur AS (
    SELECT e.MEME_CK, e.CSPD_CAT, e.GRGR_CK, e.SGSG_CK, e.CSCS_ID, e.CSPI_ID, e.PDPD_ID,
           ROW_NUMBER() OVER (PARTITION BY e.MEME_CK, e.CSPD_CAT
                              ORDER BY e.MEPE_EFF_DT DESC) AS RN
    FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
-- The class/plan row in force today.
plan_row AS (
    SELECT c.MEME_CK, c.CSPD_CAT, p.CSPI_EFF_DT, p.CSPI_TERM_DT, p.CSPI_SEL_IND,
           ROW_NUMBER() OVER (PARTITION BY c.MEME_CK, c.CSPD_CAT
                              ORDER BY p.CSPI_EFF_DT DESC) AS RN
    FROM cur AS c
    JOIN dbo.CMC_CSPI_CS_PLAN AS p
      ON p.GRGR_CK = c.GRGR_CK AND p.CSCS_ID = c.CSCS_ID
     AND p.CSPD_CAT = c.CSPD_CAT AND p.CSPI_ID = c.CSPI_ID
    WHERE c.RN = 1
      AND p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
-- The product row (line of business, risk, category) in force today.
product AS (
    SELECT c.MEME_CK, c.CSPD_CAT, x.LOBD_ID, x.PDPD_RISK_IND, x.PDPD_MCTR_CCAT,
           ROW_NUMBER() OVER (PARTITION BY c.MEME_CK, c.CSPD_CAT
                              ORDER BY x.PDPD_EFF_DT DESC) AS RN
    FROM cur AS c
    JOIN dbo.CMC_PDPD_PRODUCT AS x ON x.PDPD_ID = c.PDPD_ID
    WHERE c.RN = 1
      AND x.PDPD_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(x.PDPD_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
-- The subscriber's subgroup relationship in force today (the one naming the
-- span's subgroup first).
sbsg AS (
    SELECT c.MEME_CK, c.CSPD_CAT, r.SGSG_CK, r.SBSG_EFF_DT, r.SBSG_TERM_DT, r.SBSG_MCTR_TRSN,
           ROW_NUMBER() OVER (PARTITION BY c.MEME_CK, c.CSPD_CAT
                              ORDER BY CASE WHEN r.SGSG_CK = c.SGSG_CK THEN 0 ELSE 1 END,
                                       r.SBSG_EFF_DT DESC) AS RN
    FROM cur AS c
    JOIN dbo.CMC_MEME_MEMBER AS m ON m.MEME_CK = c.MEME_CK
    JOIN dbo.CMC_SBSG_RELATION AS r ON r.SBSB_CK = m.SBSB_CK
    WHERE c.RN = 1
      AND r.SBSG_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(r.SBSG_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
-- Race and ethnicity live on the master member record (assumption 6). A
-- member links to one master; the most recent link wins if there are more.
master AS (
    SELECT k.MEME_CK, mm.MAME_MCTR_RACE, mm.MAME_MCTR_ETHN,
           ROW_NUMBER() OVER (PARTITION BY k.MEME_CK
                              ORDER BY k.MALK_CREATE_DTM DESC, k.MAME_CK DESC) AS RN
    FROM dbo.CMC_MALK_LINK AS k
    JOIN dbo.CMC_MAME_MASTER AS mm ON mm.MAME_CK = k.MAME_CK
)
SELECT
    c.MEME_CK                                               AS MEME_CK,
    m.SBSB_CK                                               AS SBSB_CK,
    c.GRGR_CK                                               AS GRGR_CK,
    CAST(m.MEME_SFX AS int)                                 AS MEME_SFX,
    m.MEME_REL                                              AS MEME_REL,
    CAST(NULL AS nvarchar(100))                             AS MEME_LAST_NAME,   -- withheld
    CAST(NULL AS nvarchar(100))                             AS MEME_FIRST_NAME,  -- withheld
    CAST(NULL AS char(1))                                   AS MEME_MID_INIT,    -- withheld
    m.MEME_SEX                                              AS MEME_SEX,
    CAST(m.MEME_BIRTH_DT AS date)                           AS MEME_BIRTH_DT,
    CAST(NULL AS bigint)                                    AS MEME_SSN,         -- withheld
    CAST(RTRIM(m.MEME_MCTR_STS) AS nvarchar(10))            AS MEME_MCTR_STS,
    CAST(m.MEME_ORIG_EFF_DT AS date)                        AS MEME_ORIG_EFF_DT,
    m.MEME_MARITAL_STATUS                                   AS MEME_MARITAL_STATUS,
    CAST(NULL AS nvarchar(50))                              AS MEME_MEDCD_NO,    -- withheld
    CAST(NULL AS nvarchar(50))                              AS MEME_HICN,        -- withheld
    CAST(RTRIM(ms.MAME_MCTR_RACE) AS nvarchar(50))          AS MEME_MCTR_RACE_NVL,
    CAST(RTRIM(ms.MAME_MCTR_ETHN) AS nvarchar(50))          AS MEME_MCTR_ETHN_NVL,
    CAST(RTRIM(s.SBSB_ID) AS nvarchar(50))                  AS SBSB_ID,
    CAST(NULL AS nvarchar(100))                             AS SBSB_LAST_NAME,   -- withheld
    CAST(NULL AS nvarchar(100))                             AS SBSB_FIRST_NAME,  -- withheld
    CAST(s.SBSB_ORIG_EFF_DT AS date)                        AS SBSB_ORIG_EFF_DT,
    CAST(RTRIM(s.SBSB_MCTR_STS) AS nvarchar(10))            AS SBSB_MCTR_STS,
    CAST(NULL AS nvarchar(50))                              AS SBSB_EMPLOY_ID,   -- withheld
    COALESCE(sr.SGSG_CK, c.SGSG_CK)                         AS SGSG_CK,
    CAST(sr.SBSG_EFF_DT AS date)                            AS SBSG_EFF_DT,
    CAST(sr.SBSG_TERM_DT AS date)                           AS SBSG_TERM_DT,
    CAST(RTRIM(sr.SBSG_MCTR_TRSN) AS nvarchar(10))          AS SBSG_MCTR_TRSN,
    CAST(RTRIM(sg.SGSG_ID) AS nvarchar(50))                 AS SGSG_ID,
    CAST(RTRIM(sg.SGSG_NAME) AS nvarchar(200))              AS SGSG_NAME,
    CAST(RTRIM(sg.CSCS_ID) AS nvarchar(50))                 AS CSCS_ID,
    sg.SGSG_STATE                                           AS SGSG_STATE,
    CAST(RTRIM(sg.SGSG_STS) AS nvarchar(10))                AS SGSG_STS,
    CAST(sg.SGSG_ORIG_EFF_DT AS date)                       AS SGSG_ORIG_EFF_DT,
    CAST(sg.SGSG_TERM_DT AS date)                           AS SGSG_TERM_DT,
    CAST(RTRIM(g.GRGR_ID) AS nvarchar(50))                  AS GRGR_ID,
    CAST(RTRIM(g.GRGR_NAME) AS nvarchar(200))               AS GRGR_NAME,
    g.GRGR_STATE                                            AS GRGR_STATE,
    CAST(RTRIM(g.GRGR_COUNTY) AS nvarchar(100))             AS GRGR_COUNTY,
    CAST(RTRIM(g.GRGR_STS) AS nvarchar(10))                 AS GRGR_STS,
    CAST(g.GRGR_ORIG_EFF_DT AS date)                        AS GRGR_ORIG_EFF_DT,
    CAST(g.GRGR_TERM_DT AS date)                            AS GRGR_TERM_DT,
    CAST(RTRIM(g.GRGR_MCTR_TYPE) AS nvarchar(10))           AS GRGR_MCTR_TYPE,
    g.PAGR_CK                                               AS PAGR_CK,
    CAST(RTRIM(c.CSPI_ID) AS nvarchar(50))                  AS CSPI_ID,
    c.CSPD_CAT                                              AS CSPD_CAT,
    CAST(pr.CSPI_EFF_DT AS date)                            AS CSPI_EFF_DT,
    CAST(pr.CSPI_TERM_DT AS date)                           AS CSPI_TERM_DT,
    CAST(RTRIM(c.PDPD_ID) AS nvarchar(50))                  AS PDPD_ID,
    CAST(RTRIM(pr.CSPI_SEL_IND) AS nvarchar(10))            AS CSPI_SEL_IND,
    CAST(NULL AS nvarchar(50))                              AS CSPI_HIOS_ID_NVL,  -- assumption 7
    CAST(RTRIM(cd.CSPD_CAT_DESC) AS nvarchar(200))          AS CSPD_CAT_DESC,
    CAST(RTRIM(cd.CSPD_TYPE) AS nvarchar(50))               AS CSPD_TYPE,
    CAST(RTRIM(pd.LOBD_ID) AS nvarchar(50))                 AS LOBD_ID,
    CAST(RTRIM(pd.PDPD_RISK_IND) AS nvarchar(10))           AS PDPD_RISK_IND,
    CAST(RTRIM(pd.PDPD_MCTR_CCAT) AS nvarchar(50))          AS PDPD_MCTR_CCAT,
    CAST(RTRIM(pl.PLDS_DESC) AS nvarchar(200))              AS PLDS_DESC,
    CAST(RTRIM(ds.PDDS_DESC) AS nvarchar(200))              AS PDDS_DESC,
    CAST(NULL AS varchar(200))                              AS EMAIL             -- withheld
FROM cur AS c
JOIN dbo.CMC_MEME_MEMBER AS m ON m.MEME_CK = c.MEME_CK
JOIN dbo.CMC_SBSB_SUBSC AS s ON s.SBSB_CK = m.SBSB_CK
LEFT JOIN dbo.CMC_GRGR_GROUP AS g ON g.GRGR_CK = c.GRGR_CK
LEFT JOIN sbsg AS sr ON sr.MEME_CK = c.MEME_CK AND sr.CSPD_CAT = c.CSPD_CAT AND sr.RN = 1
LEFT JOIN dbo.CMC_SGSG_SUB_GROUP AS sg ON sg.SGSG_CK = COALESCE(sr.SGSG_CK, c.SGSG_CK)
LEFT JOIN plan_row AS pr ON pr.MEME_CK = c.MEME_CK AND pr.CSPD_CAT = c.CSPD_CAT AND pr.RN = 1
LEFT JOIN product AS pd ON pd.MEME_CK = c.MEME_CK AND pd.CSPD_CAT = c.CSPD_CAT AND pd.RN = 1
LEFT JOIN dbo.CMC_CSPD_DESC AS cd ON cd.CSPD_CAT = c.CSPD_CAT
LEFT JOIN dbo.CMC_PLDS_PLAN_DESC AS pl ON pl.CSPI_ID = c.CSPI_ID
LEFT JOIN dbo.CMC_PDDS_PROD_DESC AS ds ON ds.PDPD_ID = c.PDPD_ID
LEFT JOIN master AS ms ON ms.MEME_CK = c.MEME_CK AND ms.RN = 1
WHERE c.RN = 1;


-- table: claims_denorm
-- Grain: one row per claim line (CLCL_ID, CDML_SEQ_NO) of the claims in scope:
-- members with an eligible span in force today, first service date in the
-- last 24 months, every claim status (assumption 4). Header fields repeat on
-- each line and are the claim's own snapshot (member sex, relationship, age,
-- group, plan, product type as FACETS recorded them on CMC_CLCL_CLAIM), not
-- today's member record. The provider is the claim's servicing provider
-- (CLCL.PRPR_ID); its name and NPI come from CMC_PRPR_PROV, its specialty from
-- the line as adjudicated, else the provider's. Diagnoses come from
-- CMC_CLMD_DIAG (assumption 3).
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
),
dx AS (
    SELECT d.CLCL_ID,
           MAX(CASE WHEN d.CLMD_TYPE = '01' THEN d.IDCD_ID END) AS IDCD_ID_PRIMARY,
           MAX(CASE WHEN d.CLMD_TYPE = 'AD' THEN d.IDCD_ID END) AS IDCD_ID_ADMIT,
           COUNT(NULLIF(RTRIM(d.IDCD_ID), ''))                  AS DIAG_COUNT
    FROM dbo.CMC_CLMD_DIAG AS d
    JOIN scope_claim AS k ON k.CLCL_ID = d.CLCL_ID
    GROUP BY d.CLCL_ID
),
-- One description per diagnosis code: the most recently effective row (a code
-- can have a row per code set, IDCD_TYPE).
dx_code AS (
    SELECT i.IDCD_ID, i.IDCD_DESC,
           ROW_NUMBER() OVER (PARTITION BY i.IDCD_ID
                              ORDER BY i.IDCD_EFF_DT DESC, i.IDCD_TYPE DESC) AS RN
    FROM dbo.CMC_IDCD_DIAG_CD AS i
)
SELECT
    l.CLCL_ID                                               AS CLCL_ID,
    l.CDML_SEQ_NO                                           AS CDML_SEQ_NO,
    c.CLCL_CL_TYPE                                          AS CLCL_CL_TYPE,
    c.CLCL_CL_SUB_TYPE                                      AS CLCL_CL_SUB_TYPE,
    c.CLCL_CUR_STS                                          AS CLCL_CUR_STS,
    c.CLST_MCTR_REAS                                        AS CLST_MCTR_REAS,
    c.CLCL_NTWK_IND                                         AS CLCL_NTWK_IND,
    c.CLCL_INPUT_DT                                         AS CLCL_INPUT_DT,
    c.CLCL_RECD_DT                                          AS CLCL_RECD_DT,
    c.CLCL_PAID_DT                                          AS CLCL_PAID_DT,
    c.CLCL_LOW_SVC_DT                                       AS CLCL_LOW_SVC_DT,
    c.CLCL_HIGH_SVC_DT                                      AS CLCL_HIGH_SVC_DT,
    c.CLCL_TOT_CHG                                          AS CLCL_TOT_CHG,
    c.CLCL_TOT_PAYABLE                                      AS CLCL_TOT_PAYABLE,
    c.MEME_CK                                               AS MEME_CK,
    c.SBSB_CK                                               AS SBSB_CK,
    c.CLCL_ME_AGE                                           AS CLCL_ME_AGE,
    c.MEME_SEX                                              AS MEME_SEX,
    c.MEME_REL                                              AS MEME_REL,
    c.MEPE_FI                                               AS MEPE_FI,
    c.GRGR_CK                                               AS GRGR_CK,
    CAST(RTRIM(g.GRGR_NAME) AS varchar(200))                AS GRGR_NAME,
    c.SGSG_CK                                               AS SGSG_CK,
    c.CSPD_CAT                                              AS CSPD_CAT,
    c.CSPI_ID                                               AS CSPI_ID,
    CAST(RTRIM(pl.PLDS_DESC) AS varchar(70))                AS PLDS_DESC,
    c.PDPD_ID                                               AS PDPD_ID,
    c.PDDS_PROD_TYPE                                        AS PDDS_PROD_TYPE,
    CAST(COALESCE(dx.IDCD_ID_PRIMARY, l.IDCD_ID) AS varchar(10)) AS IDCD_ID_PRIMARY,
    CAST(LEFT(RTRIM(dp.IDCD_DESC), 70) AS varchar(70))      AS IDCD_DESC_PRIMARY,
    CAST(dx.IDCD_ID_ADMIT AS varchar(10))                   AS IDCD_ID_ADMIT,
    CAST(ISNULL(dx.DIAG_COUNT, 0) AS smallint)              AS CLMD_DIAG_COUNT,
    l.IPCD_ID                                               AS IPCD_ID,
    CAST(LEFT(RTRIM(ip.IPCD_DESC), 70) AS varchar(70))      AS IPCD_DESC,
    CAST(ip.IPCD_TYPE AS char(2))                           AS IPCD_TYPE,
    l.CDML_UNITS                                            AS CDML_UNITS,
    l.CDML_UNITS_ALLOW                                      AS CDML_UNITS_ALLOW,
    CAST(l.PSCD_ID AS char(4))                              AS PSCD_ID,
    l.RCRC_ID                                               AS RCRC_ID,
    l.LOBD_ID                                               AS LOBD_ID,
    c.PRPR_ID                                               AS PRPR_ID,
    CAST(RTRIM(pv.PRPR_NAME) AS varchar(60))                AS PRPR_NAME,
    CAST(NULLIF(RTRIM(pv.PRPR_NPI), '') AS varchar(10))     AS PRPR_NPI,
    CAST(COALESCE(NULLIF(RTRIM(l.PRCF_MCTR_SPEC), ''), pv.PRCF_MCTR_SPEC) AS char(4)) AS PRPR_MCTR_SPEC,
    c.NWNW_ID                                               AS NWNW_ID,
    l.CDML_CHG_AMT                                          AS CDML_CHG_AMT,
    l.CDML_ALLOW                                            AS CDML_ALLOW,
    l.CDML_PAID_AMT                                         AS CDML_PAID_AMT,
    l.CDML_DED_AMT                                          AS CDML_DED_AMT,
    l.CDML_COPAY_AMT                                        AS CDML_COPAY_AMT,
    l.CDML_COINS_AMT                                        AS CDML_COINS_AMT,
    l.CDML_DISALL_AMT                                       AS CDML_DISALL_AMT,
    l.CDML_DISC_AMT                                         AS CDML_DISC_AMT,
    l.CDML_TOT_PA_LIAB                                      AS CDML_TOT_PA_LIAB,
    l.CDML_FROM_DT                                          AS CDML_FROM_DT,
    l.CDML_TO_DT                                            AS CDML_TO_DT,
    l.CDML_CUR_STS                                          AS CDML_CUR_STS,
    c.CLCL_EXT_AUTH_NO                                      AS CLCL_EXT_AUTH_NO,
    l.CDML_PRE_AUTH_IND                                     AS CDML_PRE_AUTH_IND,
    c.CLCL_PRPR_ID_REF                                      AS CLCL_PRPR_ID_REF,
    c.CLCL_PRPR_ID_PCP                                      AS CLCL_PRPR_ID_PCP,
    c.CLCL_PCP_IND                                          AS CLCL_PCP_IND
FROM dbo.CMC_CDML_CL_LINE AS l
JOIN scope_claim AS k ON k.CLCL_ID = l.CLCL_ID
JOIN dbo.CMC_CLCL_CLAIM AS c ON c.CLCL_ID = l.CLCL_ID
LEFT JOIN dx ON dx.CLCL_ID = l.CLCL_ID
LEFT JOIN dx_code AS dp ON dp.IDCD_ID = COALESCE(dx.IDCD_ID_PRIMARY, l.IDCD_ID) AND dp.RN = 1
LEFT JOIN dbo.CMC_IPCD_PROC_CD AS ip ON ip.IPCD_ID = l.IPCD_ID
LEFT JOIN dbo.CMC_GRGR_GROUP AS g ON g.GRGR_CK = c.GRGR_CK
LEFT JOIN dbo.CMC_PLDS_PLAN_DESC AS pl ON pl.CSPI_ID = c.CSPI_ID
LEFT JOIN dbo.CMC_PRPR_PROV AS pv ON pv.PRPR_ID = c.PRPR_ID;


-- ===========================================================================
-- Member (837 support): members with an eligible span in force today
-- ===========================================================================

-- table: member_elig
-- The eligible spans in force today (MEPE_ELIG_IND = 'Y'). Carries CSCS_ID,
-- the key member_denorm lacks for CMC_CSPI_CS_PLAN / CMC_CSCS_CLASS.
SELECT e.MEME_CK, e.CSPD_CAT, e.MEPE_EFF_DT, e.MEPE_TERM_DT, e.MEPE_ELIG_IND,
       e.GRGR_CK, e.SGSG_CK, e.CSCS_ID, e.CSPI_ID, e.PDPD_ID, e.MEPE_FI,
       e.MEPE_PLAN_ENTRY_DT
FROM dbo.CMC_MEPE_PRCS_ELIG AS e
WHERE e.MEPE_ELIG_IND = 'Y'
  AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
  AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date);


-- table: subscriber_denorm
-- 2000B / 2010BA: the subscriber's name, group number (SBR03) and home
-- address, for the subscribers of the members in scope. CMC_SBAD_ADDR has no
-- dates, so this is the current address.
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_subscriber AS (
    SELECT DISTINCT m.SBSB_CK
    FROM dbo.CMC_MEME_MEMBER AS m
    JOIN scope_member AS k ON k.MEME_CK = m.MEME_CK
)
SELECT sb.SBSB_CK, sb.GRGR_CK, g.GRGR_ID, sb.SBSB_ID,
       CAST(NULL AS varchar(35)) AS SBSB_LAST_NAME,      -- withheld
       CAST(NULL AS varchar(15)) AS SBSB_FIRST_NAME,     -- withheld
       CAST(NULL AS char(1))     AS SBSB_MID_INIT,       -- withheld
       sb.SBAD_TYPE_HOME,
       CAST(NULL AS varchar(40)) AS SBAD_ADDR1,          -- withheld
       CAST(NULL AS varchar(40)) AS SBAD_ADDR2,          -- withheld
       a.SBAD_CITY, a.SBAD_STATE, a.SBAD_ZIP,
       a.SBAD_COUNTY, a.SBAD_CTRY_CD
FROM dbo.CMC_SBSB_SUBSC AS sb
JOIN scope_subscriber AS k ON k.SBSB_CK = sb.SBSB_CK
LEFT JOIN dbo.CMC_GRGR_GROUP AS g ON g.GRGR_CK = sb.GRGR_CK
LEFT JOIN dbo.CMC_SBAD_ADDR AS a ON a.SBSB_CK = sb.SBSB_CK AND a.SBAD_TYPE = sb.SBAD_TYPE_HOME;


-- table: member_address
-- 2010CA patient address (2010BA when the member is the subscriber), one row
-- per member and role (HOME, MAIL). FACETS rule from the dictionary: a member
-- with no home or mailing address type of its own uses the subscriber's home
-- address; FROM_SUBSCRIBER says when that happened. Current address (no dates).
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
)
SELECT m.MEME_CK,
       CAST(r.ADDR_ROLE AS char(4))       AS ADDR_ROLE,
       m.SBSB_CK,
       a.SBAD_TYPE,
       CAST(r.FROM_SUBSCRIBER AS char(1)) AS FROM_SUBSCRIBER,
       CAST(NULL AS varchar(40)) AS SBAD_ADDR1,          -- withheld
       CAST(NULL AS varchar(40)) AS SBAD_ADDR2,          -- withheld
       a.SBAD_CITY, a.SBAD_STATE, a.SBAD_ZIP,
       a.SBAD_COUNTY, a.SBAD_CTRY_CD,
       CAST(NULL AS varchar(20)) AS SBAD_PHONE           -- withheld
FROM dbo.CMC_MEME_MEMBER AS m
JOIN scope_member AS k ON k.MEME_CK = m.MEME_CK
JOIN dbo.CMC_SBSB_SUBSC AS sb ON sb.SBSB_CK = m.SBSB_CK
CROSS APPLY (VALUES
    ('HOME', COALESCE(NULLIF(RTRIM(m.SBAD_TYPE_HOME), ''), sb.SBAD_TYPE_HOME),
             CASE WHEN NULLIF(RTRIM(m.SBAD_TYPE_HOME), '') IS NULL THEN 'Y' ELSE 'N' END),
    ('MAIL', COALESCE(NULLIF(RTRIM(m.SBAD_TYPE_MAIL), ''), sb.SBAD_TYPE_HOME),
             CASE WHEN NULLIF(RTRIM(m.SBAD_TYPE_MAIL), '') IS NULL THEN 'Y' ELSE 'N' END)
) AS r (ADDR_ROLE, ADDR_TYPE, FROM_SUBSCRIBER)
JOIN dbo.CMC_SBAD_ADDR AS a ON a.SBSB_CK = m.SBSB_CK AND a.SBAD_TYPE = r.ADDR_TYPE;


-- table: member_pcp
-- The PCP assignments in force today of the members in scope, with the PCP's
-- NPI and entity.
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
)
SELECT pc.MEME_CK, pc.MEPR_PCP_TYPE, pc.MEPR_EFF_DT, pc.MEPR_TERM_DT, pc.GRGR_CK,
       pc.PRPR_ID, p.PRPR_NPI, p.PRPR_ENTITY
FROM dbo.CMC_MEPR_PRIM_PROV AS pc
JOIN scope_member AS k ON k.MEME_CK = pc.MEME_CK
LEFT JOIN dbo.CMC_PRPR_PROV AS p ON p.PRPR_ID = pc.PRPR_ID
WHERE pc.MEPR_EFF_DT <= CAST(GETDATE() AS date)
  AND ISNULL(pc.MEPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date);


-- table: member_cob
-- Other coverage in force today of the members in scope (2000B SBR01, 2320,
-- 2330A, 2330B) with the other carrier's name and address.
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
)
SELECT cb.MEME_CK, cb.MECB_INSUR_TYPE, cb.MECB_INSUR_ORDER, cb.MECB_MCTR_STYP,
       cb.MECB_EFF_DT, cb.MECB_TERM_DT,
       CAST(NULL AS varchar(30)) AS MECB_POLICY_ID,      -- withheld
       CAST(NULL AS varchar(35)) AS MECB_PRIM_LAST_NM,   -- withheld
       CAST(NULL AS varchar(15)) AS MECB_PRIM_FIRST_NM,  -- withheld
       CAST(NULL AS varchar(30)) AS MECB_PRIM_ID,        -- withheld
       cb.MECB_MCTR_MSP,
       cb.MCRE_ID, re.MCRE_NAME, re.MCRE_ADDR1, re.MCRE_ADDR2, re.MCRE_CITY,
       re.MCRE_STATE, re.MCRE_ZIP
FROM dbo.CMC_MECB_COB AS cb
JOIN scope_member AS k ON k.MEME_CK = cb.MEME_CK
LEFT JOIN dbo.CMC_MCRE_RELAT_ENT AS re ON re.MCRE_ID = cb.MCRE_ID
WHERE cb.MECB_EFF_DT <= CAST(GETDATE() AS date)
  AND ISNULL(cb.MECB_TERM_DT, '99991231') >= CAST(GETDATE() AS date);


-- ===========================================================================
-- Plan and benefit (837 support): current plans and products only
--
-- scope_plan is the same CTE in each block: the class/plan rows in force
-- today whose key an eligible span in force today names.
-- ===========================================================================

-- table: plan_denorm
-- The class/plan rows in force today, with the product, network set
-- (NWST_PFX), service conversion prefix (TPCT_PFX), line of business and payer
-- (2010BB). The product row and the payer row are the ones in force today.
WITH scope_plan AS (
    SELECT p.GRGR_CK, p.CSCS_ID, p.CSPD_CAT, p.CSPI_ID, p.CSPI_EFF_DT, p.CSPI_TERM_DT,
           p.PDPD_ID, p.NWST_PFX,
           -- Not in every FACETS release (assumption 11); nothing reads it.
           CAST(NULL AS smallint) AS CSPI_PDPD_BEG_MMDD
    FROM dbo.CMC_CSPI_CS_PLAN AS p
    WHERE p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND EXISTS (SELECT 1 FROM dbo.CMC_MEPE_PRCS_ELIG AS e
                  WHERE e.GRGR_CK = p.GRGR_CK AND e.CSCS_ID = p.CSCS_ID
                    AND e.CSPD_CAT = p.CSPD_CAT AND e.CSPI_ID = p.CSPI_ID
                    AND e.MEPE_ELIG_IND = 'Y'
                    AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
product AS (
    SELECT r.GRGR_CK, r.CSCS_ID, r.CSPD_CAT, r.CSPI_ID, r.CSPI_EFF_DT, x.LOBD_ID,
           ROW_NUMBER() OVER (
               PARTITION BY r.GRGR_CK, r.CSCS_ID, r.CSPD_CAT, r.CSPI_ID, r.CSPI_EFF_DT
               ORDER BY x.PDPD_EFF_DT DESC) AS RN
    FROM scope_plan AS r
    JOIN dbo.CMC_PDPD_PRODUCT AS x
      ON x.PDPD_ID = r.PDPD_ID
     AND x.PDPD_EFF_DT <= CAST(GETDATE() AS date)
     AND ISNULL(x.PDPD_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
payor AS (
    SELECT pd.GRGR_CK, pd.CSCS_ID, pd.CSPD_CAT, pd.CSPI_ID, pd.CSPI_EFF_DT,
           y.PYPY_PAYOR_NAME, y.PYPY_NATL_PLAN_ID, y.PYPY_NAIC_CODE, y.PYPY_TAX_ID,
           y.PYPY_ADDR1, y.PYPY_ADDR2, y.PYPY_CITY, y.PYPY_STATE, y.PYPY_ZIP,
           ROW_NUMBER() OVER (
               PARTITION BY pd.GRGR_CK, pd.CSCS_ID, pd.CSPD_CAT, pd.CSPI_ID, pd.CSPI_EFF_DT
               ORDER BY y.PYPY_EFF_DT DESC) AS RN
    FROM product AS pd
    CROSS APPLY (SELECT TOP (1) x.PYPY_ID FROM dbo.CMC_LOBD_LINE_BUS AS x
                 WHERE x.LOBD_ID = pd.LOBD_ID ORDER BY x.PYPY_ID) AS lb
    JOIN dbo.CMC_PYPY_PAYOR AS y
      ON y.PYPY_ID = lb.PYPY_ID
     AND y.PYPY_EFF_DT <= CAST(GETDATE() AS date)
     AND ISNULL(y.PYPY_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    WHERE pd.RN = 1
)
SELECT r.GRGR_CK, r.CSCS_ID, r.CSPD_CAT, r.CSPI_ID, r.CSPI_EFF_DT, r.CSPI_TERM_DT,
       r.PDPD_ID, r.NWST_PFX, r.CSPI_PDPD_BEG_MMDD,
       pl.PLDS_DESC, cs.TPCT_PFX, pd.LOBD_ID,
       ds.PDDS_DESC, ds.PDDS_PROD_TYPE, ds.PDDS_MCTR_BCAT, ds.PDDS_UM_IND, ds.PDDS_OON_IND,
       lb.PYPY_ID, py.PYPY_PAYOR_NAME, py.PYPY_NATL_PLAN_ID, py.PYPY_NAIC_CODE, py.PYPY_TAX_ID,
       py.PYPY_ADDR1, py.PYPY_ADDR2, py.PYPY_CITY, py.PYPY_STATE, py.PYPY_ZIP
FROM scope_plan AS r
LEFT JOIN product AS pd
       ON pd.GRGR_CK = r.GRGR_CK AND pd.CSCS_ID = r.CSCS_ID AND pd.CSPD_CAT = r.CSPD_CAT
      AND pd.CSPI_ID = r.CSPI_ID AND pd.CSPI_EFF_DT = r.CSPI_EFF_DT AND pd.RN = 1
-- One row from each lookup, whatever its key is in the customer's release:
-- a description table with several rows per code must not multiply plans.
OUTER APPLY (SELECT TOP (1) x.PYPY_ID FROM dbo.CMC_LOBD_LINE_BUS AS x
             WHERE x.LOBD_ID = pd.LOBD_ID ORDER BY x.PYPY_ID) AS lb
LEFT JOIN payor AS py
       ON py.GRGR_CK = r.GRGR_CK AND py.CSCS_ID = r.CSCS_ID AND py.CSPD_CAT = r.CSPD_CAT
      AND py.CSPI_ID = r.CSPI_ID AND py.CSPI_EFF_DT = r.CSPI_EFF_DT AND py.RN = 1
OUTER APPLY (SELECT TOP (1) x.PLDS_DESC FROM dbo.CMC_PLDS_PLAN_DESC AS x
             WHERE x.CSPI_ID = r.CSPI_ID ORDER BY x.PLDS_DESC) AS pl
OUTER APPLY (SELECT TOP (1) x.TPCT_PFX FROM dbo.CMC_CSCS_CLASS AS x
             WHERE x.GRGR_CK = r.GRGR_CK AND x.CSCS_ID = r.CSCS_ID ORDER BY x.TPCT_PFX) AS cs
OUTER APPLY (SELECT TOP (1) x.PDDS_DESC, x.PDDS_PROD_TYPE, x.PDDS_MCTR_BCAT, x.PDDS_UM_IND,
                    x.PDDS_OON_IND
             FROM dbo.CMC_PDDS_PROD_DESC AS x
             WHERE x.PDPD_ID = r.PDPD_ID ORDER BY x.PDDS_DESC) AS ds;


-- table: plan_network
-- The network-set rows in force today of the network sets the plans in scope
-- use: which networks, in-network (I) vs participating (P), and the tier that
-- selects the cost-share row in plan_benefit_denorm.
WITH scope_plan AS (
    SELECT p.GRGR_CK, p.CSCS_ID, p.CSPD_CAT, p.CSPI_ID, p.PDPD_ID, p.NWST_PFX
    FROM dbo.CMC_CSPI_CS_PLAN AS p
    WHERE p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND EXISTS (SELECT 1 FROM dbo.CMC_MEPE_PRCS_ELIG AS e
                  WHERE e.GRGR_CK = p.GRGR_CK AND e.CSCS_ID = p.CSCS_ID
                    AND e.CSPD_CAT = p.CSPD_CAT AND e.CSPI_ID = p.CSPI_ID
                    AND e.MEPE_ELIG_IND = 'Y'
                    AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
)
SELECT ns.NWST_PFX, ns.NWST_EFF_DT, ns.NWST_SEQ_NO, ns.NWST_TERM_DT, ns.NWNW_ID,
       ns.NWPR_PFX, ns.NWST_PR_NW_STS, ns.PDVC_TIER, n.NWNW_NAME, n.NWNW_MCTR_TYPE
FROM dbo.CMC_NWST_NET_SET AS ns
LEFT JOIN dbo.CMC_NWNW_NETWORK AS n ON n.NWNW_ID = ns.NWNW_ID
WHERE ns.NWST_PFX IN (SELECT NWST_PFX FROM scope_plan)
  AND ns.NWST_EFF_DT <= CAST(GETDATE() AS date)
  AND ISNULL(ns.NWST_TERM_DT, '99991231') >= CAST(GETDATE() AS date);


-- table: plan_benefit_denorm
-- What each product in scope covers today and at what cost share, per
-- provider tier:
--   PDVC (product x tier x conditions) -> SEPY (covered Service IDs + rule)
--   -> SESE (rule type, age/sex) -> SETR (copay, coinsurance, deductible
--   accumulator) and SEDF (referral / pre-authorization requirement).
-- Only PDVC and SEPY rows in force today. BEN_EFF_DT / BEN_TERM_DT is where
-- the two windows overlap ('9999-12-31' when both are open). SEDF comes from
-- the product's service-definition component (PDBC_TYPE = 'SEDF', assumption
-- 9), the component and SEDF rows in force today.
-- Only the product + payment prefix + Service ID combinations billed on the
-- lines of the claims in scope (members eligible today, last 24 months):
-- what generated claims use. A line without a SEPY_PFX matches any prefix
-- of its product. Every product's full covered-service list is tens of
-- millions of rows and is not needed.
WITH scope_plan AS (
    SELECT p.GRGR_CK, p.CSCS_ID, p.CSPD_CAT, p.CSPI_ID, p.PDPD_ID, p.NWST_PFX
    FROM dbo.CMC_CSPI_CS_PLAN AS p
    WHERE p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND EXISTS (SELECT 1 FROM dbo.CMC_MEPE_PRCS_ELIG AS e
                  WHERE e.GRGR_CK = p.GRGR_CK AND e.CSCS_ID = p.CSCS_ID
                    AND e.CSPD_CAT = p.CSPD_CAT AND e.CSPI_ID = p.CSPI_ID
                    AND e.MEPE_ELIG_IND = 'Y'
                    AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
-- Products of the current plans and of the eligible spans in force today,
-- that have a product row in force today.
scope_pdpd AS (
    SELECT x.PDPD_ID
    FROM (SELECT PDPD_ID FROM scope_plan
          UNION
          SELECT e.PDPD_ID FROM dbo.CMC_MEPE_PRCS_ELIG AS e
          WHERE e.MEPE_ELIG_IND = 'Y'
            AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
            AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)) AS x
    WHERE EXISTS (SELECT 1 FROM dbo.CMC_PDPD_PRODUCT AS d
                  WHERE d.PDPD_ID = x.PDPD_ID
                    AND d.PDPD_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(d.PDPD_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_use AS (
    SELECT DISTINCT c.PDPD_ID, NULLIF(RTRIM(l.SEPY_PFX), '') AS SEPY_PFX, l.SESE_ID
    FROM dbo.CMC_CDML_CL_LINE AS l
    JOIN dbo.CMC_CLCL_CLAIM AS c ON c.CLCL_ID = l.CLCL_ID
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND NULLIF(RTRIM(l.SESE_ID), '') IS NOT NULL
),
pair AS (
    SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT,
           v.PDVC_PR_PCP, v.PDVC_PR_IN, v.PDVC_PR_PAR, v.PDVC_PR_NONPAR,
           v.PDVC_PC_NR, v.PDVC_PC_OBT, v.PDVC_PC_VIOL,
           v.PDVC_REF_NR, v.PDVC_REF_OBT, v.PDVC_REF_VIOL, v.PDVC_LOBD_PTR,
           v.SEPY_PFX, v.DEDE_PFX, v.LTLT_PFX,
           sp.SEPY_EFF_DT, sp.SEPY_TERM_DT, sp.SESE_ID, sp.SESE_RULE,
           CASE WHEN sp.SEPY_EFF_DT > v.PDVC_EFF_DT THEN sp.SEPY_EFF_DT
                ELSE v.PDVC_EFF_DT END AS BEN_EFF_DT,
           CASE WHEN ISNULL(sp.SEPY_TERM_DT, '99991231') < ISNULL(v.PDVC_TERM_DT, '99991231')
                THEN ISNULL(sp.SEPY_TERM_DT, '99991231')
                ELSE ISNULL(v.PDVC_TERM_DT, '99991231') END AS BEN_TERM_DT
    FROM dbo.CMC_PDVC_VAR_COMP AS v
    JOIN scope_pdpd AS k ON k.PDPD_ID = v.PDPD_ID
    JOIN dbo.CMC_SEPY_SE_PYMT AS sp
      ON sp.SEPY_PFX = v.SEPY_PFX
     AND sp.SEPY_EFF_DT <= CAST(GETDATE() AS date)
     AND ISNULL(sp.SEPY_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    WHERE EXISTS (SELECT 1 FROM scope_use AS u
                  WHERE u.PDPD_ID = v.PDPD_ID AND u.SESE_ID = sp.SESE_ID
                    AND (u.SEPY_PFX = v.SEPY_PFX OR u.SEPY_PFX IS NULL))
      AND v.PDVC_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(v.PDVC_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
sedf AS (
    SELECT b.PDPD_ID, b.SESE_ID,
           f.SEDF_UM_REQ_IND, f.SEDF_UM_BP_REF_IND, f.SEDF_CAP_IND,
           ROW_NUMBER() OVER (PARTITION BY b.PDPD_ID, b.SESE_ID
                              ORDER BY f.SEDF_EFF_DT DESC, pc.PDBC_EFF_DT DESC) AS RN
    FROM (SELECT DISTINCT PDPD_ID, SESE_ID FROM pair) AS b
    JOIN dbo.CMC_PDBC_PROD_COMP AS pc
      ON pc.PDPD_ID = b.PDPD_ID
     AND pc.PDBC_TYPE = 'SEDF'
     AND pc.PDBC_EFF_DT <= CAST(GETDATE() AS date)
     AND ISNULL(pc.PDBC_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    JOIN dbo.CMC_SEDF_SE_DEFN AS f
      ON f.PDBC_PFX = pc.PDBC_PFX
     AND f.SESE_ID = b.SESE_ID
     AND f.SEDF_EFF_DT <= CAST(GETDATE() AS date)
     AND ISNULL(f.SEDF_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
)
SELECT b.PDPD_ID, b.PDVC_TIER, b.PDVC_TYPE, b.PDVC_SEQ_NO, b.PDVC_EFF_DT, b.PDVC_TERM_DT,
       b.PDVC_PR_PCP, b.PDVC_PR_IN, b.PDVC_PR_PAR, b.PDVC_PR_NONPAR,
       b.PDVC_PC_NR, b.PDVC_PC_OBT, b.PDVC_PC_VIOL,
       b.PDVC_REF_NR, b.PDVC_REF_OBT, b.PDVC_REF_VIOL, b.PDVC_LOBD_PTR,
       b.SEPY_PFX, b.DEDE_PFX, b.LTLT_PFX, b.SEPY_EFF_DT, b.SEPY_TERM_DT,
       b.BEN_EFF_DT, b.BEN_TERM_DT, b.SESE_ID, b.SESE_RULE,
       sd.SEDS_DESC, sd.SEDS_TYPE,
       se.SESE_RULE_TYPE, se.SESE_CALC_IND, se.SESE_VALID_SEX, se.SESE_MIN_AGE,
       se.SESE_MAX_AGE, se.SESE_COV_TYPE,
       tr.SETR_TIER_NO, tr.SETR_ALLOW_AMT, tr.SETR_ALLOW_CTR, tr.SETR_COPAY_AMT,
       tr.SETR_COIN_PCT, tr.ACAC_ACC_NO,
       df.SEDF_UM_REQ_IND, df.SEDF_UM_BP_REF_IND, df.SEDF_CAP_IND
FROM pair AS b
LEFT JOIN dbo.CMC_SESE_SERVICE AS se ON se.SESE_ID = b.SESE_ID AND se.SESE_RULE = b.SESE_RULE
LEFT JOIN dbo.CMC_SEDS_SE_DESC AS sd ON sd.SESE_ID = b.SESE_ID
LEFT JOIN dbo.CMC_SETR_TIER AS tr ON tr.SESE_ID = b.SESE_ID AND tr.SESE_RULE = b.SESE_RULE
LEFT JOIN sedf AS df ON df.PDPD_ID = b.PDPD_ID AND df.SESE_ID = b.SESE_ID AND df.RN = 1;


-- table: plan_accumulator
-- Deductibles (ACCUM_KIND 'DED', from DEDE) and limits / stop-loss ('LMT',
-- from LTLT, with the Service IDs they count from LTSE) reached from the
-- product variable components in force today of the products in scope.
-- (DEDE, LTLT and LTSE carry no dates.)
WITH scope_plan AS (
    SELECT p.GRGR_CK, p.CSCS_ID, p.CSPD_CAT, p.CSPI_ID, p.PDPD_ID, p.NWST_PFX
    FROM dbo.CMC_CSPI_CS_PLAN AS p
    WHERE p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND EXISTS (SELECT 1 FROM dbo.CMC_MEPE_PRCS_ELIG AS e
                  WHERE e.GRGR_CK = p.GRGR_CK AND e.CSCS_ID = p.CSCS_ID
                    AND e.CSPD_CAT = p.CSPD_CAT AND e.CSPI_ID = p.CSPI_ID
                    AND e.MEPE_ELIG_IND = 'Y'
                    AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
scope_pdpd AS (
    SELECT x.PDPD_ID
    FROM (SELECT PDPD_ID FROM scope_plan
          UNION
          SELECT e.PDPD_ID FROM dbo.CMC_MEPE_PRCS_ELIG AS e
          WHERE e.MEPE_ELIG_IND = 'Y'
            AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
            AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)) AS x
    WHERE EXISTS (SELECT 1 FROM dbo.CMC_PDPD_PRODUCT AS d
                  WHERE d.PDPD_ID = x.PDPD_ID
                    AND d.PDPD_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(d.PDPD_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
pdvc AS (
    SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT,
           v.DEDE_PFX, v.LTLT_PFX
    FROM dbo.CMC_PDVC_VAR_COMP AS v
    JOIN scope_pdpd AS k ON k.PDPD_ID = v.PDPD_ID
    WHERE v.PDVC_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(v.PDVC_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
)
SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT,
       CAST('DED' AS char(3))          AS ACCUM_KIND,
       d.DEDE_PFX                      AS ACCUM_PFX,
       d.ACAC_ACC_NO,
       CAST(d.DEDE_DESC AS varchar(70)) AS ACCUM_DESC,
       d.DEDE_RULE, d.DEDE_PERIOD_IND, d.DEDE_MEME_AMT, d.DEDE_FAM_AMT,
       CAST(NULL AS char(1))           AS LTLT_CAT,
       CAST(NULL AS char(1))           AS LTLT_LEVEL,
       CAST(NULL AS char(1))           AS LTLT_PERIOD_IND,
       CAST(NULL AS char(1))           AS LTLT_RULE,
       CAST(NULL AS char(1))           AS LTLT_IX_IND,
       CAST(NULL AS char(1))           AS LTLT_IX_TYPE,
       CAST(NULL AS money)             AS LTLT_AMT1,
       CAST(NULL AS char(4))           AS SESE_ID,
       CAST(NULL AS int)               AS LTSE_WT_CTR
FROM pdvc AS v
JOIN dbo.CMC_DEDE_DEDUCT AS d ON d.DEDE_PFX = v.DEDE_PFX
UNION ALL
SELECT v.PDPD_ID, v.PDVC_TIER, v.PDVC_TYPE, v.PDVC_SEQ_NO, v.PDVC_EFF_DT, v.PDVC_TERM_DT,
       CAST('LMT' AS char(3)),
       l.LTLT_PFX,
       l.ACAC_ACC_NO,
       CAST(l.LTLT_DESC AS varchar(70)),
       NULL, NULL, NULL, NULL,
       l.LTLT_CAT, l.LTLT_LEVEL, l.LTLT_PERIOD_IND, l.LTLT_RULE, l.LTLT_IX_IND, l.LTLT_IX_TYPE,
       l.LTLT_AMT1, x.SESE_ID, x.LTSE_WT_CTR
FROM pdvc AS v
JOIN dbo.CMC_LTLT_LIMIT AS l ON l.LTLT_PFX = v.LTLT_PFX
LEFT JOIN dbo.CMC_LTSE_SE_IX AS x ON x.LTLT_PFX = l.LTLT_PFX AND x.ACAC_ACC_NO = l.ACAC_ACC_NO;


-- table: service_code_map
-- Procedure / revenue code + setting -> Service ID, for the service conversion
-- prefixes (CMC_CSCS_CLASS.TPCT_PFX) of the classes of the plans in scope:
-- procedure ranges ('PROC', CMC_TPCT_CPT_CONV), revenue-code ranges ('REV',
-- CMC_RCCT_CONV) and default services ('SVC', CMC_SECT_CONV). (These tables
-- carry no dates.)
WITH scope_plan AS (
    SELECT p.GRGR_CK, p.CSCS_ID, p.CSPD_CAT, p.CSPI_ID, p.PDPD_ID, p.NWST_PFX
    FROM dbo.CMC_CSPI_CS_PLAN AS p
    WHERE p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND EXISTS (SELECT 1 FROM dbo.CMC_MEPE_PRCS_ELIG AS e
                  WHERE e.GRGR_CK = p.GRGR_CK AND e.CSCS_ID = p.CSCS_ID
                    AND e.CSPD_CAT = p.CSPD_CAT AND e.CSPI_ID = p.CSPI_ID
                    AND e.MEPE_ELIG_IND = 'Y'
                    AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
scope_tpct AS (
    SELECT DISTINCT cs.TPCT_PFX
    FROM dbo.CMC_CSCS_CLASS AS cs
    JOIN (SELECT DISTINCT GRGR_CK, CSCS_ID FROM scope_plan) AS pc
      ON pc.GRGR_CK = cs.GRGR_CK AND pc.CSCS_ID = cs.CSCS_ID
    WHERE NULLIF(RTRIM(cs.TPCT_PFX), '') IS NOT NULL
)
SELECT tp.TPCT_PFX,
       CAST('PROC' AS char(4))           AS MAP_KIND,
       tp.TPCT_MCTR_TCAT,
       tp.TPCT_MCTR_SETG                 AS MCTR_SETG,
       CAST(tp.TPCT_IPCD_LOW AS char(7))  AS CODE_LOW,
       CAST(tp.TPCT_IPCD_HIGH AS char(7)) AS CODE_HIGH,
       tp.TPCT_MODIFIER,
       tp.SESE_ID, tp.CSPD_CAT, tp.RPTC_RPT_CAT
FROM dbo.CMC_TPCT_CPT_CONV AS tp
JOIN scope_tpct AS k ON k.TPCT_PFX = tp.TPCT_PFX
UNION ALL
SELECT rc.TPCT_PFX, CAST('REV' AS char(4)), rc.TPCT_MCTR_TCAT, rc.RCCT_MCTR_SETG,
       CAST(rc.RCCT_RCRC_ID_LOW AS char(7)), CAST(rc.RCCT_RCRC_ID_HIGH AS char(7)),
       CAST(NULL AS char(2)), rc.SESE_ID, rc.CSPD_CAT, rc.RPTC_RPT_CAT
FROM dbo.CMC_RCCT_CONV AS rc
JOIN scope_tpct AS k ON k.TPCT_PFX = rc.TPCT_PFX
UNION ALL
SELECT sc.TPCT_PFX, CAST('SVC' AS char(4)), CAST(NULL AS char(1)), CAST(NULL AS char(1)),
       CAST(NULL AS char(7)), CAST(NULL AS char(7)), CAST(NULL AS char(2)),
       sc.SESE_ID, sc.CSPD_CAT, sc.RPTC_RPT_CAT
FROM dbo.CMC_SECT_CONV AS sc
JOIN scope_tpct AS k ON k.TPCT_PFX = sc.TPCT_PFX;


-- table: place_of_service
-- Every place of service with its setting (small reference table, no scope).
SELECT ps.PSCD_ID, ps.PSCD_DESC, ps.PSCD_POS_IND, ps.TPCT_MCTR_TCAT, ps.TPCT_MCTR_SETG
FROM dbo.CMC_PSCD_POS_DESC AS ps;


-- ===========================================================================
-- Claims (837 support): the claims claims_denorm holds
--
-- scope_claim is the same CTE in each block: claims with at least one line,
-- of members with an eligible span in force today, first service date
-- (CLCL_LOW_SVC_DT) in the last 24 months.
-- ===========================================================================

-- table: claims_header_ext_denorm
-- One row per claim: 2300 fields, institutional (UB) header, claim-level
-- providers (attending, operating, service facility, rendering, payee), EDI
-- and claim-level COB. CLM_FREQUENCY for 837P is derived: '7' when the claim
-- adjusts another, else '1' (assumption from 04; voids are not identifiable).
-- CLMF_EXT_REFRAL_NO / CLMF_EXT_PREATH_NO (50 in some releases) are cut to
-- the 18 the SDM columns hold, CLCL_PA_ACCT_NO to 35.
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
)
SELECT cl.CLCL_ID, cl.MEME_CK, cl.CSCS_ID, cl.PRPR_ID, cl.PRAD_TYPE, cl.CLCL_PAYEE_PR_ID,
       cl.NWPR_PFX, cl.AGAG_ID, CAST(LEFT(cl.CLCL_PA_ACCT_NO, 35) AS varchar(35)) AS CLCL_PA_ACCT_NO,
       cl.CLCL_PAY_PR_IND, cl.CLCL_REL_INFO_IND,
       cl.CLCL_MED_ASSGN_IND, cl.CLCL_ACD_IND, cl.CLCL_ACD_STATE, cl.CLCL_ACD_DT,
       cl.CLCL_CURR_ILL_DT, cl.CLCL_SIMI_ILL_DT, cl.CLCL_UNABL_FROM_DT, cl.CLCL_UNABL_TO_DT,
       cl.CLCL_RELHP_FROM_DT, cl.CLCL_RELHP_TO_DT, cl.CLCL_OUT_LAB_IND, cl.CLCL_ID_ADJ_FROM,
       cl.CLCL_MECD_RESUB_NO, cl.CLCL_OTHER_BN_IND, cl.CLCL_EXT_REF_IND,
       CAST(CASE WHEN NULLIF(RTRIM(cl.CLCL_ID_ADJ_FROM), '') IS NOT NULL THEN '7' ELSE '1' END
            AS char(1)) AS CLM_FREQUENCY,
       hp.CLHP_FAC_TYPE, hp.CLHP_BILL_CLASS, hp.CLHP_FREQUENCY, hp.CLHP_ADM_TYP,
       hp.CLHP_ADM_SOURCE, hp.CLHP_DC_STAT, hp.CLHP_ADM_DT, hp.CLHP_ADM_HOUR_CD, hp.CLHP_DC_DT,
       hp.CLHP_DC_HOUR_CD, hp.CLHP_STAMENT_FR_DT, hp.CLHP_STAMENT_TO_DT, hp.CLHP_MED_REC_NO,
       hp.AGRG_ID, hp.CLHP_INPUT_AGRG_ID,
       hp.CLHP_PRPR_ID_ADM, hp.CLHP_PRPR_AD_NPI, hp.CLHP_PRPR_ID_OPER, hp.CLHP_PRPR_OP_NPI,
       hp.CLHP_PRPR_ID_OTH1, hp.CLHP_PRPR_O1_NPI, hp.CLHP_ICD_QUAL_IND, hp.CLHP_BIRTH_WGT,
       mf.CLMF_PRPR_ID_FAC, mf.CLMF_PRPR_FA_NPI, mf.CLMF_PRPR_RF_NPI, mf.CLMF_INPUT_TXNM_CD,
       CAST(LEFT(mf.CLMF_EXT_REFRAL_NO, 18) AS varchar(18)) AS CLMF_EXT_REFRAL_NO,
       CAST(LEFT(mf.CLMF_EXT_PREATH_NO, 18) AS varchar(18)) AS CLMF_EXT_PREATH_NO, mf.CLMF_ICD_IND_PROC,
       sd.CLSD_PRPR_ID_REND_NVL, sd.CLSD_PRPR_RE_NPI_NVL,
       ed.CLED_HIPAA_VERSION, ed.CLED_EXT_REF, ed.CLED_TRAD_PARTNER,
       cb.CLCB_COB_TYPE, cb.CLCB_COB_AMT, cb.CLCB_COB_ALLOW, cb.CLCB_COB_DED_AMT,
       cb.CLCB_COB_COPAY_AMT, cb.CLCB_COB_COINS_AMT,
       CAST(NULL AS char(4)) AS CLCB_COB_REAS_CD
FROM dbo.CMC_CLCL_CLAIM AS cl
JOIN scope_claim AS k ON k.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLHP_HOSP AS hp ON hp.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLMF_MULT_FUNC AS mf ON mf.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLSD_SUPP_DATA AS sd ON sd.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLED_EDI_DATA AS ed ON ed.CLCL_ID = cl.CLCL_ID
LEFT JOIN dbo.CMC_CLCB_CL_COB AS cb ON cb.CLCL_ID = cl.CLCL_ID;


-- table: claims_detail_denorm
-- One row per claim line: the 2400 fields claims_denorm lacks. PROC_CODE and
-- IPCD_MOD1 split CDML IPCD_ID (char 7) into code and first modifier
-- (assumption 9).
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
)
SELECT d.CLCL_ID, d.CDML_SEQ_NO, d.CDML_EXT_LINE_NO,
       CAST(LEFT(d.IPCD_ID, 5) AS char(5))                     AS PROC_CODE,
       CAST(NULLIF(SUBSTRING(d.IPCD_ID, 6, 2), '') AS char(2)) AS IPCD_MOD1,
       d.CDML_IPCD_MOD2, d.CDML_IPCD_MOD3, d.CDML_IPCD_MOD4,
       d.IDCD_ID, d.IDCD_ID_REL,
       d.CDML_CLMD_TYPE2, d.CDML_CLMD_TYPE3, d.CDML_CLMD_TYPE4, d.CDML_CLMD_TYPE5,
       d.SESE_ID, d.SESE_RULE, d.SEPY_PFX, d.DEDE_PFX, d.LTLT_PFX, d.PDVC_LOBD_PTR,
       d.CDML_ROOM_TYPE, d.CDML_HCPCS_AMT, d.CDML_REF_IND, d.CDML_PC_IND,
       d.CDML_UMREF_ID, d.CDML_REFSV_SEQ_NO, d.CDML_UMAUTH_ID, d.CDML_AUTHSV_SEQ_NO,
       d.CDML_CL_NTWK_IND, d.CDML_DISALL_EXCD,
       sd.CDSD_PRPR_ID_REND, sd.CDSD_PRPR_RE_NPI, sd.CDSD_NDC_CODE, sd.CDSD_NDC_UNITS,
       sd.CDSD_NDC_MCTR_TYPE, sd.CDSD_SERV_LOC_ZIP, sd.CDSD_MED_AMB_ZIP,
       cb.CDCB_COB_TYPE, cb.CDCB_COB_AMT, cb.CDCB_COB_ALLOW, cb.CDCB_COB_DED_AMT,
       cb.CDCB_COB_COPAY_AMT, cb.CDCB_COB_COINS_AMT,
       CAST(NULL AS char(4)) AS CDCB_COB_REAS_CD
FROM dbo.CMC_CDML_CL_LINE AS d
JOIN scope_claim AS k ON k.CLCL_ID = d.CLCL_ID
LEFT JOIN dbo.CMC_CDSD_SUPP_DATA AS sd ON sd.CLCL_ID = d.CLCL_ID AND sd.CDML_SEQ_NO = d.CDML_SEQ_NO
LEFT JOIN dbo.CMC_CDCB_LI_COB AS cb ON cb.CLCL_ID = d.CLCL_ID AND cb.CDML_SEQ_NO = d.CDML_SEQ_NO;


-- table: claims_diag
-- Every diagnosis of every claim in scope (HI segments).
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
)
SELECT dg.CLCL_ID, dg.CLMD_TYPE, dg.IDCD_ID, dg.CLMD_POA_IND, dg.IDCD_ID_SUB
FROM dbo.CMC_CLMD_DIAG AS dg
JOIN scope_claim AS k ON k.CLCL_ID = dg.CLCL_ID;


-- table: claims_icd_proc
-- Institutional claim-level procedures (837I HI*BBR / BBQ).
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
)
SELECT hi.CLCL_ID, hi.CLHI_TYPE, hi.IPCD_ID, hi.CLHI_IP_DT
FROM dbo.CMC_CLHI_PROC AS hi
JOIN scope_claim AS k ON k.CLCL_ID = hi.CLCL_ID;


-- table: claims_ub_code
-- UB condition (HI*BG), occurrence (HI*BH / BI) and value (HI*BE) codes.
-- Value codes have no sequence number of their own; CODE_SEQ numbers them in
-- CLVC_NUMBER, CLVC_LETTER order.
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
)
SELECT hc.CLCL_ID, CAST('COND' AS char(4)) AS CODE_KIND, hc.CLHC_SEQ_NO AS CODE_SEQ,
       hc.CLHC_COND_CD AS UB_CODE,
       CAST(NULL AS datetime) AS FROM_DT, CAST(NULL AS datetime) AS TO_DT,
       CAST(NULL AS money) AS VALUE_AMT, CAST(NULL AS smallint) AS VALUE_UNITS
FROM dbo.CMC_CLHC_COND_CODE AS hc
JOIN scope_claim AS k ON k.CLCL_ID = hc.CLCL_ID
UNION ALL
SELECT ho.CLCL_ID, CAST('OCC' AS char(4)), ho.CLHO_SEQ_NO, ho.CLHO_OCC_CODE,
       ho.CLHO_OCC_FROM_DT, ho.CLHO_OCC_TO_DT, NULL, NULL
FROM dbo.CMC_CLHO_OCC_CODE AS ho
JOIN scope_claim AS k ON k.CLCL_ID = ho.CLCL_ID
UNION ALL
SELECT vc.CLCL_ID, CAST('VAL' AS char(4)),
       CAST(ROW_NUMBER() OVER (PARTITION BY vc.CLCL_ID
                               ORDER BY vc.CLVC_NUMBER, vc.CLVC_LETTER) AS smallint),
       vc.CLVC_CODE, NULL, NULL, vc.CLVC_AMT, vc.CLVC_VALUE
FROM dbo.CMC_CLVC_VAL_CODE AS vc
JOIN scope_claim AS k ON k.CLCL_ID = vc.CLCL_ID;


-- table: claims_auth
-- The authorized ('AUTH', CDML_UMAUTH_ID) or referred ('REF', CDML_UMREF_ID)
-- service each line of a claim in scope points to (REF*G1 / REF*9F), so
-- generated lines stay within the authorized dates, provider, POS and units.
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
)
SELECT d.CLCL_ID, d.CDML_SEQ_NO, CAST('AUTH' AS char(4)) AS AUTH_KIND,
       u.UMUM_REF_ID, u.UMSV_SEQ_NO, u.UMSV_FROM_DT, u.UMSV_TO_DT,
       u.UMSV_PRPR_ID_REQ, u.UMSV_PRPR_ID_SVC, u.UMSV_PRPR_ID_FAC, u.UMSV_PSCD_ID_AUTH,
       u.IPCD_ID, u.SESE_ID, u.UMSV_UNITS_AUTH, u.UMVT_STS, u.UMSV_AUTH_DT
FROM dbo.CMC_CDML_CL_LINE AS d
JOIN scope_claim AS k ON k.CLCL_ID = d.CLCL_ID
JOIN dbo.CMC_UMSV_SERVICES AS u
  ON u.UMUM_REF_ID = d.CDML_UMAUTH_ID AND u.UMSV_SEQ_NO = d.CDML_AUTHSV_SEQ_NO
UNION ALL
SELECT d.CLCL_ID, d.CDML_SEQ_NO, CAST('REF' AS char(4)),
       u.UMUM_REF_ID, u.UMSV_SEQ_NO, u.UMSV_FROM_DT, u.UMSV_TO_DT,
       u.UMSV_PRPR_ID_REQ, u.UMSV_PRPR_ID_SVC, u.UMSV_PRPR_ID_FAC, u.UMSV_PSCD_ID_AUTH,
       u.IPCD_ID, u.SESE_ID, u.UMSV_UNITS_AUTH, u.UMVT_STS, u.UMSV_AUTH_DT
FROM dbo.CMC_CDML_CL_LINE AS d
JOIN scope_claim AS k ON k.CLCL_ID = d.CLCL_ID
JOIN dbo.CMC_UMSV_SERVICES AS u
  ON u.UMUM_REF_ID = d.CDML_UMREF_ID AND u.UMSV_SEQ_NO = d.CDML_REFSV_SEQ_NO;


-- ===========================================================================
-- Provider (837 support): active providers only
--
-- The provider scope (scope_prov) is the same CTE in each of the four blocks:
-- the ACTIVE providers (assumption 10) that a claim in scope names
-- (servicing, referring, PCP, payee), the institutional claim's admitting /
-- operating / other physician, the service facility, the claim and line
-- rendering providers, the requesting / servicing / facility providers of the
-- authorizations and referrals the claim's lines point to, the PCPs in force
-- today of the members in scope, and the providers on the roster today
-- (CMC_NWPR_RELATION in force today) of the networks in the network sets in
-- force today of the plans in scope -- the pool generated claims draw from.
-- The roster part can be large; drop that UNION branch to keep only
-- providers seen on claims and PCP assignments.
-- ===========================================================================

-- table: provider_relation
-- Relationships in force today:
-- PRER: practitioner/facility -> group (G), IPA (I) or sponsor (S).
-- PRDS: claims payee (2010AB): the provider itself (PRDS_PAY_CL_IND 'P') or
--   the PRER_PRPR_ID of the relationship in force today whose entity equals
--   PRDS_PAY_CL_IND.
-- PRAF: practitioner -> affiliated facility (through PRCR_ID), for 837I
--   attending / operating physicians at the billing facility.
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
),
scope_plan AS (
    SELECT p.NWST_PFX
    FROM dbo.CMC_CSPI_CS_PLAN AS p
    WHERE p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND EXISTS (SELECT 1 FROM dbo.CMC_MEPE_PRCS_ELIG AS e
                  WHERE e.GRGR_CK = p.GRGR_CK AND e.CSCS_ID = p.CSCS_ID
                    AND e.CSPD_CAT = p.CSPD_CAT AND e.CSPI_ID = p.CSPI_ID
                    AND e.MEPE_ELIG_IND = 'Y'
                    AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
scope_net AS (
    SELECT DISTINCT ns.NWNW_ID, ns.NWPR_PFX
    FROM dbo.CMC_NWST_NET_SET AS ns
    WHERE ns.NWST_PFX IN (SELECT NWST_PFX FROM scope_plan)
      AND ns.NWST_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(ns.NWST_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
prov_ref (PRPR_ID) AS (
    SELECT v.PRPR_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_claim AS k ON k.CLCL_ID = c.CLCL_ID
    CROSS APPLY (VALUES (c.PRPR_ID), (c.CLCL_PRPR_ID_REF), (c.CLCL_PRPR_ID_PCP),
                        (c.CLCL_PAYEE_PR_ID)) AS v (PRPR_ID)
    UNION ALL
    SELECT v.PRPR_ID
    FROM dbo.CMC_CLHP_HOSP AS h
    JOIN scope_claim AS k ON k.CLCL_ID = h.CLCL_ID
    CROSS APPLY (VALUES (h.CLHP_PRPR_ID_ADM), (h.CLHP_PRPR_ID_OPER),
                        (h.CLHP_PRPR_ID_OTH1)) AS v (PRPR_ID)
    UNION ALL
    SELECT f.CLMF_PRPR_ID_FAC FROM dbo.CMC_CLMF_MULT_FUNC AS f
    JOIN scope_claim AS k ON k.CLCL_ID = f.CLCL_ID
    UNION ALL
    SELECT CAST(s.CLSD_PRPR_ID_REND_NVL AS char(12)) FROM dbo.CMC_CLSD_SUPP_DATA AS s
    JOIN scope_claim AS k ON k.CLCL_ID = s.CLCL_ID
    UNION ALL
    SELECT CAST(d.CDSD_PRPR_ID_REND AS char(12)) FROM dbo.CMC_CDSD_SUPP_DATA AS d
    JOIN scope_claim AS k ON k.CLCL_ID = d.CLCL_ID
    UNION ALL
    SELECT v.PRPR_ID
    FROM dbo.CMC_CDML_CL_LINE AS l
    JOIN scope_claim AS k ON k.CLCL_ID = l.CLCL_ID
    CROSS APPLY (VALUES (l.CDML_UMAUTH_ID, l.CDML_AUTHSV_SEQ_NO),
                        (l.CDML_UMREF_ID, l.CDML_REFSV_SEQ_NO)) AS x (REF_ID, SEQ_NO)
    JOIN dbo.CMC_UMSV_SERVICES AS u ON u.UMUM_REF_ID = x.REF_ID AND u.UMSV_SEQ_NO = x.SEQ_NO
    CROSS APPLY (VALUES (u.UMSV_PRPR_ID_REQ), (u.UMSV_PRPR_ID_SVC),
                        (u.UMSV_PRPR_ID_FAC)) AS v (PRPR_ID)
    UNION ALL
    SELECT pc.PRPR_ID FROM dbo.CMC_MEPR_PRIM_PROV AS pc
    JOIN scope_member AS k ON k.MEME_CK = pc.MEME_CK
    WHERE pc.MEPR_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(pc.MEPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    UNION ALL
    SELECT nw.PRPR_ID
    FROM dbo.CMC_NWPR_RELATION AS nw
    JOIN scope_net AS k ON k.NWNW_ID = nw.NWNW_ID AND k.NWPR_PFX = nw.NWPR_PFX
    WHERE nw.NWPR_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(nw.NWPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_prov AS (
    SELECT DISTINCT p.PRPR_ID
    FROM prov_ref AS r
    JOIN dbo.CMC_PRPR_PROV AS p ON p.PRPR_ID = r.PRPR_ID
    WHERE ISNULL(p.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
prer AS (
    SELECT r.PRPR_ID, r.PRER_PRPR_ENTITY, r.PRER_EFF_DT, r.PRER_TERM_DT, r.PRER_PRPR_ID
    FROM dbo.CMC_PRER_RELATION AS r
    JOIN scope_prov AS k ON k.PRPR_ID = r.PRPR_ID
    WHERE r.PRER_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(r.PRER_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
payee AS (
    SELECT ds.PRPR_ID, ds.PRDS_EFF_DT, r.PRER_PRPR_ID,
           ROW_NUMBER() OVER (PARTITION BY ds.PRPR_ID, ds.PRDS_EFF_DT
                              ORDER BY r.PRER_EFF_DT DESC) AS RN
    FROM dbo.CMC_PRDS_DATE AS ds
    JOIN prer AS r ON r.PRPR_ID = ds.PRPR_ID AND r.PRER_PRPR_ENTITY = ds.PRDS_PAY_CL_IND
    WHERE ds.PRDS_PAY_CL_IND <> 'P'
)
SELECT r.PRPR_ID, CAST('PRER' AS char(4)) AS REL_SOURCE, r.PRER_PRPR_ENTITY AS REL_ENTITY,
       r.PRER_EFF_DT AS REL_EFF_DT, r.PRER_TERM_DT AS REL_TERM_DT, r.PRER_PRPR_ID AS REL_PRPR_ID,
       rp.PRPR_ENTITY AS REL_PRPR_ENTITY, rp.PRPR_NAME AS REL_PRPR_NAME,
       rp.PRPR_NPI AS REL_PRPR_NPI, rp.PRPR_TAXONOMY_CD AS REL_PRPR_TAXONOMY_CD,
       rp.MCTN_ID AS REL_MCTN_ID,
       CAST(NULL AS char(1)) AS PRAF_PRIM_IND, CAST(NULL AS char(2)) AS PRAF_STS
FROM prer AS r
LEFT JOIN dbo.CMC_PRPR_PROV AS rp ON rp.PRPR_ID = r.PRER_PRPR_ID
UNION ALL
SELECT ds.PRPR_ID, CAST('PRDS' AS char(4)), ds.PRDS_PAY_CL_IND, ds.PRDS_EFF_DT, ds.PRDS_TERM_DT,
       CASE WHEN ds.PRDS_PAY_CL_IND = 'P' THEN ds.PRPR_ID ELSE py.PRER_PRPR_ID END,
       rp.PRPR_ENTITY, rp.PRPR_NAME, rp.PRPR_NPI, rp.PRPR_TAXONOMY_CD, rp.MCTN_ID,
       NULL, NULL
FROM dbo.CMC_PRDS_DATE AS ds
JOIN scope_prov AS k ON k.PRPR_ID = ds.PRPR_ID
LEFT JOIN payee AS py ON py.PRPR_ID = ds.PRPR_ID AND py.PRDS_EFF_DT = ds.PRDS_EFF_DT AND py.RN = 1
LEFT JOIN dbo.CMC_PRPR_PROV AS rp
       ON rp.PRPR_ID = CASE WHEN ds.PRDS_PAY_CL_IND = 'P' THEN ds.PRPR_ID ELSE py.PRER_PRPR_ID END
WHERE ds.PRDS_EFF_DT <= CAST(GETDATE() AS date)
  AND ISNULL(ds.PRDS_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
UNION ALL
SELECT p.PRPR_ID, CAST('PRAF' AS char(4)), CAST('F' AS char(1)), af.PRAF_FROM_DT, af.PRAF_TO_DT,
       af.PRPR_ID, fp.PRPR_ENTITY, fp.PRPR_NAME, fp.PRPR_NPI, fp.PRPR_TAXONOMY_CD, fp.MCTN_ID,
       af.PRAF_PRIM_IND, af.PRAF_STS
FROM dbo.CMC_PRPR_PROV AS p
JOIN scope_prov AS k ON k.PRPR_ID = p.PRPR_ID
JOIN dbo.CMC_PRAF_FAC_AFFIL AS af ON af.PRCR_ID = p.PRCR_ID
LEFT JOIN dbo.CMC_PRPR_PROV AS fp ON fp.PRPR_ID = af.PRPR_ID
WHERE p.PRPR_ENTITY <> 'F'
  AND NULLIF(RTRIM(p.PRCR_ID), '') IS NOT NULL
  AND af.PRAF_FROM_DT <= CAST(GETDATE() AS date)
  AND ISNULL(af.PRAF_TO_DT, '99991231') >= CAST(GETDATE() AS date);


-- table: provider_denorm
-- One row per active provider in scope, plus the active groups, IPAs, payees
-- and facilities they relate to today (one level). Person names come from the
-- common practitioner (PRPR_ENTITY 'P'); the facility name from PRFA. Tax ID
-- type (EI / SY) from MCTN; where one MCTN_ID has rows of both types, the one
-- the provider's CMC_MCTI_TAX_INFO row names wins.
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
),
scope_plan AS (
    SELECT p.NWST_PFX
    FROM dbo.CMC_CSPI_CS_PLAN AS p
    WHERE p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND EXISTS (SELECT 1 FROM dbo.CMC_MEPE_PRCS_ELIG AS e
                  WHERE e.GRGR_CK = p.GRGR_CK AND e.CSCS_ID = p.CSCS_ID
                    AND e.CSPD_CAT = p.CSPD_CAT AND e.CSPI_ID = p.CSPI_ID
                    AND e.MEPE_ELIG_IND = 'Y'
                    AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
scope_net AS (
    SELECT DISTINCT ns.NWNW_ID, ns.NWPR_PFX
    FROM dbo.CMC_NWST_NET_SET AS ns
    WHERE ns.NWST_PFX IN (SELECT NWST_PFX FROM scope_plan)
      AND ns.NWST_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(ns.NWST_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
prov_ref (PRPR_ID) AS (
    SELECT v.PRPR_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_claim AS k ON k.CLCL_ID = c.CLCL_ID
    CROSS APPLY (VALUES (c.PRPR_ID), (c.CLCL_PRPR_ID_REF), (c.CLCL_PRPR_ID_PCP),
                        (c.CLCL_PAYEE_PR_ID)) AS v (PRPR_ID)
    UNION ALL
    SELECT v.PRPR_ID
    FROM dbo.CMC_CLHP_HOSP AS h
    JOIN scope_claim AS k ON k.CLCL_ID = h.CLCL_ID
    CROSS APPLY (VALUES (h.CLHP_PRPR_ID_ADM), (h.CLHP_PRPR_ID_OPER),
                        (h.CLHP_PRPR_ID_OTH1)) AS v (PRPR_ID)
    UNION ALL
    SELECT f.CLMF_PRPR_ID_FAC FROM dbo.CMC_CLMF_MULT_FUNC AS f
    JOIN scope_claim AS k ON k.CLCL_ID = f.CLCL_ID
    UNION ALL
    SELECT CAST(s.CLSD_PRPR_ID_REND_NVL AS char(12)) FROM dbo.CMC_CLSD_SUPP_DATA AS s
    JOIN scope_claim AS k ON k.CLCL_ID = s.CLCL_ID
    UNION ALL
    SELECT CAST(d.CDSD_PRPR_ID_REND AS char(12)) FROM dbo.CMC_CDSD_SUPP_DATA AS d
    JOIN scope_claim AS k ON k.CLCL_ID = d.CLCL_ID
    UNION ALL
    SELECT v.PRPR_ID
    FROM dbo.CMC_CDML_CL_LINE AS l
    JOIN scope_claim AS k ON k.CLCL_ID = l.CLCL_ID
    CROSS APPLY (VALUES (l.CDML_UMAUTH_ID, l.CDML_AUTHSV_SEQ_NO),
                        (l.CDML_UMREF_ID, l.CDML_REFSV_SEQ_NO)) AS x (REF_ID, SEQ_NO)
    JOIN dbo.CMC_UMSV_SERVICES AS u ON u.UMUM_REF_ID = x.REF_ID AND u.UMSV_SEQ_NO = x.SEQ_NO
    CROSS APPLY (VALUES (u.UMSV_PRPR_ID_REQ), (u.UMSV_PRPR_ID_SVC),
                        (u.UMSV_PRPR_ID_FAC)) AS v (PRPR_ID)
    UNION ALL
    SELECT pc.PRPR_ID FROM dbo.CMC_MEPR_PRIM_PROV AS pc
    JOIN scope_member AS k ON k.MEME_CK = pc.MEME_CK
    WHERE pc.MEPR_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(pc.MEPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    UNION ALL
    SELECT nw.PRPR_ID
    FROM dbo.CMC_NWPR_RELATION AS nw
    JOIN scope_net AS k ON k.NWNW_ID = nw.NWNW_ID AND k.NWPR_PFX = nw.NWPR_PFX
    WHERE nw.NWPR_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(nw.NWPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_prov AS (
    SELECT DISTINCT p.PRPR_ID
    FROM prov_ref AS r
    JOIN dbo.CMC_PRPR_PROV AS p ON p.PRPR_ID = r.PRPR_ID
    WHERE ISNULL(p.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
-- Related active providers (groups, IPAs, payees, facilities), one level,
-- through relationships in force today. A PRDS payee is the provider itself
-- or one of its PRER providers, so PRER and PRAF cover it.
scope_prov_all AS (
    SELECT PRPR_ID FROM scope_prov
    UNION
    SELECT rp.PRPR_ID
    FROM dbo.CMC_PRER_RELATION AS r
    JOIN scope_prov AS k ON k.PRPR_ID = r.PRPR_ID
    JOIN dbo.CMC_PRPR_PROV AS rp ON rp.PRPR_ID = r.PRER_PRPR_ID
    WHERE r.PRER_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(r.PRER_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND ISNULL(rp.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    UNION
    SELECT fp.PRPR_ID
    FROM dbo.CMC_PRPR_PROV AS p
    JOIN scope_prov AS k ON k.PRPR_ID = p.PRPR_ID
    JOIN dbo.CMC_PRAF_FAC_AFFIL AS af ON af.PRCR_ID = p.PRCR_ID
    JOIN dbo.CMC_PRPR_PROV AS fp ON fp.PRPR_ID = af.PRPR_ID
    WHERE p.PRPR_ENTITY <> 'F'
      AND NULLIF(RTRIM(p.PRCR_ID), '') IS NOT NULL
      AND af.PRAF_FROM_DT <= CAST(GETDATE() AS date)
      AND ISNULL(af.PRAF_TO_DT, '99991231') >= CAST(GETDATE() AS date)
      AND ISNULL(fp.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
tax AS (
    SELECT p.PRPR_ID, t.MCTN_TYPE, t.MCTN_NAME,
           ROW_NUMBER() OVER (PARTITION BY p.PRPR_ID
                              ORDER BY CASE WHEN ti.MCTN_ID IS NOT NULL THEN 0 ELSE 1 END,
                                       t.MCTN_TYPE) AS RN
    FROM dbo.CMC_PRPR_PROV AS p
    JOIN scope_prov_all AS k ON k.PRPR_ID = p.PRPR_ID
    JOIN dbo.CMC_MCTN_TAX_NAME AS t ON t.MCTN_ID = p.MCTN_ID
    LEFT JOIN dbo.CMC_MCTI_TAX_INFO AS ti
           ON ti.MCTI_ENTY_ID = p.PRPR_ID AND ti.MCTN_ID = t.MCTN_ID AND ti.MCTN_TYPE = t.MCTN_TYPE
)
SELECT p.PRPR_ID, p.PRPR_ENTITY,
       CAST(CASE WHEN p.PRPR_ENTITY = 'P' THEN '1' ELSE '2' END AS char(1)) AS NM1_ENTITY_TYPE,
       p.PRPR_NAME, p.PRCP_ID, cp.PRCP_LAST_NAME, cp.PRCP_FIRST_NAME, cp.PRCP_MID_INIT,
       cp.PRCP_TITLE, fa.PRFA_NAME, p.PRPR_NPI, p.PRPR_TAXONOMY_CD, p.PRPR_MCTR_TYPE,
       p.PRCF_MCTR_SPEC, p.PRCF_MCTR_SPEC2, p.PRPR_MCTR_PRTY, p.TPCT_MCTR_TCAT,
       p.PRAD_ID, p.PRAD_TYPE_PRIM, p.PRAD_TYPE_CHECK, p.MCTN_ID, tn.MCTN_TYPE, tn.MCTN_NAME,
       p.PRPR_STS, p.PRPR_TERM_DT, p.PRPR_PREAUTH_IND, p.PRCR_ID
FROM dbo.CMC_PRPR_PROV AS p
JOIN scope_prov_all AS k ON k.PRPR_ID = p.PRPR_ID
LEFT JOIN dbo.CMC_PRCP_COMM_PRAC AS cp ON cp.PRCP_ID = p.PRCP_ID AND p.PRPR_ENTITY = 'P'
LEFT JOIN dbo.CMC_PRFA_FACILITY AS fa ON fa.PRPR_ID = p.PRPR_ID
LEFT JOIN tax AS tn ON tn.PRPR_ID = p.PRPR_ID AND tn.RN = 1;


-- table: provider_address
-- The addresses in force today of each provider's address owner (PRAD_ID),
-- for the providers of provider_denorm, flagged as primary practice (2010AA,
-- 2310C) and remittance (2010AB; FACETS defaults the remit type to the
-- primary).
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
),
scope_plan AS (
    SELECT p.NWST_PFX
    FROM dbo.CMC_CSPI_CS_PLAN AS p
    WHERE p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND EXISTS (SELECT 1 FROM dbo.CMC_MEPE_PRCS_ELIG AS e
                  WHERE e.GRGR_CK = p.GRGR_CK AND e.CSCS_ID = p.CSCS_ID
                    AND e.CSPD_CAT = p.CSPD_CAT AND e.CSPI_ID = p.CSPI_ID
                    AND e.MEPE_ELIG_IND = 'Y'
                    AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
scope_net AS (
    SELECT DISTINCT ns.NWNW_ID, ns.NWPR_PFX
    FROM dbo.CMC_NWST_NET_SET AS ns
    WHERE ns.NWST_PFX IN (SELECT NWST_PFX FROM scope_plan)
      AND ns.NWST_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(ns.NWST_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
prov_ref (PRPR_ID) AS (
    SELECT v.PRPR_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_claim AS k ON k.CLCL_ID = c.CLCL_ID
    CROSS APPLY (VALUES (c.PRPR_ID), (c.CLCL_PRPR_ID_REF), (c.CLCL_PRPR_ID_PCP),
                        (c.CLCL_PAYEE_PR_ID)) AS v (PRPR_ID)
    UNION ALL
    SELECT v.PRPR_ID
    FROM dbo.CMC_CLHP_HOSP AS h
    JOIN scope_claim AS k ON k.CLCL_ID = h.CLCL_ID
    CROSS APPLY (VALUES (h.CLHP_PRPR_ID_ADM), (h.CLHP_PRPR_ID_OPER),
                        (h.CLHP_PRPR_ID_OTH1)) AS v (PRPR_ID)
    UNION ALL
    SELECT f.CLMF_PRPR_ID_FAC FROM dbo.CMC_CLMF_MULT_FUNC AS f
    JOIN scope_claim AS k ON k.CLCL_ID = f.CLCL_ID
    UNION ALL
    SELECT CAST(s.CLSD_PRPR_ID_REND_NVL AS char(12)) FROM dbo.CMC_CLSD_SUPP_DATA AS s
    JOIN scope_claim AS k ON k.CLCL_ID = s.CLCL_ID
    UNION ALL
    SELECT CAST(d.CDSD_PRPR_ID_REND AS char(12)) FROM dbo.CMC_CDSD_SUPP_DATA AS d
    JOIN scope_claim AS k ON k.CLCL_ID = d.CLCL_ID
    UNION ALL
    SELECT v.PRPR_ID
    FROM dbo.CMC_CDML_CL_LINE AS l
    JOIN scope_claim AS k ON k.CLCL_ID = l.CLCL_ID
    CROSS APPLY (VALUES (l.CDML_UMAUTH_ID, l.CDML_AUTHSV_SEQ_NO),
                        (l.CDML_UMREF_ID, l.CDML_REFSV_SEQ_NO)) AS x (REF_ID, SEQ_NO)
    JOIN dbo.CMC_UMSV_SERVICES AS u ON u.UMUM_REF_ID = x.REF_ID AND u.UMSV_SEQ_NO = x.SEQ_NO
    CROSS APPLY (VALUES (u.UMSV_PRPR_ID_REQ), (u.UMSV_PRPR_ID_SVC),
                        (u.UMSV_PRPR_ID_FAC)) AS v (PRPR_ID)
    UNION ALL
    SELECT pc.PRPR_ID FROM dbo.CMC_MEPR_PRIM_PROV AS pc
    JOIN scope_member AS k ON k.MEME_CK = pc.MEME_CK
    WHERE pc.MEPR_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(pc.MEPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    UNION ALL
    SELECT nw.PRPR_ID
    FROM dbo.CMC_NWPR_RELATION AS nw
    JOIN scope_net AS k ON k.NWNW_ID = nw.NWNW_ID AND k.NWPR_PFX = nw.NWPR_PFX
    WHERE nw.NWPR_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(nw.NWPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_prov AS (
    SELECT DISTINCT p.PRPR_ID
    FROM prov_ref AS r
    JOIN dbo.CMC_PRPR_PROV AS p ON p.PRPR_ID = r.PRPR_ID
    WHERE ISNULL(p.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_prov_all AS (
    SELECT PRPR_ID FROM scope_prov
    UNION
    SELECT rp.PRPR_ID
    FROM dbo.CMC_PRER_RELATION AS r
    JOIN scope_prov AS k ON k.PRPR_ID = r.PRPR_ID
    JOIN dbo.CMC_PRPR_PROV AS rp ON rp.PRPR_ID = r.PRER_PRPR_ID
    WHERE r.PRER_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(r.PRER_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND ISNULL(rp.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    UNION
    SELECT fp.PRPR_ID
    FROM dbo.CMC_PRPR_PROV AS p
    JOIN scope_prov AS k ON k.PRPR_ID = p.PRPR_ID
    JOIN dbo.CMC_PRAF_FAC_AFFIL AS af ON af.PRCR_ID = p.PRCR_ID
    JOIN dbo.CMC_PRPR_PROV AS fp ON fp.PRPR_ID = af.PRPR_ID
    WHERE p.PRPR_ENTITY <> 'F'
      AND NULLIF(RTRIM(p.PRCR_ID), '') IS NOT NULL
      AND af.PRAF_FROM_DT <= CAST(GETDATE() AS date)
      AND ISNULL(af.PRAF_TO_DT, '99991231') >= CAST(GETDATE() AS date)
      AND ISNULL(fp.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
)
SELECT p.PRPR_ID, a.PRAD_ID, a.PRAD_TYPE, a.PRAD_EFF_DT, a.PRAD_TERM_DT,
       CAST(CASE WHEN a.PRAD_TYPE = p.PRAD_TYPE_PRIM THEN 'Y' ELSE 'N' END AS char(1)) AS IS_PRIMARY,
       CAST(CASE WHEN a.PRAD_TYPE = COALESCE(NULLIF(RTRIM(p.PRAD_TYPE_CHECK), ''), p.PRAD_TYPE_PRIM)
                 THEN 'Y' ELSE 'N' END AS char(1))                                    AS IS_REMIT,
       a.PRAD_PRACTICE_IND, a.PRAD_TYPE_MAIL,
       a.PRAD_ADDR1, a.PRAD_ADDR2, a.PRAD_CITY, a.PRAD_STATE, a.PRAD_ZIP, a.PRAD_COUNTY,
       a.PRAD_CTRY_CD, a.PRAD_PHONE
FROM dbo.CMC_PRPR_PROV AS p
JOIN scope_prov_all AS k ON k.PRPR_ID = p.PRPR_ID
JOIN dbo.CMC_PRAD_ADDRESS AS a ON a.PRAD_ID = p.PRAD_ID
WHERE a.PRAD_EFF_DT <= CAST(GETDATE() AS date)
  AND ISNULL(a.PRAD_TERM_DT, '99991231') >= CAST(GETDATE() AS date);


-- table: provider_network
-- Network participation in force today of the providers of provider_denorm,
-- with the network and the agreement (contract) in force today. In network
-- for a member: a row whose NWNW_ID + NWPR_PFX is in the member plan's
-- plan_network rows. Only participation in those networks (the networks of
-- the plans in scope) is kept; a provider's other networks decide nothing.
WITH scope_member AS (
    SELECT DISTINCT e.MEME_CK FROM dbo.CMC_MEPE_PRCS_ELIG AS e
    WHERE e.MEPE_ELIG_IND = 'Y'
      AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_claim AS (
    SELECT c.CLCL_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_member AS m ON m.MEME_CK = c.MEME_CK
    WHERE c.CLCL_LOW_SVC_DT >= DATEADD(month, -24, CAST(GETDATE() AS date))
      AND EXISTS (SELECT 1 FROM dbo.CMC_CDML_CL_LINE AS l WHERE l.CLCL_ID = c.CLCL_ID)
),
scope_plan AS (
    SELECT p.NWST_PFX
    FROM dbo.CMC_CSPI_CS_PLAN AS p
    WHERE p.CSPI_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(p.CSPI_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND EXISTS (SELECT 1 FROM dbo.CMC_MEPE_PRCS_ELIG AS e
                  WHERE e.GRGR_CK = p.GRGR_CK AND e.CSCS_ID = p.CSCS_ID
                    AND e.CSPD_CAT = p.CSPD_CAT AND e.CSPI_ID = p.CSPI_ID
                    AND e.MEPE_ELIG_IND = 'Y'
                    AND e.MEPE_EFF_DT <= CAST(GETDATE() AS date)
                    AND ISNULL(e.MEPE_TERM_DT, '99991231') >= CAST(GETDATE() AS date))
),
scope_net AS (
    SELECT DISTINCT ns.NWNW_ID, ns.NWPR_PFX
    FROM dbo.CMC_NWST_NET_SET AS ns
    WHERE ns.NWST_PFX IN (SELECT NWST_PFX FROM scope_plan)
      AND ns.NWST_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(ns.NWST_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
prov_ref (PRPR_ID) AS (
    SELECT v.PRPR_ID
    FROM dbo.CMC_CLCL_CLAIM AS c
    JOIN scope_claim AS k ON k.CLCL_ID = c.CLCL_ID
    CROSS APPLY (VALUES (c.PRPR_ID), (c.CLCL_PRPR_ID_REF), (c.CLCL_PRPR_ID_PCP),
                        (c.CLCL_PAYEE_PR_ID)) AS v (PRPR_ID)
    UNION ALL
    SELECT v.PRPR_ID
    FROM dbo.CMC_CLHP_HOSP AS h
    JOIN scope_claim AS k ON k.CLCL_ID = h.CLCL_ID
    CROSS APPLY (VALUES (h.CLHP_PRPR_ID_ADM), (h.CLHP_PRPR_ID_OPER),
                        (h.CLHP_PRPR_ID_OTH1)) AS v (PRPR_ID)
    UNION ALL
    SELECT f.CLMF_PRPR_ID_FAC FROM dbo.CMC_CLMF_MULT_FUNC AS f
    JOIN scope_claim AS k ON k.CLCL_ID = f.CLCL_ID
    UNION ALL
    SELECT CAST(s.CLSD_PRPR_ID_REND_NVL AS char(12)) FROM dbo.CMC_CLSD_SUPP_DATA AS s
    JOIN scope_claim AS k ON k.CLCL_ID = s.CLCL_ID
    UNION ALL
    SELECT CAST(d.CDSD_PRPR_ID_REND AS char(12)) FROM dbo.CMC_CDSD_SUPP_DATA AS d
    JOIN scope_claim AS k ON k.CLCL_ID = d.CLCL_ID
    UNION ALL
    SELECT v.PRPR_ID
    FROM dbo.CMC_CDML_CL_LINE AS l
    JOIN scope_claim AS k ON k.CLCL_ID = l.CLCL_ID
    CROSS APPLY (VALUES (l.CDML_UMAUTH_ID, l.CDML_AUTHSV_SEQ_NO),
                        (l.CDML_UMREF_ID, l.CDML_REFSV_SEQ_NO)) AS x (REF_ID, SEQ_NO)
    JOIN dbo.CMC_UMSV_SERVICES AS u ON u.UMUM_REF_ID = x.REF_ID AND u.UMSV_SEQ_NO = x.SEQ_NO
    CROSS APPLY (VALUES (u.UMSV_PRPR_ID_REQ), (u.UMSV_PRPR_ID_SVC),
                        (u.UMSV_PRPR_ID_FAC)) AS v (PRPR_ID)
    UNION ALL
    SELECT pc.PRPR_ID FROM dbo.CMC_MEPR_PRIM_PROV AS pc
    JOIN scope_member AS k ON k.MEME_CK = pc.MEME_CK
    WHERE pc.MEPR_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(pc.MEPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    UNION ALL
    SELECT nw.PRPR_ID
    FROM dbo.CMC_NWPR_RELATION AS nw
    JOIN scope_net AS k ON k.NWNW_ID = nw.NWNW_ID AND k.NWPR_PFX = nw.NWPR_PFX
    WHERE nw.NWPR_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(nw.NWPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_prov AS (
    SELECT DISTINCT p.PRPR_ID
    FROM prov_ref AS r
    JOIN dbo.CMC_PRPR_PROV AS p ON p.PRPR_ID = r.PRPR_ID
    WHERE ISNULL(p.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
scope_prov_all AS (
    SELECT PRPR_ID FROM scope_prov
    UNION
    SELECT rp.PRPR_ID
    FROM dbo.CMC_PRER_RELATION AS r
    JOIN scope_prov AS k ON k.PRPR_ID = r.PRPR_ID
    JOIN dbo.CMC_PRPR_PROV AS rp ON rp.PRPR_ID = r.PRER_PRPR_ID
    WHERE r.PRER_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(r.PRER_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
      AND ISNULL(rp.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
    UNION
    SELECT fp.PRPR_ID
    FROM dbo.CMC_PRPR_PROV AS p
    JOIN scope_prov AS k ON k.PRPR_ID = p.PRPR_ID
    JOIN dbo.CMC_PRAF_FAC_AFFIL AS af ON af.PRCR_ID = p.PRCR_ID
    JOIN dbo.CMC_PRPR_PROV AS fp ON fp.PRPR_ID = af.PRPR_ID
    WHERE p.PRPR_ENTITY <> 'F'
      AND NULLIF(RTRIM(p.PRCR_ID), '') IS NOT NULL
      AND af.PRAF_FROM_DT <= CAST(GETDATE() AS date)
      AND ISNULL(af.PRAF_TO_DT, '99991231') >= CAST(GETDATE() AS date)
      AND ISNULL(fp.PRPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
participation AS (
    SELECT nw.PRPR_ID, nw.NWNW_ID, nw.NWPR_PFX, nw.NWPR_PCP_IND, nw.NWPR_EFF_DT, nw.NWPR_TERM_DT,
           nw.AGAG_ID, nw.NWPR_ACC_PAT_IND, nw.NWPR_MIN_AGE, nw.NWPR_MAX_AGE, nw.NWPR_SEX
    FROM dbo.CMC_NWPR_RELATION AS nw
    JOIN scope_prov_all AS k ON k.PRPR_ID = nw.PRPR_ID
    JOIN scope_net AS sn ON sn.NWNW_ID = nw.NWNW_ID AND sn.NWPR_PFX = nw.NWPR_PFX
    WHERE nw.NWPR_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(nw.NWPR_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
),
agreement AS (
    SELECT g.AGAG_ID, g.AGAG_EFF_DT, g.AGAG_TERM_DT, g.AGAG_MCTR_TYPE, g.AGAG_DESC,
           ROW_NUMBER() OVER (PARTITION BY g.AGAG_ID ORDER BY g.AGAG_EFF_DT DESC) AS RN
    FROM dbo.CMC_AGAG_AGREEMENT AS g
    WHERE g.AGAG_ID IN (SELECT AGAG_ID FROM participation)
      AND g.AGAG_EFF_DT <= CAST(GETDATE() AS date)
      AND ISNULL(g.AGAG_TERM_DT, '99991231') >= CAST(GETDATE() AS date)
)
SELECT nw.PRPR_ID, nw.NWNW_ID, nw.NWPR_PFX, nw.NWPR_PCP_IND, nw.NWPR_EFF_DT, nw.NWPR_TERM_DT,
       nw.AGAG_ID, nw.NWPR_ACC_PAT_IND, nw.NWPR_MIN_AGE, nw.NWPR_MAX_AGE, nw.NWPR_SEX,
       n.NWNW_NAME, n.NWNW_MCTR_TYPE,
       ag.AGAG_EFF_DT, ag.AGAG_TERM_DT, ag.AGAG_MCTR_TYPE, ag.AGAG_DESC
FROM participation AS nw
LEFT JOIN dbo.CMC_NWNW_NETWORK AS n ON n.NWNW_ID = nw.NWNW_ID
LEFT JOIN agreement AS ag ON ag.AGAG_ID = nw.AGAG_ID AND ag.RN = 1;
