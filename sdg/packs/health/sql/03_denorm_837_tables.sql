-- 03_denorm_837_tables.sql: add-only. Tables that let generated EDI 837
-- (professional and institutional) claims agree across member, plan, benefit
-- and provider.
--
-- For the customer's SDM platform database, schema sdm. To use another
-- schema, replace "sdm." throughout. sdm.member_denorm and sdm.claims_denorm
-- are not touched. Each table is created only if absent and each index only
-- if missing, so the script can be run again safely. Nothing is dropped,
-- renamed, retyped or deleted.
--
-- Column names and types are FACETS's own (from the FACETS data dictionary);
-- the few derived columns are noted. Tables are narrow and keyed for the
-- joins the generator makes (by MEME_CK, SBSB_CK, PRPR_ID, PDPD_ID, CSPI_ID,
-- CLCL_ID + CDML_SEQ_NO). Fill them with 04_denorm_837_extract.sql.
-- Design and 837 crosswalk: docs/facets-837-sources.md.
--
-- Tables:
--   Provider : provider_denorm, provider_address, provider_network, provider_relation
--   Member   : member_elig, subscriber_denorm, member_address, member_pcp, member_cob
--   Plan     : plan_denorm, plan_network, plan_benefit_denorm, plan_accumulator,
--              service_code_map, place_of_service
--   Claims   : claims_header_ext_denorm, claims_detail_denorm, claims_diag,
--              claims_icd_proc, claims_ub_code, claims_auth

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

-- sdm.provider_denorm
-- Grain: One row per provider (PRPR_ID): the non-dated facts plus names, NPI, taxonomy and tax ID. Addresses, networks and relationships are dated and live in their own tables.
IF OBJECT_ID('sdm.provider_denorm', 'U') IS NULL
CREATE TABLE sdm.provider_denorm (
    PRPR_ID                  char(12)       NOT NULL,
    PRPR_ENTITY              char(1)        NULL,
    NM1_ENTITY_TYPE          char(1)        NULL,
    PRPR_NAME                char(55)       NULL,
    PRCP_ID                  char(12)       NULL,
    PRCP_LAST_NAME           char(35)       NULL,
    PRCP_FIRST_NAME          char(15)       NULL,
    PRCP_MID_INIT            char(1)        NULL,
    PRCP_TITLE               char(10)       NULL,
    PRFA_NAME                char(50)       NULL,
    PRPR_NPI                 char(10)       NULL,
    PRPR_TAXONOMY_CD         char(12)       NULL,
    PRPR_MCTR_TYPE           char(4)        NULL,
    PRCF_MCTR_SPEC           char(4)        NULL,
    PRCF_MCTR_SPEC2          char(4)        NULL,
    PRPR_MCTR_PRTY           char(4)        NULL,
    TPCT_MCTR_TCAT           char(1)        NULL,
    PRAD_ID                  char(12)       NULL,
    PRAD_TYPE_PRIM           char(3)        NULL,
    PRAD_TYPE_CHECK          char(3)        NULL,
    MCTN_ID                  char(9)        NULL,
    MCTN_TYPE                char(1)        NULL,
    MCTN_NAME                char(55)       NULL,
    PRPR_STS                 char(2)        NULL,
    PRPR_TERM_DT             datetime       NULL,
    PRPR_PREAUTH_IND         char(1)        NULL,
    PRCR_ID                  char(12)       NULL,
    CONSTRAINT PK_provider_denorm PRIMARY KEY CLUSTERED (PRPR_ID)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_provider_denorm_npi' AND object_id = OBJECT_ID('sdm.provider_denorm'))
CREATE INDEX ix_provider_denorm_npi ON sdm.provider_denorm (PRPR_NPI) INCLUDE (PRPR_ID, PRPR_ENTITY);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_provider_denorm_spec' AND object_id = OBJECT_ID('sdm.provider_denorm'))
CREATE INDEX ix_provider_denorm_spec ON sdm.provider_denorm (PRPR_ENTITY, PRCF_MCTR_SPEC) INCLUDE (PRPR_ID);
GO

-- sdm.provider_address
-- Grain: One row per provider x address type x effective date (PRPR_ID, PRAD_TYPE, PRAD_EFF_DT). Shared addresses (PRAD_ID of a group) are copied onto each provider that uses them, so a lookup never needs PRAD_ID.
IF OBJECT_ID('sdm.provider_address', 'U') IS NULL
CREATE TABLE sdm.provider_address (
    PRPR_ID                  char(12)       NOT NULL,
    PRAD_ID                  char(12)       NULL,
    PRAD_TYPE                char(3)        NOT NULL,
    PRAD_EFF_DT              datetime       NOT NULL,
    PRAD_TERM_DT             datetime       NULL,
    IS_PRIMARY               char(1)        NULL,
    IS_REMIT                 char(1)        NULL,
    PRAD_PRACTICE_IND        char(1)        NULL,
    PRAD_TYPE_MAIL           char(3)        NULL,
    PRAD_ADDR1               char(40)       NULL,
    PRAD_ADDR2               char(40)       NULL,
    PRAD_CITY                char(19)       NULL,
    PRAD_STATE               char(2)        NULL,
    PRAD_ZIP                 char(11)       NULL,
    PRAD_COUNTY              char(20)       NULL,
    PRAD_CTRY_CD             char(4)        NULL,
    PRAD_PHONE               char(20)       NULL,
    CONSTRAINT PK_provider_address PRIMARY KEY CLUSTERED (PRPR_ID, PRAD_TYPE, PRAD_EFF_DT)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_provider_address_role' AND object_id = OBJECT_ID('sdm.provider_address'))
CREATE INDEX ix_provider_address_role ON sdm.provider_address (PRPR_ID, IS_PRIMARY, IS_REMIT, PRAD_EFF_DT) INCLUDE (PRAD_TERM_DT, PRAD_ZIP, PRAD_STATE);
GO

-- sdm.provider_network
-- Grain: One row per provider network participation (PRPR_ID, NWNW_ID, NWPR_PFX, NWPR_PCP_IND, NWPR_EFF_DT), with the network and the agreement (the latest agreement row overlapping the participation dates).
IF OBJECT_ID('sdm.provider_network', 'U') IS NULL
CREATE TABLE sdm.provider_network (
    PRPR_ID                  char(12)       NOT NULL,
    NWNW_ID                  char(12)       NOT NULL,
    NWPR_PFX                 char(4)        NOT NULL,
    NWPR_PCP_IND             char(1)        NOT NULL,
    NWPR_EFF_DT              datetime       NOT NULL,
    NWPR_TERM_DT             datetime       NULL,
    AGAG_ID                  char(12)       NULL,
    NWPR_ACC_PAT_IND         char(1)        NULL,
    NWPR_MIN_AGE             smallint       NULL,
    NWPR_MAX_AGE             smallint       NULL,
    NWPR_SEX                 char(1)        NULL,
    NWNW_NAME                char(50)       NULL,
    NWNW_MCTR_TYPE           char(4)        NULL,
    AGAG_EFF_DT              datetime       NULL,
    AGAG_TERM_DT             datetime       NULL,
    AGAG_MCTR_TYPE           char(4)        NULL,
    AGAG_DESC                varchar(70)    NULL,
    CONSTRAINT PK_provider_network PRIMARY KEY CLUSTERED (PRPR_ID, NWNW_ID, NWPR_PFX, NWPR_PCP_IND, NWPR_EFF_DT)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_provider_network_net' AND object_id = OBJECT_ID('sdm.provider_network'))
CREATE INDEX ix_provider_network_net ON sdm.provider_network (NWNW_ID, NWPR_PFX, NWPR_EFF_DT) INCLUDE (PRPR_ID, NWPR_TERM_DT, NWPR_PCP_IND, AGAG_ID);
GO

-- sdm.provider_relation
-- Grain: One row per dated provider relationship: PRER (practitioner/facility to group, IPA or sponsor), PRDS (claims payee, resolved to a provider ID) and PRAF (practitioner to affiliated facility). REL_SOURCE says which.
IF OBJECT_ID('sdm.provider_relation', 'U') IS NULL
CREATE TABLE sdm.provider_relation (
    PRPR_ID                  char(12)       NOT NULL,
    REL_SOURCE               char(4)        NULL,
    REL_ENTITY               char(1)        NULL,
    REL_EFF_DT               datetime       NULL,
    REL_TERM_DT              datetime       NULL,
    REL_PRPR_ID              char(12)       NULL,
    REL_PRPR_ENTITY          char(1)        NULL,
    REL_PRPR_NAME            char(55)       NULL,
    REL_PRPR_NPI             char(10)       NULL,
    REL_PRPR_TAXONOMY_CD     char(12)       NULL,
    REL_MCTN_ID              char(9)        NULL,
    PRAF_PRIM_IND            char(1)        NULL,
    PRAF_STS                 char(2)        NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cx_provider_relation' AND object_id = OBJECT_ID('sdm.provider_relation'))
CREATE CLUSTERED INDEX cx_provider_relation ON sdm.provider_relation (PRPR_ID, REL_SOURCE, REL_EFF_DT);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_provider_relation_rel' AND object_id = OBJECT_ID('sdm.provider_relation'))
CREATE INDEX ix_provider_relation_rel ON sdm.provider_relation (REL_PRPR_ID, REL_SOURCE) INCLUDE (PRPR_ID, REL_EFF_DT, REL_TERM_DT);
GO

-- sdm.member_elig
-- Grain: One row per member eligibility span (MEME_CK, CSPD_CAT, MEPE_EFF_DT). The bridge from member_denorm to plans: it carries CSCS_ID, which member_denorm lacks and which CMC_CSPI_CS_PLAN and CMC_CSCS_CLASS need.
IF OBJECT_ID('sdm.member_elig', 'U') IS NULL
CREATE TABLE sdm.member_elig (
    MEME_CK                  int            NOT NULL,
    CSPD_CAT                 char(1)        NOT NULL,
    MEPE_EFF_DT              datetime       NOT NULL,
    MEPE_TERM_DT             datetime       NULL,
    MEPE_ELIG_IND            char(1)        NULL,
    GRGR_CK                  int            NULL,
    SGSG_CK                  int            NULL,
    CSCS_ID                  char(4)        NULL,
    CSPI_ID                  char(8)        NULL,
    PDPD_ID                  char(8)        NULL,
    MEPE_FI                  char(1)        NULL,
    MEPE_PLAN_ENTRY_DT       datetime       NULL,
    CONSTRAINT PK_member_elig PRIMARY KEY CLUSTERED (MEME_CK, CSPD_CAT, MEPE_EFF_DT)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_member_elig_plan' AND object_id = OBJECT_ID('sdm.member_elig'))
CREATE INDEX ix_member_elig_plan ON sdm.member_elig (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID) INCLUDE (MEME_CK, MEPE_EFF_DT, MEPE_TERM_DT, PDPD_ID);
GO

-- sdm.subscriber_denorm
-- Grain: One row per subscriber (SBSB_CK): 2000B / 2010BA, the subscriber's name, group number and home address.
IF OBJECT_ID('sdm.subscriber_denorm', 'U') IS NULL
CREATE TABLE sdm.subscriber_denorm (
    SBSB_CK                  int            NOT NULL,
    GRGR_CK                  int            NULL,
    GRGR_ID                  char(8)        NULL,
    SBSB_ID                  char(9)        NULL,
    SBSB_LAST_NAME           char(35)       NULL,
    SBSB_FIRST_NAME          char(15)       NULL,
    SBSB_MID_INIT            char(1)        NULL,
    SBAD_TYPE_HOME           char(1)        NULL,
    SBAD_ADDR1               char(40)       NULL,
    SBAD_ADDR2               char(40)       NULL,
    SBAD_CITY                char(19)       NULL,
    SBAD_STATE               char(2)        NULL,
    SBAD_ZIP                 char(11)       NULL,
    SBAD_COUNTY              char(20)       NULL,
    SBAD_CTRY_CD             char(4)        NULL,
    CONSTRAINT PK_subscriber_denorm PRIMARY KEY CLUSTERED (SBSB_CK)
);
GO

-- sdm.member_address
-- Grain: One row per member x address role (MEME_CK, ADDR_ROLE = HOME or MAIL), resolved the way Facets does: the member's own address type, else the subscriber's home. 2010CA patient address (and 2010BA when the member is the subscriber).
IF OBJECT_ID('sdm.member_address', 'U') IS NULL
CREATE TABLE sdm.member_address (
    MEME_CK                  int            NOT NULL,
    ADDR_ROLE                char(4)        NOT NULL,
    SBSB_CK                  int            NULL,
    SBAD_TYPE                char(1)        NULL,
    FROM_SUBSCRIBER          char(1)        NULL,
    SBAD_ADDR1               char(40)       NULL,
    SBAD_ADDR2               char(40)       NULL,
    SBAD_CITY                char(19)       NULL,
    SBAD_STATE               char(2)        NULL,
    SBAD_ZIP                 char(11)       NULL,
    SBAD_COUNTY              char(20)       NULL,
    SBAD_CTRY_CD             char(4)        NULL,
    SBAD_PHONE               char(20)       NULL,
    CONSTRAINT PK_member_address PRIMARY KEY CLUSTERED (MEME_CK, ADDR_ROLE)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_member_address_zip' AND object_id = OBJECT_ID('sdm.member_address'))
CREATE INDEX ix_member_address_zip ON sdm.member_address (SBAD_ZIP) INCLUDE (MEME_CK, SBAD_STATE);
GO

-- sdm.member_pcp
-- Grain: One row per member PCP assignment (MEME_CK, MEPR_PCP_TYPE, MEPR_EFF_DT), with the PCP's NPI and entity.
IF OBJECT_ID('sdm.member_pcp', 'U') IS NULL
CREATE TABLE sdm.member_pcp (
    MEME_CK                  int            NOT NULL,
    MEPR_PCP_TYPE            char(2)        NOT NULL,
    MEPR_EFF_DT              datetime       NOT NULL,
    MEPR_TERM_DT             datetime       NULL,
    GRGR_CK                  int            NULL,
    PRPR_ID                  char(12)       NULL,
    PRPR_NPI                 char(10)       NULL,
    PRPR_ENTITY              char(1)        NULL,
    CONSTRAINT PK_member_pcp PRIMARY KEY CLUSTERED (MEME_CK, MEPR_PCP_TYPE, MEPR_EFF_DT)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_member_pcp_prov' AND object_id = OBJECT_ID('sdm.member_pcp'))
CREATE INDEX ix_member_pcp_prov ON sdm.member_pcp (PRPR_ID) INCLUDE (MEME_CK, MEPR_EFF_DT, MEPR_TERM_DT);
GO

-- sdm.member_cob
-- Grain: One row per member other-coverage span (MEME_CK, MECB_INSUR_TYPE, MECB_INSUR_ORDER, MECB_MCTR_STYP, MECB_EFF_DT), with the other carrier's name and address: 2000B SBR01, 2320, 2330A, 2330B.
IF OBJECT_ID('sdm.member_cob', 'U') IS NULL
CREATE TABLE sdm.member_cob (
    MEME_CK                  int            NOT NULL,
    MECB_INSUR_TYPE          char(1)        NULL,
    MECB_INSUR_ORDER         char(1)        NULL,
    MECB_MCTR_STYP           char(4)        NULL,
    MECB_EFF_DT              datetime       NULL,
    MECB_TERM_DT             datetime       NULL,
    MECB_POLICY_ID           varchar(25)    NULL,
    MECB_PRIM_LAST_NM        varchar(35)    NULL,
    MECB_PRIM_FIRST_NM       varchar(15)    NULL,
    MECB_PRIM_ID             varchar(40)    NULL,
    MECB_MCTR_MSP            char(4)        NULL,
    MCRE_ID                  char(9)        NULL,
    MCRE_NAME                char(50)       NULL,
    MCRE_ADDR1               char(40)       NULL,
    MCRE_ADDR2               char(40)       NULL,
    MCRE_CITY                char(19)       NULL,
    MCRE_STATE               char(2)        NULL,
    MCRE_ZIP                 char(11)       NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cx_member_cob' AND object_id = OBJECT_ID('sdm.member_cob'))
CREATE CLUSTERED INDEX cx_member_cob ON sdm.member_cob (MEME_CK, MECB_EFF_DT);
GO

-- sdm.plan_denorm
-- Grain: One row per class/plan row (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID, CSPI_EFF_DT): product, network set, service conversion prefix, line of business and the payer (2010BB) in force at the plan's start.
IF OBJECT_ID('sdm.plan_denorm', 'U') IS NULL
CREATE TABLE sdm.plan_denorm (
    GRGR_CK                  int            NOT NULL,
    CSCS_ID                  char(4)        NOT NULL,
    CSPD_CAT                 char(1)        NOT NULL,
    CSPI_ID                  char(8)        NOT NULL,
    CSPI_EFF_DT              datetime       NOT NULL,
    CSPI_TERM_DT             datetime       NULL,
    PDPD_ID                  char(8)        NULL,
    NWST_PFX                 char(4)        NULL,
    CSPI_PDPD_BEG_MMDD       smallint       NULL,
    PLDS_DESC                char(70)       NULL,
    TPCT_PFX                 char(4)        NULL,
    LOBD_ID                  char(4)        NULL,
    PDDS_DESC                varchar(70)    NULL,
    PDDS_PROD_TYPE           char(1)        NULL,
    PDDS_MCTR_BCAT           char(4)        NULL,
    PDDS_UM_IND              char(1)        NULL,
    PDDS_OON_IND             char(1)        NULL,
    PYPY_ID                  char(8)        NULL,
    PYPY_PAYOR_NAME          char(50)       NULL,
    PYPY_NATL_PLAN_ID        varchar(80)    NULL,
    PYPY_NAIC_CODE           varchar(50)    NULL,
    PYPY_TAX_ID              char(9)        NULL,
    PYPY_ADDR1               char(40)       NULL,
    PYPY_ADDR2               char(40)       NULL,
    PYPY_CITY                char(19)       NULL,
    PYPY_STATE               char(2)        NULL,
    PYPY_ZIP                 char(11)       NULL,
    CONSTRAINT PK_plan_denorm PRIMARY KEY CLUSTERED (GRGR_CK, CSCS_ID, CSPD_CAT, CSPI_ID, CSPI_EFF_DT)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_plan_denorm_pdpd' AND object_id = OBJECT_ID('sdm.plan_denorm'))
CREATE INDEX ix_plan_denorm_pdpd ON sdm.plan_denorm (PDPD_ID) INCLUDE (CSPI_ID, NWST_PFX, TPCT_PFX);
GO

-- sdm.plan_network
-- Grain: One row per network-set row (NWST_PFX, NWST_EFF_DT, NWST_SEQ_NO): which networks a plan uses, in-network vs participating, and the tier.
IF OBJECT_ID('sdm.plan_network', 'U') IS NULL
CREATE TABLE sdm.plan_network (
    NWST_PFX                 char(4)        NOT NULL,
    NWST_EFF_DT              datetime       NOT NULL,
    NWST_SEQ_NO              smallint       NOT NULL,
    NWST_TERM_DT             datetime       NULL,
    NWNW_ID                  char(12)       NULL,
    NWPR_PFX                 char(4)        NULL,
    NWST_PR_NW_STS           char(1)        NULL,
    PDVC_TIER                smallint       NULL,
    NWNW_NAME                char(50)       NULL,
    NWNW_MCTR_TYPE           char(4)        NULL,
    CONSTRAINT PK_plan_network PRIMARY KEY CLUSTERED (NWST_PFX, NWST_EFF_DT, NWST_SEQ_NO)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_plan_network_net' AND object_id = OBJECT_ID('sdm.plan_network'))
CREATE INDEX ix_plan_network_net ON sdm.plan_network (NWNW_ID, NWPR_PFX) INCLUDE (NWST_PFX, NWST_EFF_DT, NWST_TERM_DT, PDVC_TIER);
GO

-- sdm.plan_benefit_denorm
-- Grain: One row per product variable component x covered Service ID x cost-share tier: (PDPD_ID, PDVC_TIER, PDVC_TYPE, PDVC_SEQ_NO, PDVC_EFF_DT, SESE_ID, SEPY_EFF_DT, SETR_TIER_NO). BEN_EFF_DT / BEN_TERM_DT is where the PDVC and SEPY windows overlap; a service is covered for a member on svc_dt when a row exists for the member's PDPD_ID and network tier with BEN_EFF_DT <= svc_dt <= BEN_TERM_DT and SESE_RULE_TYPE is not Disallow.
IF OBJECT_ID('sdm.plan_benefit_denorm', 'U') IS NULL
CREATE TABLE sdm.plan_benefit_denorm (
    PDPD_ID                  char(8)        NOT NULL,
    PDVC_TIER                smallint       NULL,
    PDVC_TYPE                char(1)        NULL,
    PDVC_SEQ_NO              smallint       NULL,
    PDVC_EFF_DT              datetime       NULL,
    PDVC_TERM_DT             datetime       NULL,
    PDVC_PR_PCP              char(1)        NULL,
    PDVC_PR_IN               char(1)        NULL,
    PDVC_PR_PAR              char(1)        NULL,
    PDVC_PR_NONPAR           char(1)        NULL,
    PDVC_PC_NR               char(1)        NULL,
    PDVC_PC_OBT              char(1)        NULL,
    PDVC_PC_VIOL             char(1)        NULL,
    PDVC_REF_NR              char(1)        NULL,
    PDVC_REF_OBT             char(1)        NULL,
    PDVC_REF_VIOL            char(1)        NULL,
    PDVC_LOBD_PTR            char(1)        NULL,
    SEPY_PFX                 char(4)        NULL,
    DEDE_PFX                 char(4)        NULL,
    LTLT_PFX                 char(4)        NULL,
    SEPY_EFF_DT              datetime       NULL,
    SEPY_TERM_DT             datetime       NULL,
    BEN_EFF_DT               datetime       NULL,
    BEN_TERM_DT              datetime       NULL,
    SESE_ID                  char(4)        NULL,
    SESE_RULE                char(3)        NULL,
    SEDS_DESC                char(70)       NULL,
    SEDS_TYPE                char(1)        NULL,
    SESE_RULE_TYPE           char(1)        NULL,
    SESE_CALC_IND            char(1)        NULL,
    SESE_VALID_SEX           char(1)        NULL,
    SESE_MIN_AGE             smallint       NULL,
    SESE_MAX_AGE             smallint       NULL,
    SESE_COV_TYPE            char(1)        NULL,
    SETR_TIER_NO             smallint       NULL,
    SETR_ALLOW_AMT           money          NULL,
    SETR_ALLOW_CTR           smallint       NULL,
    SETR_COPAY_AMT           money          NULL,
    SETR_COIN_PCT            money          NULL,
    ACAC_ACC_NO              smallint       NULL,
    SEDF_UM_REQ_IND          char(1)        NULL,
    SEDF_UM_BP_REF_IND       char(1)        NULL,
    SEDF_CAP_IND             char(1)        NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cx_plan_benefit_denorm' AND object_id = OBJECT_ID('sdm.plan_benefit_denorm'))
CREATE CLUSTERED INDEX cx_plan_benefit_denorm ON sdm.plan_benefit_denorm (PDPD_ID, SESE_ID, BEN_EFF_DT);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_plan_benefit_tier' AND object_id = OBJECT_ID('sdm.plan_benefit_denorm'))
CREATE INDEX ix_plan_benefit_tier ON sdm.plan_benefit_denorm (PDPD_ID, PDVC_TIER, BEN_EFF_DT) INCLUDE (BEN_TERM_DT, SESE_ID, SESE_RULE, SESE_RULE_TYPE);
GO

-- sdm.plan_accumulator
-- Grain: One row per product variable component x accumulator (x limited Service ID): deductibles (ACCUM_KIND = 'DED', from DEDE) and limits / stop-loss (ACCUM_KIND = 'LMT', from LTLT + LTSE).
IF OBJECT_ID('sdm.plan_accumulator', 'U') IS NULL
CREATE TABLE sdm.plan_accumulator (
    PDPD_ID                  char(8)        NOT NULL,
    PDVC_TIER                smallint       NULL,
    PDVC_TYPE                char(1)        NULL,
    PDVC_SEQ_NO              smallint       NULL,
    PDVC_EFF_DT              datetime       NULL,
    PDVC_TERM_DT             datetime       NULL,
    ACCUM_KIND               char(3)        NULL,
    ACCUM_PFX                char(4)        NULL,
    ACAC_ACC_NO              smallint       NULL,
    ACCUM_DESC               varchar(70)    NULL,
    DEDE_RULE                smallint       NULL,
    DEDE_PERIOD_IND          char(1)        NULL,
    DEDE_MEME_AMT            money          NULL,
    DEDE_FAM_AMT             money          NULL,
    LTLT_CAT                 char(1)        NULL,
    LTLT_LEVEL               char(1)        NULL,
    LTLT_PERIOD_IND          char(1)        NULL,
    LTLT_RULE                char(1)        NULL,
    LTLT_IX_IND              char(1)        NULL,
    LTLT_IX_TYPE             char(1)        NULL,
    LTLT_AMT1                money          NULL,
    SESE_ID                  char(4)        NULL,
    LTSE_WT_CTR              int            NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cx_plan_accumulator' AND object_id = OBJECT_ID('sdm.plan_accumulator'))
CREATE CLUSTERED INDEX cx_plan_accumulator ON sdm.plan_accumulator (PDPD_ID, ACCUM_KIND, ACAC_ACC_NO);
GO

-- sdm.service_code_map
-- Grain: One row per conversion range: procedure ranges (MAP_KIND = 'PROC', CMC_TPCT_CPT_CONV), revenue-code ranges ('REV', CMC_RCCT_CONV) and default services ('SVC', CMC_SECT_CONV), for the TPCT_PFX values of the plans in scope. Procedure / revenue code + setting -> Service ID.
IF OBJECT_ID('sdm.service_code_map', 'U') IS NULL
CREATE TABLE sdm.service_code_map (
    TPCT_PFX                 char(4)        NOT NULL,
    MAP_KIND                 char(4)        NULL,
    TPCT_MCTR_TCAT           char(1)        NULL,
    MCTR_SETG                char(1)        NULL,
    CODE_LOW                 char(7)        NULL,
    CODE_HIGH                char(7)        NULL,
    TPCT_MODIFIER            char(2)        NULL,
    SESE_ID                  char(4)        NULL,
    CSPD_CAT                 char(1)        NULL,
    RPTC_RPT_CAT             char(4)        NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'cx_service_code_map' AND object_id = OBJECT_ID('sdm.service_code_map'))
CREATE CLUSTERED INDEX cx_service_code_map ON sdm.service_code_map (TPCT_PFX, MAP_KIND, CODE_LOW);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_service_code_map_sese' AND object_id = OBJECT_ID('sdm.service_code_map'))
CREATE INDEX ix_service_code_map_sese ON sdm.service_code_map (TPCT_PFX, SESE_ID) INCLUDE (MAP_KIND, MCTR_SETG, CODE_LOW, CODE_HIGH, TPCT_MODIFIER);
GO

-- sdm.place_of_service
-- Grain: One row per place of service (PSCD_ID): its setting, which selects the procedure-to-service rows in service_code_map.
IF OBJECT_ID('sdm.place_of_service', 'U') IS NULL
CREATE TABLE sdm.place_of_service (
    PSCD_ID                  char(2)        NOT NULL,
    PSCD_DESC                char(70)       NULL,
    PSCD_POS_IND             char(1)        NULL,
    TPCT_MCTR_TCAT           char(1)        NULL,
    TPCT_MCTR_SETG           char(1)        NULL,
    CONSTRAINT PK_place_of_service PRIMARY KEY CLUSTERED (PSCD_ID)
);
GO

-- sdm.claims_header_ext_denorm
-- Grain: One row per claim (CLCL_ID): the 2300-level and claim-provider fields claims_denorm does not carry (it repeats header fields on each line; this table does not).
IF OBJECT_ID('sdm.claims_header_ext_denorm', 'U') IS NULL
CREATE TABLE sdm.claims_header_ext_denorm (
    CLCL_ID                  char(12)       NOT NULL,
    MEME_CK                  int            NULL,
    CSCS_ID                  char(4)        NULL,
    PRPR_ID                  char(12)       NULL,
    PRAD_TYPE                char(3)        NULL,
    CLCL_PAYEE_PR_ID         char(12)       NULL,
    NWPR_PFX                 char(4)        NULL,
    AGAG_ID                  char(12)       NULL,
    CLCL_PA_ACCT_NO          varchar(35)    NULL,
    CLCL_PAY_PR_IND          char(1)        NULL,
    CLCL_REL_INFO_IND        char(1)        NULL,
    CLCL_MED_ASSGN_IND       char(1)        NULL,
    CLCL_ACD_IND             char(1)        NULL,
    CLCL_ACD_STATE           char(2)        NULL,
    CLCL_ACD_DT              datetime       NULL,
    CLCL_CURR_ILL_DT         datetime       NULL,
    CLCL_SIMI_ILL_DT         datetime       NULL,
    CLCL_UNABL_FROM_DT       datetime       NULL,
    CLCL_UNABL_TO_DT         datetime       NULL,
    CLCL_RELHP_FROM_DT       datetime       NULL,
    CLCL_RELHP_TO_DT         datetime       NULL,
    CLCL_OUT_LAB_IND         char(1)        NULL,
    CLCL_ID_ADJ_FROM         char(12)       NULL,
    CLCL_MECD_RESUB_NO       char(15)       NULL,
    CLCL_OTHER_BN_IND        char(1)        NULL,
    CLCL_EXT_REF_IND         char(1)        NULL,
    CLM_FREQUENCY            char(1)        NULL,
    CLHP_FAC_TYPE            char(2)        NULL,
    CLHP_BILL_CLASS          char(1)        NULL,
    CLHP_FREQUENCY           char(1)        NULL,
    CLHP_ADM_TYP             char(2)        NULL,
    CLHP_ADM_SOURCE          char(1)        NULL,
    CLHP_DC_STAT             char(2)        NULL,
    CLHP_ADM_DT              datetime       NULL,
    CLHP_ADM_HOUR_CD         char(2)        NULL,
    CLHP_DC_DT               datetime       NULL,
    CLHP_DC_HOUR_CD          char(2)        NULL,
    CLHP_STAMENT_FR_DT       datetime       NULL,
    CLHP_STAMENT_TO_DT       datetime       NULL,
    CLHP_MED_REC_NO          char(17)       NULL,
    AGRG_ID                  char(4)        NULL,
    CLHP_INPUT_AGRG_ID       char(4)        NULL,
    CLHP_PRPR_ID_ADM         char(12)       NULL,
    CLHP_PRPR_AD_NPI         varchar(10)    NULL,
    CLHP_PRPR_ID_OPER        char(12)       NULL,
    CLHP_PRPR_OP_NPI         varchar(10)    NULL,
    CLHP_PRPR_ID_OTH1        char(12)       NULL,
    CLHP_PRPR_O1_NPI         varchar(10)    NULL,
    CLHP_ICD_QUAL_IND        char(1)        NULL,
    CLHP_BIRTH_WGT           smallint       NULL,
    CLMF_PRPR_ID_FAC         char(12)       NULL,
    CLMF_PRPR_FA_NPI         varchar(10)    NULL,
    CLMF_PRPR_RF_NPI         varchar(10)    NULL,
    CLMF_INPUT_TXNM_CD       char(12)       NULL,
    CLMF_EXT_REFRAL_NO       varchar(18)    NULL,
    CLMF_EXT_PREATH_NO       varchar(18)    NULL,
    CLMF_ICD_IND_PROC        char(1)        NULL,
    CLSD_PRPR_ID_REND_NVL    varchar(12)    NULL,
    CLSD_PRPR_RE_NPI_NVL     varchar(10)    NULL,
    CLED_HIPAA_VERSION       varchar(12)    NULL,
    CLED_EXT_REF             varchar(50)    NULL,
    CLED_TRAD_PARTNER        char(15)       NULL,
    CLCB_COB_TYPE            char(1)        NULL,
    CLCB_COB_AMT             money          NULL,
    CLCB_COB_ALLOW           money          NULL,
    CLCB_COB_DED_AMT         money          NULL,
    CLCB_COB_COPAY_AMT       money          NULL,
    CLCB_COB_COINS_AMT       money          NULL,
    CLCB_COB_REAS_CD         char(4)        NULL,
    CONSTRAINT PK_claims_header_ext_denorm PRIMARY KEY CLUSTERED (CLCL_ID)
);
GO

-- sdm.claims_detail_denorm
-- Grain: One row per claim line (CLCL_ID, CDML_SEQ_NO): the 2400-level fields claims_denorm does not carry (modifiers, line diagnosis pointers, NDC, line rendering provider, service ID, UM links, line COB).
IF OBJECT_ID('sdm.claims_detail_denorm', 'U') IS NULL
CREATE TABLE sdm.claims_detail_denorm (
    CLCL_ID                  char(12)       NOT NULL,
    CDML_SEQ_NO              smallint       NOT NULL,
    CDML_EXT_LINE_NO         smallint       NULL,
    PROC_CODE                char(5)        NULL,
    IPCD_MOD1                char(2)        NULL,
    CDML_IPCD_MOD2           char(2)        NULL,
    CDML_IPCD_MOD3           char(2)        NULL,
    CDML_IPCD_MOD4           char(2)        NULL,
    IDCD_ID                  varchar(10)    NULL,
    IDCD_ID_REL              varchar(10)    NULL,
    CDML_CLMD_TYPE2          char(2)        NULL,
    CDML_CLMD_TYPE3          char(2)        NULL,
    CDML_CLMD_TYPE4          char(2)        NULL,
    CDML_CLMD_TYPE5          char(2)        NULL,
    SESE_ID                  char(4)        NULL,
    SESE_RULE                char(3)        NULL,
    SEPY_PFX                 char(4)        NULL,
    DEDE_PFX                 char(4)        NULL,
    LTLT_PFX                 char(4)        NULL,
    PDVC_LOBD_PTR            char(1)        NULL,
    CDML_ROOM_TYPE           char(2)        NULL,
    CDML_HCPCS_AMT           money          NULL,
    CDML_REF_IND             char(1)        NULL,
    CDML_PC_IND              char(1)        NULL,
    CDML_UMREF_ID            char(9)        NULL,
    CDML_REFSV_SEQ_NO        smallint       NULL,
    CDML_UMAUTH_ID           char(9)        NULL,
    CDML_AUTHSV_SEQ_NO       smallint       NULL,
    CDML_CL_NTWK_IND         char(1)        NULL,
    CDML_DISALL_EXCD         char(3)        NULL,
    CDSD_PRPR_ID_REND        varchar(12)    NULL,
    CDSD_PRPR_RE_NPI         varchar(10)    NULL,
    CDSD_NDC_CODE            varchar(11)    NULL,
    CDSD_NDC_UNITS           varchar(15)    NULL,
    CDSD_NDC_MCTR_TYPE       char(4)        NULL,
    CDSD_SERV_LOC_ZIP        varchar(11)    NULL,
    CDSD_MED_AMB_ZIP         varchar(11)    NULL,
    CDCB_COB_TYPE            char(1)        NULL,
    CDCB_COB_AMT             money          NULL,
    CDCB_COB_ALLOW           money          NULL,
    CDCB_COB_DED_AMT         money          NULL,
    CDCB_COB_COPAY_AMT       money          NULL,
    CDCB_COB_COINS_AMT       money          NULL,
    CDCB_COB_REAS_CD         char(4)        NULL,
    CONSTRAINT PK_claims_detail_denorm PRIMARY KEY CLUSTERED (CLCL_ID, CDML_SEQ_NO)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_claims_detail_sese' AND object_id = OBJECT_ID('sdm.claims_detail_denorm'))
CREATE INDEX ix_claims_detail_sese ON sdm.claims_detail_denorm (SESE_ID) INCLUDE (CLCL_ID, CDML_SEQ_NO, SEPY_PFX);
GO

-- sdm.claims_diag
-- Grain: One row per claim diagnosis (CLCL_ID, CLMD_TYPE): every diagnosis, not only the primary and admitting ones in claims_denorm.
IF OBJECT_ID('sdm.claims_diag', 'U') IS NULL
CREATE TABLE sdm.claims_diag (
    CLCL_ID                  char(12)       NOT NULL,
    CLMD_TYPE                char(2)        NOT NULL,
    IDCD_ID                  varchar(10)    NULL,
    CLMD_POA_IND             char(1)        NULL,
    IDCD_ID_SUB              varchar(10)    NULL,
    CONSTRAINT PK_claims_diag PRIMARY KEY CLUSTERED (CLCL_ID, CLMD_TYPE)
);
GO

-- sdm.claims_icd_proc
-- Grain: One row per institutional claim procedure (CLCL_ID, CLHI_TYPE): 837I HI*BBR / BBQ.
IF OBJECT_ID('sdm.claims_icd_proc', 'U') IS NULL
CREATE TABLE sdm.claims_icd_proc (
    CLCL_ID                  char(12)       NOT NULL,
    CLHI_TYPE                char(2)        NOT NULL,
    IPCD_ID                  char(7)        NULL,
    CLHI_IP_DT               datetime       NULL,
    CONSTRAINT PK_claims_icd_proc PRIMARY KEY CLUSTERED (CLCL_ID, CLHI_TYPE)
);
GO

-- sdm.claims_ub_code
-- Grain: One row per UB condition, occurrence or value code (CLCL_ID, CODE_KIND, CODE_SEQ): 837I HI*BG / BH / BI / BE.
IF OBJECT_ID('sdm.claims_ub_code', 'U') IS NULL
CREATE TABLE sdm.claims_ub_code (
    CLCL_ID                  char(12)       NOT NULL,
    CODE_KIND                char(4)        NOT NULL,
    CODE_SEQ                 smallint       NOT NULL,
    UB_CODE                  char(2)        NULL,
    FROM_DT                  datetime       NULL,
    TO_DT                    datetime       NULL,
    VALUE_AMT                money          NULL,
    VALUE_UNITS              smallint       NULL,
    CONSTRAINT PK_claims_ub_code PRIMARY KEY CLUSTERED (CLCL_ID, CODE_KIND, CODE_SEQ)
);
GO

-- sdm.claims_auth
-- Grain: One row per claim line x UM link (CLCL_ID, CDML_SEQ_NO, AUTH_KIND = 'AUTH' or 'REF'): the authorized or referred service the line points to, so generated lines fall inside the authorized dates, provider, POS and units.
IF OBJECT_ID('sdm.claims_auth', 'U') IS NULL
CREATE TABLE sdm.claims_auth (
    CLCL_ID                  char(12)       NOT NULL,
    CDML_SEQ_NO              smallint       NOT NULL,
    AUTH_KIND                char(4)        NOT NULL,
    UMUM_REF_ID              char(9)        NULL,
    UMSV_SEQ_NO              smallint       NULL,
    UMSV_FROM_DT             datetime       NULL,
    UMSV_TO_DT               datetime       NULL,
    UMSV_PRPR_ID_REQ         char(12)       NULL,
    UMSV_PRPR_ID_SVC         char(12)       NULL,
    UMSV_PRPR_ID_FAC         char(12)       NULL,
    UMSV_PSCD_ID_AUTH        char(2)        NULL,
    IPCD_ID                  char(7)        NULL,
    SESE_ID                  char(4)        NULL,
    UMSV_UNITS_AUTH          smallint       NULL,
    UMVT_STS                 char(2)        NULL,
    UMSV_AUTH_DT             datetime       NULL,
    CONSTRAINT PK_claims_auth PRIMARY KEY CLUSTERED (CLCL_ID, CDML_SEQ_NO, AUTH_KIND)
);
GO
