-- The analysis store: the two denormalized tables pattern analysis reads.
--
-- These are the customer's tables, column for column and type for type, as
-- their SDM platform holds them (sdm.member_denorm, sdm.claims_denorm). The
-- customer keeps them filled from their own FACETS; nothing here may change
-- them, so this script only creates them where they are absent -- locally,
-- where the extract fills them from the demo source. There is no source
-- column: a store holds one landscape, and an extract replaces it.
--
-- Written for schema sdm; seed/bootstrap.py runs it in the configured one.
-- Indexes are not part of the customer's tables: see denorm_indexes.sql.
--
-- A table with a source column is this tool's own, from a version before the
-- store took the customer's shape: an extract, refilled by the next extract.
-- It is replaced. The customer's tables have no such column and are never
-- touched.

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

IF COL_LENGTH('sdm.member_denorm', 'source') IS NOT NULL
    DROP TABLE sdm.member_denorm;
IF COL_LENGTH('sdm.claims_denorm', 'source') IS NOT NULL
    DROP TABLE sdm.claims_denorm;
GO

IF OBJECT_ID('sdm.member_denorm', 'U') IS NULL
BEGIN
CREATE TABLE sdm.[member_denorm] (
    [MEME_CK] int NULL,
    [SBSB_CK] int NULL,
    [GRGR_CK] int NULL,
    [MEME_SFX] int NULL,
    [MEME_REL] char(1) NULL,
    [MEME_LAST_NAME] nvarchar(100) NULL,
    [MEME_FIRST_NAME] nvarchar(100) NULL,
    [MEME_MID_INIT] char(1) NULL,
    [MEME_SEX] char(1) NULL,
    [MEME_BIRTH_DT] date NULL,
    [MEME_SSN] bigint NULL,
    [MEME_MCTR_STS] nvarchar(10) NULL,
    [MEME_ORIG_EFF_DT] date NULL,
    [MEME_MARITAL_STATUS] char(1) NULL,
    [MEME_MEDCD_NO] nvarchar(50) NULL,
    [MEME_HICN] nvarchar(50) NULL,
    [MEME_MCTR_RACE_NVL] nvarchar(50) NULL,
    [MEME_MCTR_ETHN_NVL] nvarchar(50) NULL,
    [SBSB_ID] nvarchar(50) NULL,
    [SBSB_LAST_NAME] nvarchar(100) NULL,
    [SBSB_FIRST_NAME] nvarchar(100) NULL,
    [SBSB_ORIG_EFF_DT] date NULL,
    [SBSB_MCTR_STS] nvarchar(10) NULL,
    [SBSB_EMPLOY_ID] nvarchar(50) NULL,
    [SGSG_CK] int NULL,
    [SBSG_EFF_DT] date NULL,
    [SBSG_TERM_DT] date NULL,
    [SBSG_MCTR_TRSN] nvarchar(10) NULL,
    [SGSG_ID] nvarchar(50) NULL,
    [SGSG_NAME] nvarchar(200) NULL,
    [CSCS_ID] nvarchar(50) NULL,
    [SGSG_STATE] char(2) NULL,
    [SGSG_STS] nvarchar(10) NULL,
    [SGSG_ORIG_EFF_DT] date NULL,
    [SGSG_TERM_DT] date NULL,
    [GRGR_ID] nvarchar(50) NULL,
    [GRGR_NAME] nvarchar(200) NULL,
    [GRGR_STATE] char(2) NULL,
    [GRGR_COUNTY] nvarchar(100) NULL,
    [GRGR_STS] nvarchar(10) NULL,
    [GRGR_ORIG_EFF_DT] date NULL,
    [GRGR_TERM_DT] date NULL,
    [GRGR_MCTR_TYPE] nvarchar(10) NULL,
    [PAGR_CK] int NULL,
    [CSPI_ID] nvarchar(50) NULL,
    [CSPD_CAT] char(1) NULL,
    [CSPI_EFF_DT] date NULL,
    [CSPI_TERM_DT] date NULL,
    [PDPD_ID] nvarchar(50) NULL,
    [CSPI_SEL_IND] nvarchar(10) NULL,
    [CSPI_HIOS_ID_NVL] nvarchar(50) NULL,
    [CSPD_CAT_DESC] nvarchar(200) NULL,
    [CSPD_TYPE] nvarchar(50) NULL,
    [LOBD_ID] nvarchar(50) NULL,
    [PDPD_RISK_IND] nvarchar(10) NULL,
    [PDPD_MCTR_CCAT] nvarchar(50) NULL,
    [PLDS_DESC] nvarchar(200) NULL,
    [PDDS_DESC] nvarchar(200) NULL,
    [EMAIL] varchar(200) NULL
);
END;
GO

IF OBJECT_ID('sdm.claims_denorm', 'U') IS NULL
BEGIN
CREATE TABLE sdm.[claims_denorm] (
    [CLCL_ID] char(12) NOT NULL,
    [CDML_SEQ_NO] smallint NOT NULL,
    [CLCL_CL_TYPE] char(1) NULL,
    [CLCL_CL_SUB_TYPE] char(1) NULL,
    [CLCL_CUR_STS] char(2) NULL,
    [CLST_MCTR_REAS] char(4) NULL,
    [CLCL_NTWK_IND] char(1) NULL,
    [CLCL_INPUT_DT] datetime NULL,
    [CLCL_RECD_DT] datetime NULL,
    [CLCL_PAID_DT] datetime NULL,
    [CLCL_LOW_SVC_DT] datetime NULL,
    [CLCL_HIGH_SVC_DT] datetime NULL,
    [CLCL_TOT_CHG] money NULL,
    [CLCL_TOT_PAYABLE] money NULL,
    [MEME_CK] int NULL,
    [SBSB_CK] int NULL,
    [CLCL_ME_AGE] smallint NULL,
    [MEME_SEX] char(1) NULL,
    [MEME_REL] char(1) NULL,
    [MEPE_FI] char(1) NULL,
    [GRGR_CK] int NULL,
    [GRGR_NAME] varchar(200) NULL,
    [SGSG_CK] int NULL,
    [CSPD_CAT] char(1) NULL,
    [CSPI_ID] char(8) NULL,
    [PLDS_DESC] varchar(70) NULL,
    [PDPD_ID] char(8) NULL,
    [PDDS_PROD_TYPE] char(1) NULL,
    [IDCD_ID_PRIMARY] varchar(10) NULL,
    [IDCD_DESC_PRIMARY] varchar(70) NULL,
    [IDCD_ID_ADMIT] varchar(10) NULL,
    [CLMD_DIAG_COUNT] smallint NULL,
    [IPCD_ID] char(7) NULL,
    [IPCD_DESC] varchar(70) NULL,
    [IPCD_TYPE] char(2) NULL,
    [CDML_UNITS] smallint NULL,
    [CDML_UNITS_ALLOW] smallint NULL,
    [PSCD_ID] char(4) NULL,
    [RCRC_ID] char(4) NULL,
    [LOBD_ID] char(4) NULL,
    [PRPR_ID] char(12) NULL,
    [PRPR_NAME] varchar(60) NULL,
    [PRPR_NPI] varchar(10) NULL,
    [PRPR_MCTR_SPEC] char(4) NULL,
    [NWNW_ID] char(12) NULL,
    [CDML_CHG_AMT] money NULL,
    [CDML_ALLOW] money NULL,
    [CDML_PAID_AMT] money NULL,
    [CDML_DED_AMT] money NULL,
    [CDML_COPAY_AMT] money NULL,
    [CDML_COINS_AMT] money NULL,
    [CDML_DISALL_AMT] money NULL,
    [CDML_DISC_AMT] money NULL,
    [CDML_TOT_PA_LIAB] money NULL,
    [CDML_FROM_DT] datetime NULL,
    [CDML_TO_DT] datetime NULL,
    [CDML_CUR_STS] char(2) NULL,
    [CLCL_EXT_AUTH_NO] char(18) NULL,
    [CDML_PRE_AUTH_IND] char(1) NULL,
    [CLCL_PRPR_ID_REF] char(12) NULL,
    [CLCL_PRPR_ID_PCP] char(12) NULL,
    [CLCL_PCP_IND] char(1) NULL
);
END;
GO

