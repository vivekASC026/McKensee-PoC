-- MDM_REF: master reference data, a separate database from FACETS_QA.

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
    THROW 50000, N'This script drops and rebuilds the demo reference tables, never in a production database. Nothing was changed. Choose the demo database and run it again.', 1;
GO
IF (SELECT CASE WHEN p.v LIKE N'{{%' OR CHARINDEX(N',' + UPPER(DB_NAME()) + N',', N',' + UPPER(REPLACE(p.v, N' ', N'')) + N',') > 0 THEN 1 ELSE 0 END FROM (SELECT N'{{PROTECTED_DATABASES}}' AS v) AS p) = 1
    SET NOEXEC ON;
GO

IF OBJECT_ID('dbo.REF_PLAN_CODE', 'U') IS NOT NULL DROP TABLE dbo.REF_PLAN_CODE;
IF OBJECT_ID('dbo.REF_PRODUCT_CODE', 'U') IS NOT NULL DROP TABLE dbo.REF_PRODUCT_CODE;
GO

CREATE TABLE dbo.REF_PLAN_CODE (
    PLAN_CD          char(6)      NOT NULL,
    PLAN_NAME        varchar(60)  NOT NULL,
    ACTIVE_FLAG      char(1)      NOT NULL,
    CONSTRAINT PK_REF_PLAN_CODE PRIMARY KEY CLUSTERED (PLAN_CD),
    CONSTRAINT CK_REF_PLAN_CODE_ACTIVE CHECK (ACTIVE_FLAG IN ('Y', 'N'))
);
GO

-- Same identifiers as FACETS_QA.CMC_PDPD_PRODUCT, held by the master system.
CREATE TABLE dbo.REF_PRODUCT_CODE (
    PRODUCT_CD       char(8)      NOT NULL,
    PRODUCT_TYPE     char(1)      NOT NULL,
    CONSTRAINT PK_REF_PRODUCT_CODE PRIMARY KEY CLUSTERED (PRODUCT_CD),
    CONSTRAINT CK_REF_PRODUCT_CODE_TYPE CHECK (PRODUCT_TYPE IN ('M', 'D'))
);
GO
