-- FACETS_QA: a Facets-shaped slice for the SDG proof of concept.
-- Drop order is reverse dependency order so the script can rebuild in one run.

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
    THROW 50000, N'This script drops and rebuilds the demo FACETS tables, never in a production database. Nothing was changed. Choose the demo database and run it again.', 1;
GO
IF (SELECT CASE WHEN p.v LIKE N'{{%' OR CHARINDEX(N',' + UPPER(DB_NAME()) + N',', N',' + UPPER(REPLACE(p.v, N' ', N'')) + N',') > 0 THEN 1 ELSE 0 END FROM (SELECT N'{{PROTECTED_DATABASES}}' AS v) AS p) = 1
    SET NOEXEC ON;
GO

IF OBJECT_ID('dbo.TR_CMC_CLCL_CLAIM_AUDIT', 'TR') IS NOT NULL DROP TRIGGER dbo.TR_CMC_CLCL_CLAIM_AUDIT;
GO
IF OBJECT_ID('dbo.CMC_CLCL_AUDIT', 'U') IS NOT NULL DROP TABLE dbo.CMC_CLCL_AUDIT;
IF OBJECT_ID('dbo.CMC_CDML_CL_LINE', 'U') IS NOT NULL DROP TABLE dbo.CMC_CDML_CL_LINE;
IF OBJECT_ID('dbo.CMC_IPCD_PROC_CODE', 'U') IS NOT NULL DROP TABLE dbo.CMC_IPCD_PROC_CODE;
IF OBJECT_ID('dbo.CMC_CLCL_CLAIM', 'U') IS NOT NULL DROP TABLE dbo.CMC_CLCL_CLAIM;
IF OBJECT_ID('dbo.CMC_MEPR_PRIM_PROV', 'U') IS NOT NULL DROP TABLE dbo.CMC_MEPR_PRIM_PROV;
IF OBJECT_ID('dbo.CMC_MEME_MEMBER', 'U') IS NOT NULL DROP TABLE dbo.CMC_MEME_MEMBER;
IF OBJECT_ID('dbo.CMC_SBSB_SUBSC', 'U') IS NOT NULL DROP TABLE dbo.CMC_SBSB_SUBSC;
IF OBJECT_ID('dbo.CMC_SGSG_SUBGROUP', 'U') IS NOT NULL DROP TABLE dbo.CMC_SGSG_SUBGROUP;
IF OBJECT_ID('dbo.CMC_PLDS_PLAN_DESC', 'U') IS NOT NULL DROP TABLE dbo.CMC_PLDS_PLAN_DESC;
IF OBJECT_ID('dbo.CMC_CSPI_CS_PLAN', 'U') IS NOT NULL DROP TABLE dbo.CMC_CSPI_CS_PLAN;
IF OBJECT_ID('dbo.CMC_CSPD_CAT_DESC', 'U') IS NOT NULL DROP TABLE dbo.CMC_CSPD_CAT_DESC;
IF OBJECT_ID('dbo.CMC_PDDS_PROD_DESC', 'U') IS NOT NULL DROP TABLE dbo.CMC_PDDS_PROD_DESC;
IF OBJECT_ID('dbo.CMC_GRGR_GROUP', 'U') IS NOT NULL DROP TABLE dbo.CMC_GRGR_GROUP;
IF OBJECT_ID('dbo.CMC_IDCD_DIAG_CODE', 'U') IS NOT NULL DROP TABLE dbo.CMC_IDCD_DIAG_CODE;
IF OBJECT_ID('dbo.CMC_PDPD_PRODUCT', 'U') IS NOT NULL DROP TABLE dbo.CMC_PDPD_PRODUCT;
IF OBJECT_ID('dbo.CMC_NWST_NET_SET', 'U') IS NOT NULL DROP TABLE dbo.CMC_NWST_NET_SET;
IF OBJECT_ID('dbo.CMC_NWPR_RELATION', 'U') IS NOT NULL DROP TABLE dbo.CMC_NWPR_RELATION;
IF OBJECT_ID('dbo.CMC_NWNW_NETWORK', 'U') IS NOT NULL DROP TABLE dbo.CMC_NWNW_NETWORK;
IF OBJECT_ID('dbo.CMC_PRPR_PROV', 'U') IS NOT NULL DROP TABLE dbo.CMC_PRPR_PROV;
GO

-- Diagnosis codes. The claims analyzer ranks and filters on these, so they
-- carry descriptions rather than bare identifiers.
CREATE TABLE dbo.CMC_IDCD_DIAG_CODE (
    IDCD_ID          char(7)      NOT NULL,
    IDCD_DESC        varchar(80)  NOT NULL,
    IDCD_CHAPTER     varchar(40)  NOT NULL,
    CONSTRAINT PK_CMC_IDCD_DIAG_CODE PRIMARY KEY CLUSTERED (IDCD_ID)
);
GO

-- Providers. Natural primary key, no identity.
CREATE TABLE dbo.CMC_PRPR_PROV (
    PRPR_ID          char(12)     NOT NULL,
    PRPR_NPI         char(10)     NOT NULL,
    PRPR_SPECIALTY   char(3)      NOT NULL,
    PRPR_STATE       char(2)      NOT NULL,
    -- P practitioner, F facility, G group.
    PRPR_ENTITY      char(1)      NOT NULL,
    CONSTRAINT PK_CMC_PRPR_PROV PRIMARY KEY CLUSTERED (PRPR_ID),
    CONSTRAINT UQ_CMC_PRPR_PROV_NPI UNIQUE (PRPR_NPI),
    CONSTRAINT CK_CMC_PRPR_PROV_ENTITY CHECK (PRPR_ENTITY IN ('P', 'F', 'G'))
);
GO

-- Provider networks, and who participates in each over which dates. A claim
-- is in network when its provider participates, on the service date, in a
-- network of the member's plan.
CREATE TABLE dbo.CMC_NWNW_NETWORK (
    NWNW_ID          char(12)     NOT NULL,
    NWNW_NAME        char(50)     NOT NULL,
    NWNW_MCTR_TYPE   char(4)      NOT NULL,
    CONSTRAINT PK_CMC_NWNW_NETWORK PRIMARY KEY CLUSTERED (NWNW_ID)
);
GO

CREATE TABLE dbo.CMC_NWPR_RELATION (
    PRPR_ID          char(12)     NOT NULL,
    NWNW_ID          char(12)     NOT NULL,
    NWPR_PFX         char(4)      NOT NULL,
    NWPR_PCP_IND     char(1)      NOT NULL,
    NWPR_EFF_DT      date         NOT NULL,
    NWPR_TERM_DT     date         NULL,
    AGAG_ID          char(12)     NOT NULL,
    CONSTRAINT PK_CMC_NWPR_RELATION PRIMARY KEY CLUSTERED
        (PRPR_ID, NWNW_ID, NWPR_PFX, NWPR_PCP_IND, NWPR_EFF_DT),
    CONSTRAINT FK_CMC_NWPR_RELATION_PRPR FOREIGN KEY (PRPR_ID)
        REFERENCES dbo.CMC_PRPR_PROV (PRPR_ID),
    CONSTRAINT FK_CMC_NWPR_RELATION_NWNW FOREIGN KEY (NWNW_ID)
        REFERENCES dbo.CMC_NWNW_NETWORK (NWNW_ID)
);
GO

-- A plan's network set: the networks its members are in network with.
CREATE TABLE dbo.CMC_NWST_NET_SET (
    NWST_PFX         char(4)      NOT NULL,
    NWST_EFF_DT      date         NOT NULL,
    NWST_SEQ_NO      smallint     NOT NULL,
    NWST_TERM_DT     date         NULL,
    NWST_PR_NW_STS   char(1)      NOT NULL,
    NWNW_ID          char(12)     NOT NULL,
    NWPR_PFX         char(4)      NOT NULL,
    PDVC_TIER        smallint     NOT NULL,
    CONSTRAINT PK_CMC_NWST_NET_SET PRIMARY KEY CLUSTERED (NWST_PFX, NWST_EFF_DT, NWST_SEQ_NO),
    CONSTRAINT FK_CMC_NWST_NET_SET_NWNW FOREIGN KEY (NWNW_ID)
        REFERENCES dbo.CMC_NWNW_NETWORK (NWNW_ID)
);
GO

-- Products. Small static table; the shape the classifier should call "code".
CREATE TABLE dbo.CMC_PDPD_PRODUCT (
    PDPD_ID          char(8)      NOT NULL,
    PDPD_TYPE        char(1)      NOT NULL,
    CONSTRAINT PK_CMC_PDPD_PRODUCT PRIMARY KEY CLUSTERED (PDPD_ID),
    CONSTRAINT CK_CMC_PDPD_PRODUCT_TYPE CHECK (PDPD_TYPE IN ('M', 'D'))
);
GO

-- ---------------------------------------------------------------------------
-- The enrollment hierarchy: group, subgroup, plan category, plan, product.
-- The pattern analyzer selects down these levels and clusters the members that
-- remain, so they carry real descriptive text rather than placeholders.
-- ---------------------------------------------------------------------------

-- Groups. The employer or sponsor. Hierarchy level 1.
CREATE TABLE dbo.CMC_GRGR_GROUP (
    GRGR_CK          bigint       IDENTITY(1,1) NOT NULL,
    GRGR_ID          char(8)      NOT NULL,
    GRGR_NAME        varchar(60)  NOT NULL,
    GRGR_STATE       char(2)      NOT NULL,
    GRGR_COUNTY      varchar(40)  NOT NULL,
    GRGR_STS         char(2)      NOT NULL,
    GRGR_MCTR_TYPE   char(4)      NOT NULL,
    GRGR_ORIG_EFF_DT date         NOT NULL,
    GRGR_TERM_DT     date         NULL,
    CONSTRAINT PK_CMC_GRGR_GROUP PRIMARY KEY CLUSTERED (GRGR_CK),
    CONSTRAINT UQ_CMC_GRGR_GROUP_ID UNIQUE (GRGR_ID),
    CONSTRAINT CK_CMC_GRGR_GROUP_STS CHECK (GRGR_STS IN ('AC', 'TM'))
);
GO

-- Product descriptions. Split from the product so the description the analyzer
-- reads comes from its own table, as it does in Facets.
CREATE TABLE dbo.CMC_PDDS_PROD_DESC (
    PDPD_ID          char(8)      NOT NULL,
    PDDS_DESC        varchar(60)  NOT NULL,
    CONSTRAINT PK_CMC_PDDS_PROD_DESC PRIMARY KEY CLUSTERED (PDPD_ID),
    CONSTRAINT FK_CMC_PDDS_PROD_DESC_PDPD FOREIGN KEY (PDPD_ID)
        REFERENCES dbo.CMC_PDPD_PRODUCT (PDPD_ID)
);
GO

-- Plan categories. Hierarchy level 2.
CREATE TABLE dbo.CMC_CSPD_CAT_DESC (
    CSPD_CAT         char(1)      NOT NULL,
    CSPD_CAT_DESC    varchar(60)  NOT NULL,
    CSPD_TYPE        varchar(10)  NOT NULL,
    CONSTRAINT PK_CMC_CSPD_CAT_DESC PRIMARY KEY CLUSTERED (CSPD_CAT)
);
GO

-- Coverage plans. Hierarchy level 3, and the row that names the product.
CREATE TABLE dbo.CMC_CSPI_CS_PLAN (
    CSPI_ID          char(8)      NOT NULL,
    CSPD_CAT         char(1)      NOT NULL,
    PDPD_ID          char(8)      NOT NULL,
    CSPI_EFF_DT      date         NOT NULL,
    CSPI_TERM_DT     date         NULL,
    CSPI_SEL_IND     char(1)      NOT NULL,
    NWST_PFX         char(4)      NULL,
    CONSTRAINT PK_CMC_CSPI_CS_PLAN PRIMARY KEY CLUSTERED (CSPI_ID),
    CONSTRAINT FK_CMC_CSPI_CS_PLAN_CSPD FOREIGN KEY (CSPD_CAT)
        REFERENCES dbo.CMC_CSPD_CAT_DESC (CSPD_CAT),
    CONSTRAINT FK_CMC_CSPI_CS_PLAN_PDPD FOREIGN KEY (PDPD_ID)
        REFERENCES dbo.CMC_PDPD_PRODUCT (PDPD_ID)
);
GO

-- Plan descriptions, one per plan.
CREATE TABLE dbo.CMC_PLDS_PLAN_DESC (
    CSPI_ID          char(8)      NOT NULL,
    PLDS_DESC        varchar(60)  NOT NULL,
    CONSTRAINT PK_CMC_PLDS_PLAN_DESC PRIMARY KEY CLUSTERED (CSPI_ID),
    CONSTRAINT FK_CMC_PLDS_PLAN_DESC_CSPI FOREIGN KEY (CSPI_ID)
        REFERENCES dbo.CMC_CSPI_CS_PLAN (CSPI_ID)
);
GO

-- Subgroups. A division within a group, carried as data rather than as a level
-- the analyzer selects on.
CREATE TABLE dbo.CMC_SGSG_SUBGROUP (
    SGSG_CK          bigint       IDENTITY(1,1) NOT NULL,
    GRGR_CK          bigint       NOT NULL,
    SGSG_ID          char(8)      NOT NULL,
    SGSG_NAME        varchar(60)  NOT NULL,
    CSCS_ID          char(8)      NOT NULL,
    SGSG_STATE       char(2)      NOT NULL,
    SGSG_STS         char(2)      NOT NULL,
    SGSG_ORIG_EFF_DT date         NOT NULL,
    SGSG_TERM_DT     date         NULL,
    CONSTRAINT PK_CMC_SGSG_SUBGROUP PRIMARY KEY CLUSTERED (SGSG_CK),
    CONSTRAINT UQ_CMC_SGSG_SUBGROUP_ID UNIQUE (SGSG_ID),
    CONSTRAINT FK_CMC_SGSG_SUBGROUP_GRGR FOREIGN KEY (GRGR_CK)
        REFERENCES dbo.CMC_GRGR_GROUP (GRGR_CK)
);
GO

-- Subscribers. SBSB_CK is an identity surrogate key; SBSB_ID is the business key.
-- The subscriber is where the enrollment hierarchy attaches: every member
-- inherits its group, subgroup, and plan from the subscriber it belongs to.
CREATE TABLE dbo.CMC_SBSB_SUBSC (
    SBSB_CK          bigint       IDENTITY(1,1) NOT NULL,
    SBSB_ID          char(9)      NOT NULL,
    SBSB_LAST_NAME   varchar(30)  NOT NULL,
    SBSB_FIRST_NAME  varchar(30)  NOT NULL,
    SBSB_EFF_DT      date         NOT NULL,
    SBSB_ORIG_EFF_DT date         NOT NULL,
    SBSB_MCTR_STS    char(2)      NOT NULL,
    SBSB_EMPLOY_ID   char(10)     NOT NULL,
    GRGR_CK          bigint       NOT NULL,
    SGSG_CK          bigint       NOT NULL,
    CSPI_ID          char(8)      NOT NULL,
    CONSTRAINT PK_CMC_SBSB_SUBSC PRIMARY KEY CLUSTERED (SBSB_CK),
    CONSTRAINT UQ_CMC_SBSB_SUBSC_ID UNIQUE (SBSB_ID),
    CONSTRAINT FK_CMC_SBSB_SUBSC_GRGR FOREIGN KEY (GRGR_CK)
        REFERENCES dbo.CMC_GRGR_GROUP (GRGR_CK),
    CONSTRAINT FK_CMC_SBSB_SUBSC_SGSG FOREIGN KEY (SGSG_CK)
        REFERENCES dbo.CMC_SGSG_SUBGROUP (SGSG_CK),
    CONSTRAINT FK_CMC_SBSB_SUBSC_CSPI FOREIGN KEY (CSPI_ID)
        REFERENCES dbo.CMC_CSPI_CS_PLAN (CSPI_ID)
);
GO

-- Members. One subscriber has one or more members.
CREATE TABLE dbo.CMC_MEME_MEMBER (
    MEME_CK             bigint       IDENTITY(1,1) NOT NULL,
    SBSB_CK             bigint       NOT NULL,
    MEME_ID             char(11)     NOT NULL,
    MEME_FIRST_NAME     varchar(30)  NOT NULL,
    MEME_LAST_NAME      varchar(30)  NOT NULL,
    MEME_MID_INIT       char(1)      NULL,
    MEME_BIRTH_DT       date         NOT NULL,
    MEME_SEX            char(1)      NOT NULL,
    MEME_MARITAL_STATUS char(1)      NOT NULL,
    MEME_SSN            char(9)      NOT NULL,
    MEME_MCTR_STS       char(2)      NOT NULL,
    MEME_REL            char(1)      NOT NULL,
    MEME_SFX            smallint     NOT NULL,
    MEME_EFF_DT         date         NOT NULL,
    MEME_ORIG_EFF_DT    date         NOT NULL,
    MEME_TERM_DT        date         NULL,
    MEME_ZIP            char(5)      NOT NULL,
    MEME_CITY           varchar(40)  NOT NULL,
    MEME_STATE          char(2)      NOT NULL,
    CONSTRAINT PK_CMC_MEME_MEMBER PRIMARY KEY CLUSTERED (MEME_CK),
    CONSTRAINT UQ_CMC_MEME_MEMBER_ID UNIQUE (MEME_ID),
    CONSTRAINT FK_CMC_MEME_MEMBER_SBSB FOREIGN KEY (SBSB_CK)
        REFERENCES dbo.CMC_SBSB_SUBSC (SBSB_CK),
    CONSTRAINT CK_CMC_MEME_MEMBER_REL CHECK (MEME_REL IN ('M', 'D', 'S', 'P')),
    CONSTRAINT CK_CMC_MEME_MEMBER_SEX CHECK (MEME_SEX IN ('M', 'F')),
    CONSTRAINT CK_CMC_MEME_MEMBER_MARITAL CHECK (MEME_MARITAL_STATUS IN ('M', 'S', 'D', 'W'))
);
GO

-- Claims. MEME_CK deliberately carries no foreign key: Step 4 must infer it.
-- A member's primary care provider, over dates: a PCP of the plan's networks.
CREATE TABLE dbo.CMC_MEPR_PRIM_PROV (
    MEME_CK          bigint       NOT NULL,
    MEPR_PCP_TYPE    char(2)      NOT NULL,
    MEPR_EFF_DT      date         NOT NULL,
    MEPR_TERM_DT     date         NULL,
    GRGR_CK          bigint       NOT NULL,
    PRPR_ID          char(12)     NOT NULL,
    CONSTRAINT PK_CMC_MEPR_PRIM_PROV PRIMARY KEY CLUSTERED (MEME_CK, MEPR_PCP_TYPE, MEPR_EFF_DT),
    CONSTRAINT FK_CMC_MEPR_PRIM_PROV_MEME FOREIGN KEY (MEME_CK)
        REFERENCES dbo.CMC_MEME_MEMBER (MEME_CK),
    CONSTRAINT FK_CMC_MEPR_PRIM_PROV_PRPR FOREIGN KEY (PRPR_ID)
        REFERENCES dbo.CMC_PRPR_PROV (PRPR_ID),
    CONSTRAINT FK_CMC_MEPR_PRIM_PROV_GRGR FOREIGN KEY (GRGR_CK)
        REFERENCES dbo.CMC_GRGR_GROUP (GRGR_CK)
);
GO

CREATE TABLE dbo.CMC_CLCL_CLAIM (
    CLCL_ID          char(12)      NOT NULL,
    MEME_CK          bigint        NOT NULL,
    PRPR_ID          char(12)      NOT NULL,
    PDPD_ID          char(8)       NOT NULL,
    CLCL_CUR_STS     char(2)       NOT NULL,
    CLCL_CL_TYPE     char(1)       NOT NULL,
    CLCL_NTWK_IND    char(1)       NOT NULL,
    -- The network the provider was in for this claim; none when out of network.
    NWNW_ID          char(12)      NULL,
    CLCL_IDCD_ID     char(7)       NOT NULL,
    CLCL_SVC_DT      date          NOT NULL,
    CLCL_RECD_DT     date          NOT NULL,
    CLCL_PAID_DT     date          NULL,
    CLCL_TOT_CHG     decimal(12,2) NOT NULL,
    CLCL_CREATED_TS  datetime      NOT NULL CONSTRAINT DF_CMC_CLCL_CLAIM_CREATED DEFAULT (GETDATE()),
    CLCL_ROW_HASH    AS CONVERT(char(64),
                         HASHBYTES('SHA2_256',
                             CONCAT(CLCL_ID, '|', CLCL_CUR_STS, '|', CLCL_TOT_CHG)), 2),
    CONSTRAINT PK_CMC_CLCL_CLAIM PRIMARY KEY CLUSTERED (CLCL_ID),
    CONSTRAINT FK_CMC_CLCL_CLAIM_PRPR FOREIGN KEY (PRPR_ID)
        REFERENCES dbo.CMC_PRPR_PROV (PRPR_ID),
    CONSTRAINT FK_CMC_CLCL_CLAIM_NWNW FOREIGN KEY (NWNW_ID)
        REFERENCES dbo.CMC_NWNW_NETWORK (NWNW_ID),
    CONSTRAINT FK_CMC_CLCL_CLAIM_PDPD FOREIGN KEY (PDPD_ID)
        REFERENCES dbo.CMC_PDPD_PRODUCT (PDPD_ID),
    CONSTRAINT FK_CMC_CLCL_CLAIM_IDCD FOREIGN KEY (CLCL_IDCD_ID)
        REFERENCES dbo.CMC_IDCD_DIAG_CODE (IDCD_ID),
    CONSTRAINT CK_CMC_CLCL_CLAIM_TYPE CHECK (CLCL_CL_TYPE IN ('P', 'I', 'D')),
    CONSTRAINT CK_CMC_CLCL_CLAIM_NTWK CHECK (CLCL_NTWK_IND IN ('I', 'O'))
);
GO

-- Claim lines. Composite primary key.
CREATE TABLE dbo.CMC_CDML_CL_LINE (
    CLCL_ID          char(12)      NOT NULL,
    CDML_SEQ_NO      smallint      NOT NULL,
    CDML_SVC_CD      char(5)       NOT NULL,
    CDML_CHG_AMT     decimal(12,2) NOT NULL,
    CDML_UNITS       int           NOT NULL,
    CONSTRAINT PK_CMC_CDML_CL_LINE PRIMARY KEY CLUSTERED (CLCL_ID, CDML_SEQ_NO),
    CONSTRAINT FK_CMC_CDML_CL_LINE_CLCL FOREIGN KEY (CLCL_ID)
        REFERENCES dbo.CMC_CLCL_CLAIM (CLCL_ID)
);
GO

-- Procedure codes, one row per service code the lines use.
CREATE TABLE dbo.CMC_IPCD_PROC_CODE (
    IPCD_ID          char(5)      NOT NULL,
    IPCD_DESC        varchar(80)  NOT NULL,
    CONSTRAINT PK_CMC_IPCD_PROC_CODE PRIMARY KEY CLUSTERED (IPCD_ID)
);
GO

-- Audit sink for the claim trigger. Out of the generation slice; it exists so
-- the writer's trigger handling is exercised.
CREATE TABLE dbo.CMC_CLCL_AUDIT (
    AUDIT_ID         bigint        IDENTITY(1,1) NOT NULL,
    CLCL_ID          char(12)      NOT NULL,
    AUDIT_ACTION     varchar(10)   NOT NULL,
    AUDIT_TS         datetime2(3)  NOT NULL CONSTRAINT DF_CMC_CLCL_AUDIT_TS DEFAULT (SYSDATETIME()),
    CONSTRAINT PK_CMC_CLCL_AUDIT PRIMARY KEY CLUSTERED (AUDIT_ID)
);
GO

CREATE TRIGGER dbo.TR_CMC_CLCL_CLAIM_AUDIT
ON dbo.CMC_CLCL_CLAIM
AFTER INSERT
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO dbo.CMC_CLCL_AUDIT (CLCL_ID, AUDIT_ACTION)
    SELECT i.CLCL_ID, 'INSERT' FROM inserted AS i;
END;
GO

CREATE INDEX IX_CMC_MEME_MEMBER_SBSB ON dbo.CMC_MEME_MEMBER (SBSB_CK);
CREATE INDEX IX_CMC_SBSB_SUBSC_GRGR ON dbo.CMC_SBSB_SUBSC (GRGR_CK);
CREATE INDEX IX_CMC_SBSB_SUBSC_CSPI ON dbo.CMC_SBSB_SUBSC (CSPI_ID);
CREATE INDEX IX_CMC_SGSG_SUBGROUP_GRGR ON dbo.CMC_SGSG_SUBGROUP (GRGR_CK);
CREATE INDEX IX_CMC_CLCL_CLAIM_MEME ON dbo.CMC_CLCL_CLAIM (MEME_CK);
CREATE INDEX IX_CMC_CLCL_CLAIM_PRPR ON dbo.CMC_CLCL_CLAIM (PRPR_ID);
CREATE INDEX IX_CMC_NWPR_RELATION_NWNW ON dbo.CMC_NWPR_RELATION (NWNW_ID, PRPR_ID);
CREATE INDEX IX_CMC_MEPR_PRIM_PROV_PRPR ON dbo.CMC_MEPR_PRIM_PROV (PRPR_ID);
CREATE INDEX IX_CMC_CLCL_CLAIM_PDPD ON dbo.CMC_CLCL_CLAIM (PDPD_ID);
CREATE INDEX IX_CMC_CLCL_CLAIM_IDCD ON dbo.CMC_CLCL_CLAIM (CLCL_IDCD_ID);
GO
