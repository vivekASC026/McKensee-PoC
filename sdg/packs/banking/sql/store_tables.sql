-- store_tables.sql (banking pack): the tables pattern analysis reads.
--
-- One row per customer (customer_denorm), and one row per transaction with
-- its account, product and customer beside it (account_denorm), filled by an
-- extract (sdg/packs/banking/extract). Add-only: a table that exists is left
-- as it is. Written for schema sdm.

-- Never in a production database. SDM writes their names (SDG_PROTECTED_DATABASES)
-- in place of {{PROTECTED_DATABASES}} when it runs this script; run by hand, write
-- them there yourself (every one: Find and Replace all), separated by commas,
-- or the script stops.
-- The first batch stops the
-- script there with an error; the second keeps every batch after it from
-- running (SET NOEXEC ON lasts for the session: reconnect after).
IF N'{{PROTECTED_DATABASES}}' LIKE N'{{%'
    THROW 50000, N'This script names no production database yet: SDM names them when it runs it; run by hand, replace the placeholder in its first lines with their names, separated by commas. Nothing was changed.', 1;
IF (SELECT CASE WHEN p.v LIKE N'{{%' OR CHARINDEX(N',' + UPPER(DB_NAME()) + N',', N',' + UPPER(REPLACE(p.v, N' ', N'')) + N',') > 0 THEN 1 ELSE 0 END FROM (SELECT N'{{PROTECTED_DATABASES}}' AS v) AS p) = 1
    THROW 50000, N'This script runs in the SDM platform database, never in a production database. Nothing was changed. Choose the SDM platform database and run it again.', 1;
GO
IF (SELECT CASE WHEN p.v LIKE N'{{%' OR CHARINDEX(N',' + UPPER(DB_NAME()) + N',', N',' + UPPER(REPLACE(p.v, N' ', N'')) + N',') > 0 THEN 1 ELSE 0 END FROM (SELECT N'{{PROTECTED_DATABASES}}' AS v) AS p) = 1
    SET NOEXEC ON;
GO

IF OBJECT_ID('sdm.customer_denorm', 'U') IS NULL
CREATE TABLE sdm.customer_denorm (
    CUSTOMER_ID    int          NOT NULL,
    FIRST_NAME     varchar(40)  NULL,
    LAST_NAME      varchar(40)  NULL,
    BIRTH_DATE     date         NULL,
    SEGMENT        varchar(12)  NULL,
    CUST_SINCE     date         NULL,
    ADDR_CITY      varchar(40)  NULL,
    ADDR_STATE     char(2)      NULL,
    ADDR_ZIP       char(5)      NULL,
    EMAIL          varchar(80)  NULL,
    BRANCH_ID      int          NULL,
    BRANCH_NAME    varchar(60)  NULL
);
GO

IF OBJECT_ID('sdm.account_denorm', 'U') IS NULL
CREATE TABLE sdm.account_denorm (
    ACCOUNT_ID     bigint       NOT NULL,
    TXN_SEQ        int          NOT NULL,
    CUSTOMER_ID    int          NULL,
    PRODUCT_CODE   varchar(8)   NULL,
    PRODUCT_NAME   varchar(60)  NULL,
    PRODUCT_TYPE   varchar(10)  NULL,
    OPEN_DATE      date         NULL,
    ACCT_STATUS    char(1)      NULL,
    TXN_DATE       date         NULL,
    TXN_TYPE       varchar(10)  NULL,
    CHANNEL        varchar(10)  NULL,
    MCC_CODE       char(4)      NULL,
    AMOUNT         decimal(12,2) NULL,
    BRANCH_ID      int          NULL,
    BRANCH_NAME    varchar(60)  NULL,
    SEGMENT        varchar(12)  NULL,
    CUST_AGE       int          NULL
);
GO
