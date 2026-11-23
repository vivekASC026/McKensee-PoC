-- bank_source.sql: the banking pack's sample core-banking schema, in dbo.
--
-- A small retail bank: customers hold accounts, accounts carry transactions.
-- Branches, products and merchant category codes are reference data a run
-- reuses; the general ledger is never generated; the audit log is left out.
-- seed/bank.py creates these tables in a database of its own and fills them
-- with about 2,000 made-up customers. Nothing here is a real bank's.

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
    THROW 50000, N'This script creates the sample bank, never in a production database. Nothing was changed.', 1;
GO
IF (SELECT CASE WHEN p.v LIKE N'{{%' OR CHARINDEX(N',' + UPPER(DB_NAME()) + N',', N',' + UPPER(REPLACE(p.v, N' ', N'')) + N',') > 0 THEN 1 ELSE 0 END FROM (SELECT N'{{PROTECTED_DATABASES}}' AS v) AS p) = 1
    SET NOEXEC ON;
GO

CREATE TABLE dbo.BRANCH (
    BRANCH_ID      int          NOT NULL PRIMARY KEY,
    BRANCH_NAME    varchar(60)  NOT NULL,
    BRANCH_STATE   char(2)      NOT NULL,
    BRANCH_CITY    varchar(40)  NOT NULL
);

CREATE TABLE dbo.PRODUCT (
    PRODUCT_CODE   varchar(8)   NOT NULL PRIMARY KEY,
    PRODUCT_NAME   varchar(60)  NOT NULL,
    PRODUCT_TYPE   varchar(10)  NOT NULL   -- CHECKING, SAVINGS, CARD, LOAN
);

CREATE TABLE dbo.MCC (
    MCC_CODE       char(4)      NOT NULL PRIMARY KEY,
    MCC_DESC       varchar(60)  NOT NULL
);

CREATE TABLE dbo.CUSTOMER (
    CUSTOMER_ID    int          NOT NULL PRIMARY KEY,
    FIRST_NAME     varchar(40)  NOT NULL,
    LAST_NAME      varchar(40)  NOT NULL,
    BIRTH_DATE     date         NOT NULL,
    SEGMENT        varchar(12)  NOT NULL,  -- MASS, AFFLUENT, PRIVATE, SMALL_BIZ
    CUST_SINCE     date         NOT NULL,
    ADDR_CITY      varchar(40)  NOT NULL,
    ADDR_STATE     char(2)      NOT NULL,
    ADDR_ZIP       char(5)      NOT NULL,
    EMAIL          varchar(80)  NULL,
    BRANCH_ID      int          NOT NULL REFERENCES dbo.BRANCH (BRANCH_ID)
);

CREATE TABLE dbo.ACCOUNT (
    ACCOUNT_ID     bigint       NOT NULL PRIMARY KEY,
    CUSTOMER_ID    int          NOT NULL REFERENCES dbo.CUSTOMER (CUSTOMER_ID),
    PRODUCT_CODE   varchar(8)   NOT NULL REFERENCES dbo.PRODUCT (PRODUCT_CODE),
    OPEN_DATE      date         NOT NULL,
    ACCT_STATUS    char(1)      NOT NULL,  -- A active, D dormant, C closed
    BALANCE        decimal(12,2) NOT NULL
);

CREATE TABLE dbo.TXN (
    ACCOUNT_ID     bigint       NOT NULL REFERENCES dbo.ACCOUNT (ACCOUNT_ID),
    TXN_SEQ        int          NOT NULL,
    TXN_DATE       date         NOT NULL,
    TXN_TYPE       varchar(10)  NOT NULL,  -- DEBIT, CREDIT, FEE, INTEREST
    CHANNEL        varchar(10)  NOT NULL,  -- BRANCH, ATM, ONLINE, CARD
    MCC_CODE       char(4)      NULL REFERENCES dbo.MCC (MCC_CODE),
    AMOUNT         decimal(12,2) NOT NULL,
    CONSTRAINT PK_TXN PRIMARY KEY (ACCOUNT_ID, TXN_SEQ)
);

-- Reference data the bank keeps, and SDM never generates.
CREATE TABLE dbo.GL_ENTRY (
    GL_ID          bigint       NOT NULL PRIMARY KEY,
    GL_ACCOUNT     varchar(12)  NOT NULL,
    POSTED_DATE    date         NOT NULL,
    AMOUNT         decimal(14,2) NOT NULL
);

-- Who changed what: never copied into a test database.
CREATE TABLE dbo.AUDIT_LOG (
    AUDIT_ID       bigint       NOT NULL PRIMARY KEY,
    TABLE_NAME     varchar(40)  NOT NULL,
    CHANGED_AT     datetime2    NOT NULL,
    CHANGED_BY     varchar(40)  NOT NULL
);
