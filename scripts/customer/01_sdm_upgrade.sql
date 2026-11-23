-- 01_sdm_upgrade.sql: add-only. Columns and tables the tool needs.
--
-- For the customer's SDM platform database. Written for schema sdm; if the
-- tool is configured for another schema (SDG_DENORM_SCHEMA), replace "sdm."
-- and 'sdm' throughout before running.
--
-- Nothing is dropped, renamed, retyped or deleted, and member_denorm and
-- claims_denorm are not touched. Each statement checks before it acts, so the
-- script can be run again safely.
--
-- 1. Columns added to the platform's own tables (patterns, pattern_rules,
--    pattern_tags, generation_rules). All are NULL or carry a default, so
--    existing rows and existing inserts are unaffected.
-- 2. Tables the tool keeps that the platform does not have: layouts, column
--    aliases for rules and the pattern baseline. Their ids
--    are IDENTITY.
--
-- The tool also works on the platform's tables as they are where ids are plain
-- ints and created_at has no default: it assigns ids under a lock and stamps
-- the times itself.

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

-- patterns: what the tool records about a pattern beyond the platform's columns.
IF COL_LENGTH('sdm.patterns', 'sgsg_ck') IS NULL
    ALTER TABLE sdm.patterns ADD sgsg_ck NVARCHAR(500) NULL;
GO

IF COL_LENGTH('sdm.patterns', 'lobd_id') IS NULL
    ALTER TABLE sdm.patterns ADD lobd_id NVARCHAR(500) NULL;
GO

IF COL_LENGTH('sdm.patterns', 'sgsg_name') IS NULL
    ALTER TABLE sdm.patterns ADD sgsg_name NVARCHAR(200) NULL;
GO

IF COL_LENGTH('sdm.patterns', 'entity') IS NULL
    ALTER TABLE sdm.patterns ADD entity NVARCHAR(20) NOT NULL CONSTRAINT DF_patterns_entity DEFAULT 'members';
GO

-- The scope a pattern was found in, in the business pack's words (NULL on
-- rows written before it: read from the hierarchy columns).
IF COL_LENGTH('sdm.patterns', 'scope_json') IS NULL
    ALTER TABLE sdm.patterns ADD scope_json NVARCHAR(MAX) NULL;
GO

IF COL_LENGTH('sdm.patterns', 'origin') IS NULL
    ALTER TABLE sdm.patterns ADD origin NVARCHAR(20) NOT NULL CONSTRAINT DF_patterns_origin DEFAULT 'discovered';
GO

IF COL_LENGTH('sdm.patterns', 'combo_key') IS NULL
    ALTER TABLE sdm.patterns ADD combo_key NVARCHAR(400) NULL;
GO

IF COL_LENGTH('sdm.patterns', 'created_by') IS NULL
    ALTER TABLE sdm.patterns ADD created_by NVARCHAR(100) NULL;
GO

IF COL_LENGTH('sdm.patterns', 'certified_at') IS NULL
    ALTER TABLE sdm.patterns ADD certified_at DATETIME2 NULL;
GO

IF COL_LENGTH('sdm.patterns', 'certified_by') IS NULL
    ALTER TABLE sdm.patterns ADD certified_by NVARCHAR(100) NULL;
GO

IF COL_LENGTH('sdm.patterns', 'certified_against_id') IS NULL
    ALTER TABLE sdm.patterns ADD certified_against_id INT NULL;
GO

-- pattern_rules
IF COL_LENGTH('sdm.pattern_rules', 'member_count') IS NULL
    ALTER TABLE sdm.pattern_rules ADD member_count INT NULL;
GO

IF COL_LENGTH('sdm.pattern_rules', 'created_by') IS NULL
    ALTER TABLE sdm.pattern_rules ADD created_by NVARCHAR(100) NULL;
GO

-- pattern_tags
IF COL_LENGTH('sdm.pattern_tags', 'confidence') IS NULL
    ALTER TABLE sdm.pattern_tags ADD confidence DECIMAL(5,4) NULL;
GO

-- generation_rules: the element a rule produces, and the measured shape of a number.
IF COL_LENGTH('sdm.generation_rules', 'element') IS NULL
    ALTER TABLE sdm.generation_rules ADD element NVARCHAR(80) NULL;
GO

IF COL_LENGTH('sdm.generation_rules', 'min_value') IS NULL
    ALTER TABLE sdm.generation_rules ADD min_value FLOAT NULL;
GO

IF COL_LENGTH('sdm.generation_rules', 'max_value') IS NULL
    ALTER TABLE sdm.generation_rules ADD max_value FLOAT NULL;
GO

IF COL_LENGTH('sdm.generation_rules', 'decimals') IS NULL
    ALTER TABLE sdm.generation_rules ADD decimals INT NULL;
GO

IF COL_LENGTH('sdm.generation_rules', 'weights') IS NULL
    ALTER TABLE sdm.generation_rules ADD weights NVARCHAR(MAX) NULL;
GO

IF COL_LENGTH('sdm.generation_rules', 'distribution') IS NULL
    ALTER TABLE sdm.generation_rules ADD distribution NVARCHAR(20) NULL;
GO

IF COL_LENGTH('sdm.generation_rules', 'mean_value') IS NULL
    ALTER TABLE sdm.generation_rules ADD mean_value FLOAT NULL;
GO

IF COL_LENGTH('sdm.generation_rules', 'stddev_value') IS NULL
    ALTER TABLE sdm.generation_rules ADD stddev_value FLOAT NULL;
GO

-- New tables
IF NOT EXISTS (SELECT 1 FROM sys.objects
               WHERE object_id = OBJECT_ID(N'sdm.layouts') AND type = N'U')
CREATE TABLE sdm.layouts (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    name         NVARCHAR(200) NOT NULL,
    kind         NVARCHAR(10)  NOT NULL,          -- 'csv' or 'fixed'
    delimiter    NVARCHAR(5)   NULL,              -- csv only
    status       NVARCHAR(20)  NOT NULL DEFAULT 'pending_review',
    field_spec   NVARCHAR(MAX) NOT NULL,          -- parsed fields + assignments
    sample_name  NVARCHAR(400) NULL,
    created_by   NVARCHAR(100) NULL,
    created_at   DATETIME2     NOT NULL DEFAULT GETDATE(),
    approved_by  NVARCHAR(100) NULL,
    approved_at  DATETIME2     NULL,
    -- Who confirmed an approval at the computer (the operating-system user).
    confirmed_by NVARCHAR(100) NULL,
    -- Who last edited the layout; created_by stays its author.
    updated_by   NVARCHAR(100) NULL,
    -- What the layout is for: 'generation'. Masking is not SDM's (edi-mask's);
    -- a layout an earlier install saved as 'masking' keeps its value.
    purpose      NVARCHAR(20)  NOT NULL DEFAULT 'generation',
    CONSTRAINT UQ_layouts_name UNIQUE (name)
);
GO

IF COL_LENGTH('sdm.layouts', 'purpose') IS NULL
    ALTER TABLE sdm.layouts
        ADD purpose NVARCHAR(20) NOT NULL DEFAULT 'generation';
GO

IF COL_LENGTH('sdm.layouts', 'confirmed_by') IS NULL
    ALTER TABLE sdm.layouts ADD confirmed_by NVARCHAR(100) NULL;
GO

IF COL_LENGTH('sdm.layouts', 'updated_by') IS NULL
    ALTER TABLE sdm.layouts ADD updated_by NVARCHAR(100) NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.objects
               WHERE object_id = OBJECT_ID(N'sdm.generation_rule_aliases') AND type = N'U')
CREATE TABLE sdm.generation_rule_aliases (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    rule_id     INT           NOT NULL,
    -- One column name can only ever mean one element. The constraint is the
    -- point: without it two rules could both claim MEME_FIRST_NAME and
    -- resolution would pick whichever row came back first, which is a bug that
    -- only shows up as quietly wrong data.
    column_name NVARCHAR(200) NOT NULL UNIQUE,
    created_by  NVARCHAR(100) NULL,
    created_at  DATETIME2     NOT NULL DEFAULT GETDATE()
);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.objects
    WHERE object_id = OBJECT_ID(N'sdm.pattern_baseline') AND type = N'U'
)
CREATE TABLE sdm.pattern_baseline (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    entity        NVARCHAR(20)  NOT NULL,   -- 'members' or 'claims'
    scope_key     NVARCHAR(200) NOT NULL,   -- 'ALL', or 'GRGR:10042'
    scope_label   NVARCHAR(300) NULL,
    n_patterns    INT           NOT NULL DEFAULT 0,
    total_count   BIGINT        NOT NULL DEFAULT 0,
    detail_json   NVARCHAR(MAX) NULL,
    computed_at   DATETIME2     NOT NULL DEFAULT GETDATE(),
    CONSTRAINT UQ_pattern_baseline UNIQUE (entity, scope_key)
);
GO

-- Which features system discovered patterns are found on (the Configure
-- screen). Empty means the defaults. The tool also creates it if missing.
IF OBJECT_ID(N'sdm.discovery_features', N'U') IS NULL
CREATE TABLE sdm.discovery_features (
    entity     NVARCHAR(20)  NOT NULL,
    feature    NVARCHAR(60)  NOT NULL,
    chosen_by  NVARCHAR(100) NULL,
    chosen_at  DATETIME2     NULL,
    CONSTRAINT PK_discovery_features PRIMARY KEY (entity, feature)
);
GO
