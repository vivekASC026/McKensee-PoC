-- What this script may and may not destroy
--
-- It destroys no table and no row. Every table below -- the lookup tables and the ones
-- holding a person's work (saved patterns, their rules and tags, generation
-- history, validation rules) -- is created only if absent, and lookup seed rows
-- go only into a table that is empty, so they never double on a re-run.
-- Rebuilding the fixture runs this script, and a rebuild emptying the pattern
-- catalog is the kind of data loss nobody reports because it looks like
-- nothing happened.
--
-- The one thing replaced on every run is the pair of views at the end
-- (sdm.member_denorm, sdm.claims_denorm), which are this tool's own definition
-- over the demo FACETS tables and are run only in the source database. A hand
-- edit to either view is replaced by the definition here; a table of the same
-- name is never dropped -- the script stops with an error instead.

-- The pattern analyzer's own tables.
--
-- Adapted from the analyzer's SDM_SQL_Server_Setup.sql. The original created a
-- separate SDM_Platform database and switched into it; here the schema is
-- created in whichever database the connection is already pointed at, which is
-- the same one holding the FACETS tables it describes. A pattern found by the
-- analyzer is then about the rows this tool generates, not about a copy of
-- them in another database.
--
-- Everything below the header is the analyzer's own definition, unchanged.

-- Add-only. Nothing here drops a table, a key or a row: the store may be a
-- customer's, holding tables and rows that are not this tool's. Every table is
-- created only if absent, every index only if missing, and seed rows go only
-- into a table that is empty.

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

-- The session options scripts\customer\setup_tables.sql sets, so a run from
-- sqlcmd or SSMS (where QUOTED_IDENTIFIER may be off) creates every object
-- with the same options as the customer script. NOCOUNT is left as the caller
-- has it, since it changes the row counts a caller reads on the connection.
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET ARITHABORT ON;
SET NUMERIC_ROUNDABORT OFF;
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sdm')
    EXEC('CREATE SCHEMA sdm');
GO

-- ============================================================================
-- LOOKUP TABLES — Governed reference data for synthetic generation
-- ============================================================================

-- First names (gender-tagged)
IF OBJECT_ID('sdm.lkp_first_names', 'U') IS NULL
CREATE TABLE sdm.lkp_first_names (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    name        NVARCHAR(50)    NOT NULL,
    gender      CHAR(1)         NOT NULL,  -- M or F
    created_at  DATETIME2       NOT NULL DEFAULT GETDATE()
);

-- Last names
IF OBJECT_ID('sdm.lkp_last_names', 'U') IS NULL
CREATE TABLE sdm.lkp_last_names (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    name        NVARCHAR(50)    NOT NULL,
    created_at  DATETIME2       NOT NULL DEFAULT GETDATE()
);

-- Street names
IF OBJECT_ID('sdm.lkp_street_names', 'U') IS NULL
CREATE TABLE sdm.lkp_street_names (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    street      NVARCHAR(50)    NOT NULL,
    type        NVARCHAR(10)    NOT NULL,  -- St, Ave, Dr, Blvd, etc.
    created_at  DATETIME2       NOT NULL DEFAULT GETDATE()
);

-- ZIP / City / State / County (the service area)
IF OBJECT_ID('sdm.lkp_zip_city_state', 'U') IS NULL
CREATE TABLE sdm.lkp_zip_city_state (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    zip         CHAR(5)         NOT NULL,
    city        NVARCHAR(50)    NOT NULL,
    state       CHAR(2)         NOT NULL,
    county      NVARCHAR(50)    NOT NULL,
    created_at  DATETIME2       NOT NULL DEFAULT GETDATE()
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_lkp_zip_state' AND object_id = OBJECT_ID('sdm.lkp_zip_city_state'))
CREATE INDEX IX_lkp_zip_state ON sdm.lkp_zip_city_state (state);


-- ============================================================================
-- PATTERN METADATA — Discovered patterns, tags, and vocabulary
-- ============================================================================

-- Patterns (system-discovered)
IF OBJECT_ID('sdm.patterns', 'U') IS NULL
CREATE TABLE sdm.patterns (
    id                  INT IDENTITY(1,1) PRIMARY KEY,
    pattern_key         NVARCHAR(200)   NOT NULL UNIQUE,
    contextual_name     NVARCHAR(200)   NOT NULL,
    grgr_ck             NVARCHAR(500),          -- JSON array of group keys
    sgsg_ck             NVARCHAR(500),          -- JSON array of subgroup keys
    cspd_cat            NVARCHAR(500),          -- JSON array of plan categories
    lobd_id             NVARCHAR(500),          -- JSON array of LOB IDs
    grgr_name           NVARCHAR(200),
    sgsg_name           NVARCHAR(200),
    cspd_cat_desc       NVARCHAR(200),
    plds_desc           NVARCHAR(200),
    cluster_id          INT             NOT NULL,
    member_count        INT             NOT NULL,
    pct_of_pop          DECIMAL(8,6),
    silhouette          DECIMAL(8,6),
    profile_json        NVARCHAR(MAX),          -- Full profile data
    ai_summary          NVARCHAR(MAX),
    created_at          DATETIME2       NOT NULL DEFAULT GETDATE(),
    updated_at          DATETIME2       NOT NULL DEFAULT GETDATE()
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_patterns_name' AND object_id = OBJECT_ID('sdm.patterns'))
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_patterns_name' AND object_id = OBJECT_ID('sdm.patterns'))
CREATE INDEX IX_patterns_name ON sdm.patterns (contextual_name);

-- Pattern rules (user-saved pattern configurations)
IF OBJECT_ID('sdm.pattern_rules', 'U') IS NULL
CREATE TABLE sdm.pattern_rules (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    pattern_id      INT             REFERENCES sdm.patterns(id),
    rule_name       NVARCHAR(200)   NOT NULL,
    rule_text       NVARCHAR(MAX)   NOT NULL,
    member_count    INT,
    created_by      NVARCHAR(100)   DEFAULT 'system',
    created_at      DATETIME2       NOT NULL DEFAULT GETDATE()
);

-- Pattern tags
IF OBJECT_ID('sdm.pattern_tags', 'U') IS NULL
CREATE TABLE sdm.pattern_tags (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    pattern_id      INT             NOT NULL REFERENCES sdm.patterns(id),
    tag             NVARCHAR(100)   NOT NULL,
    tag_source      NVARCHAR(50)    NOT NULL DEFAULT 'auto',
    confidence      DECIMAL(5,4)    DEFAULT 1.0,
    confirmed_by    NVARCHAR(100),
    confirmed_at    DATETIME2,
    created_at      DATETIME2       NOT NULL DEFAULT GETDATE(),
    CONSTRAINT UQ_pattern_tag UNIQUE (pattern_id, tag)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_pattern_tags_tag' AND object_id = OBJECT_ID('sdm.pattern_tags'))
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_pattern_tags_tag' AND object_id = OBJECT_ID('sdm.pattern_tags'))
CREATE INDEX IX_pattern_tags_tag ON sdm.pattern_tags (tag);

-- Tag vocabulary (synonyms for keyword search)
IF OBJECT_ID('sdm.tag_vocabulary', 'U') IS NULL
CREATE TABLE sdm.tag_vocabulary (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    canonical_tag   NVARCHAR(100)   NOT NULL,
    synonym         NVARCHAR(100)   NOT NULL,
    category        NVARCHAR(50),
    sme_confirmed   BIT             DEFAULT 0,
    confirmed_by    NVARCHAR(100),
    confirmed_at    DATETIME2,
    created_at      DATETIME2       NOT NULL DEFAULT GETDATE(),
    CONSTRAINT UQ_tag_synonym UNIQUE (canonical_tag, synonym)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_tag_vocab_synonym' AND object_id = OBJECT_ID('sdm.tag_vocabulary'))
CREATE INDEX IX_tag_vocab_synonym ON sdm.tag_vocabulary (synonym);


-- ============================================================================
-- GENERATION — Rules, configuration, and audit log
-- ============================================================================

-- Generation rules (per-field configuration)
-- Created once and never dropped: a person edits these, and a rebuild that
-- recreated the table lost every edit without saying so. The aliases table
-- also holds a foreign key to it, so a drop would fail outright.
IF OBJECT_ID('sdm.generation_rules', 'U') IS NULL
CREATE TABLE sdm.generation_rules (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    field_name      NVARCHAR(100)   NOT NULL UNIQUE,
    field_label     NVARCHAR(200)   NOT NULL,
    field_category  NVARCHAR(50)    NOT NULL DEFAULT 'denorm',
    -- What a rule produces, as the catalog names it. A rule is referred to by
    -- this and not by its column: a column is a fact about one database, and
    -- anything naming a rule should work against any of them.
    element         NVARCHAR(80)    NULL,
    gen_method      NVARCHAR(50)    NOT NULL DEFAULT 'sequential',
    data_type       NVARCHAR(50)    NOT NULL DEFAULT 'alphanumeric',
    length          INT             DEFAULT 10,
    prefix          NVARCHAR(50)    DEFAULT '',
    postfix         NVARCHAR(50)    DEFAULT '',
    start_value     INT             DEFAULT 1,
    -- A value set is as long as it is: six hundred first names is an ordinary
    -- lookup and did not fit a short column, so those rules failed to seed.
    domain          NVARCHAR(MAX)   NULL,
    format_pattern  NVARCHAR(200)   DEFAULT '',
    lookup_source   NVARCHAR(200)   DEFAULT '',
    -- The range a number was measured in, its places, and how often each
    -- value of a list occurs: a measurement, not only its shape.
    min_value       FLOAT           NULL,
    max_value       FLOAT           NULL,
    decimals        INT             NULL,
    weights         NVARCHAR(MAX)   NULL,
    -- How a number falls between its bounds, as the plan measured it: most
    -- charges are near a hundred and a few reach thousands, which an even
    -- draw between the two cannot say. Mean and deviation are of the
    -- underlying normal for a log-normal, as the profiler reports them.
    distribution    NVARCHAR(20)    NULL,
    mean_value      FLOAT           NULL,
    stddev_value    FLOAT           NULL,
    active          BIT             DEFAULT 1,
    updated_by      NVARCHAR(100)   DEFAULT 'system',
    updated_at      DATETIME2       NOT NULL DEFAULT GETDATE()
);

-- Columns added after the table was first created, for a store that predates them.
IF COL_LENGTH('sdm.generation_rules', 'element') IS NULL
    ALTER TABLE sdm.generation_rules ADD element NVARCHAR(80) NULL;
IF COL_LENGTH('sdm.generation_rules', 'min_value') IS NULL
    ALTER TABLE sdm.generation_rules ADD min_value FLOAT NULL;
IF COL_LENGTH('sdm.generation_rules', 'max_value') IS NULL
    ALTER TABLE sdm.generation_rules ADD max_value FLOAT NULL;
IF COL_LENGTH('sdm.generation_rules', 'decimals') IS NULL
    ALTER TABLE sdm.generation_rules ADD decimals INT NULL;
IF COL_LENGTH('sdm.generation_rules', 'weights') IS NULL
    ALTER TABLE sdm.generation_rules ADD weights NVARCHAR(MAX) NULL;
IF COL_LENGTH('sdm.generation_rules', 'distribution') IS NULL
    ALTER TABLE sdm.generation_rules ADD distribution NVARCHAR(20) NULL;
IF COL_LENGTH('sdm.generation_rules', 'mean_value') IS NULL
    ALTER TABLE sdm.generation_rules ADD mean_value FLOAT NULL;
IF COL_LENGTH('sdm.generation_rules', 'stddev_value') IS NULL
    ALTER TABLE sdm.generation_rules ADD stddev_value FLOAT NULL;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_gen_rules_field'
               AND object_id = OBJECT_ID('sdm.generation_rules'))
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_gen_rules_field' AND object_id = OBJECT_ID('sdm.generation_rules'))
CREATE INDEX IX_gen_rules_field ON sdm.generation_rules (field_name);

-- Generation log (audit trail)
IF OBJECT_ID('sdm.generation_log', 'U') IS NULL
CREATE TABLE sdm.generation_log (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    pattern_ids     NVARCHAR(MAX)   NOT NULL,   -- JSON array
    data_type       NVARCHAR(50)    NOT NULL,   -- members, claims, enrollments
    volume          INT             NOT NULL,
    output_path     NVARCHAR(500),
    weighting       NVARCHAR(MAX),              -- JSON
    requested_by    NVARCHAR(100)   DEFAULT 'user',
    created_at      DATETIME2       NOT NULL DEFAULT GETDATE()
);


-- ============================================================================
-- SOURCE DATA — Member denorm (current POC structure)
-- Replace with Config Denorm + direct source reads when redesigning
-- ============================================================================

-- member_denorm is a view over the FACETS tables; see the end of this file.


-- ============================================================================
-- SEED DATA — Lookup tables and tag vocabulary
-- ============================================================================

-- First names

IF NOT EXISTS (SELECT 1 FROM sdm.lkp_first_names)
BEGIN
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'James', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'John', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Robert', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Michael', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'William', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'David', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Richard', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Joseph', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Thomas', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Charles', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Christopher', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Daniel', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Matthew', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Anthony', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Mark', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Donald', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Steven', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Andrew', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Paul', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Joshua', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Kenneth', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Kevin', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Brian', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'George', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Timothy', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Ronald', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Edward', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jason', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jeffrey', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Ryan', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jacob', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Gary', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Nicholas', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Eric', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jonathan', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Stephen', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Larry', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Justin', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Scott', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Brandon', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Benjamin', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Samuel', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Raymond', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Gregory', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Frank', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Alexander', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Patrick', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jack', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Dennis', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jerry', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Tyler', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Aaron', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Nathan', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Henry', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Peter', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Adam', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Douglas', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Zachary', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Walter', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Kyle', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Harold', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Carl', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Arthur', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Gerald', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Roger', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Keith', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Lawrence', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Terry', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Sean', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Albert', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jesse', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Austin', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Bruce', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Willie', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Ralph', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Roy', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Eugene', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Randy', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Philip', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Russell', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Bobby', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Howard', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Louis', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Wayne', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Dylan', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Ethan', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jordan', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Christian', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Mason', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Logan', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Caleb', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Luke', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Isaac', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Connor', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Elijah', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Gabriel', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Owen', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Liam', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Noah', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Aiden', N'M');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Mary', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Patricia', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jennifer', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Linda', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Barbara', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Elizabeth', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Susan', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jessica', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Sarah', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Karen', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Lisa', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Nancy', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Betty', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Margaret', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Sandra', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Ashley', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Dorothy', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Kimberly', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Emily', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Donna', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Michelle', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Carol', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Amanda', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Melissa', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Deborah', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Stephanie', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Rebecca', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Sharon', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Laura', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Cynthia', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Kathleen', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Amy', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Angela', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Shirley', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Anna', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Brenda', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Pamela', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Emma', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Nicole', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Helen', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Samantha', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Katherine', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Christine', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Debra', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Rachel', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Carolyn', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Janet', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Catherine', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Maria', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Heather', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Diane', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Ruth', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Julie', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Olivia', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Joyce', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Virginia', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Victoria', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Kelly', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Lauren', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Christina', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Joan', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Evelyn', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Judith', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Megan', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Andrea', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Cheryl', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Hannah', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jacqueline', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Martha', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Gloria', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Teresa', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Ann', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Sara', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Madison', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Frances', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Kathryn', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Janice', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Jean', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Abigail', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Alice', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Judy', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Sophia', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Grace', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Denise', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Amber', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Doris', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Marilyn', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Danielle', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Beverly', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Isabella', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Theresa', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Diana', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Natalie', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Brittany', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Charlotte', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Marie', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Kayla', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Alexis', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Lori', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Ava', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Mia', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Chloe', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Ella', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Lily', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Zoe', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Leah', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Allison', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Savannah', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Audrey', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Brooklyn', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Claire', N'F');
INSERT INTO sdm.lkp_first_names (name, gender) VALUES (N'Addison', N'F');
END;

-- Last names

IF NOT EXISTS (SELECT 1 FROM sdm.lkp_last_names)
BEGIN
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Smith');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Johnson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Williams');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Brown');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Jones');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Garcia');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Miller');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Davis');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Rodriguez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Martinez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Hernandez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Lopez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Gonzalez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Wilson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Anderson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Thomas');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Taylor');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Moore');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Jackson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Martin');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Lee');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Perez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Thompson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'White');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Harris');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Sanchez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Clark');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Ramirez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Lewis');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Robinson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Walker');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Young');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Allen');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'King');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Wright');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Scott');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Torres');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Nguyen');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Hill');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Flores');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Green');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Adams');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Nelson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Baker');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Hall');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Rivera');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Campbell');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Mitchell');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Carter');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Roberts');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Gomez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Phillips');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Evans');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Turner');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Diaz');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Parker');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Cruz');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Edwards');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Collins');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Reyes');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Stewart');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Morris');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Morales');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Murphy');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Cook');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Rogers');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Gutierrez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Ortiz');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Morgan');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Cooper');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Peterson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Bailey');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Reed');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Kelly');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Howard');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Ramos');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Kim');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Cox');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Ward');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Richardson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Watson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Brooks');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Chavez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Wood');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'James');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Bennett');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Gray');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Mendoza');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Ruiz');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Hughes');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Price');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Alvarez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Castillo');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Sanders');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Patel');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Myers');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Long');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Ross');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Foster');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Jimenez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Powell');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Jenkins');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Perry');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Russell');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Sullivan');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Bell');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Coleman');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Butler');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Henderson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Barnes');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Gonzales');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Fisher');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Vasquez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Simmons');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Graham');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Murray');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Ford');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Castro');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Marshall');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Owens');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Harrison');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Fernandez');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Patterson');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Wallace');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'West');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Webb');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Tucker');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Burns');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Crawford');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Mason');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Hunt');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Warren');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Bishop');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Dixon');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Wagner');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Spencer');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Freeman');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Stone');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Horton');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Hoffman');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Carpenter');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'May');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Knight');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Hart');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Ray');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Armstrong');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Day');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Montgomery');
INSERT INTO sdm.lkp_last_names (name) VALUES (N'Andrews');
END;

-- Street names

IF NOT EXISTS (SELECT 1 FROM sdm.lkp_street_names)
BEGIN
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Main', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Oak', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Maple', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Cedar', N'Ln');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Elm', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Pine', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Washington', N'Blvd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Park', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Lake', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Hill', N'Rd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Church', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Spring', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'High', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Market', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Center', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Union', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Bridge', N'Rd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Broad', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'River', N'Rd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Walnut', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Cherry', N'Ln');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Meadow', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Forest', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Summit', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Valley', N'Rd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Lincoln', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Franklin', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Jefferson', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Madison', N'Blvd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Adams', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Monroe', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Harrison', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Jackson', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Grant', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Garfield', N'Rd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Cleveland', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Wilson', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Roosevelt', N'Blvd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Kennedy', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Reagan', N'Ct');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Sunset', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Sunrise', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Country', N'Ln');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Heritage', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Liberty', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Eagle', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Cardinal', N'Ln');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Willow', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Birch', N'Ct');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Sycamore', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Dogwood', N'Ln');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Magnolia', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Chestnut', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Hickory', N'Ln');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Ash', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Beech', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Poplar', N'St');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Spruce', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Holly', N'Ln');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Ivy', N'Ct');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Laurel', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Orchard', N'Ln');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Garden', N'Ave');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Vineyard', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Ridge', N'Rd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Creek', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Brook', N'Ln');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Pond', N'Dr');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Stone', N'Rd');
INSERT INTO sdm.lkp_street_names (street, type) VALUES (N'Mill', N'Rd');
END;

-- ZIP / City / State

IF NOT EXISTS (SELECT 1 FROM sdm.lkp_zip_city_state)
BEGIN
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43215', N'Columbus', N'OH', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43085', N'Columbus', N'OH', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43201', N'Columbus', N'OH', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43220', N'Columbus', N'OH', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44114', N'Cleveland', N'OH', N'Cuyahoga');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44106', N'Cleveland', N'OH', N'Cuyahoga');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44120', N'Cleveland', N'OH', N'Cuyahoga');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44130', N'Cleveland', N'OH', N'Cuyahoga');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45202', N'Cincinnati', N'OH', N'Hamilton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45219', N'Cincinnati', N'OH', N'Hamilton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45229', N'Cincinnati', N'OH', N'Hamilton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45238', N'Cincinnati', N'OH', N'Hamilton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45402', N'Dayton', N'OH', N'Montgomery');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45409', N'Dayton', N'OH', N'Montgomery');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45424', N'Dayton', N'OH', N'Montgomery');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43604', N'Toledo', N'OH', N'Lucas');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43606', N'Toledo', N'OH', N'Lucas');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43612', N'Toledo', N'OH', N'Lucas');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44308', N'Akron', N'OH', N'Summit');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44313', N'Akron', N'OH', N'Summit');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44320', N'Akron', N'OH', N'Summit');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44503', N'Youngstown', N'OH', N'Mahoning');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44512', N'Youngstown', N'OH', N'Mahoning');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45601', N'Chillicothe', N'OH', N'Ross');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45701', N'Athens', N'OH', N'Athens');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43055', N'Newark', N'OH', N'Licking');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43015', N'Delaware', N'OH', N'Delaware');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43123', N'Grove City', N'OH', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43068', N'Reynoldsburg', N'OH', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43081', N'Westerville', N'OH', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43026', N'Hilliard', N'OH', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'43235', N'Columbus', N'OH', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44035', N'Elyria', N'OH', N'Lorain');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44256', N'Medina', N'OH', N'Medina');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'44691', N'Wooster', N'OH', N'Wayne');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45066', N'Springboro', N'OH', N'Warren');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45040', N'Mason', N'OH', N'Warren');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'45069', N'West Chester', N'OH', N'Butler');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40202', N'Louisville', N'KY', N'Jefferson');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40205', N'Louisville', N'KY', N'Jefferson');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40214', N'Louisville', N'KY', N'Jefferson');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40220', N'Louisville', N'KY', N'Jefferson');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40241', N'Louisville', N'KY', N'Jefferson');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40258', N'Louisville', N'KY', N'Jefferson');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40502', N'Lexington', N'KY', N'Fayette');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40508', N'Lexington', N'KY', N'Fayette');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40513', N'Lexington', N'KY', N'Fayette');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40517', N'Lexington', N'KY', N'Fayette');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'41011', N'Covington', N'KY', N'Kenton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'41017', N'Ft Mitchell', N'KY', N'Kenton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'41042', N'Florence', N'KY', N'Boone');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'41048', N'Hebron', N'KY', N'Boone');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'42001', N'Paducah', N'KY', N'McCracken');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'42101', N'Bowling Green', N'KY', N'Warren');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'42301', N'Owensboro', N'KY', N'Daviess');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40601', N'Frankfort', N'KY', N'Franklin');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40701', N'Corbin', N'KY', N'Whitley');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'40741', N'London', N'KY', N'Laurel');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'41501', N'Pikeville', N'KY', N'Pike');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'41101', N'Ashland', N'KY', N'Boyd');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46204', N'Indianapolis', N'IN', N'Marion');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46220', N'Indianapolis', N'IN', N'Marion');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46237', N'Indianapolis', N'IN', N'Marion');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46250', N'Indianapolis', N'IN', N'Marion');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46268', N'Indianapolis', N'IN', N'Marion');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46802', N'Fort Wayne', N'IN', N'Allen');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46815', N'Fort Wayne', N'IN', N'Allen');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46901', N'Kokomo', N'IN', N'Howard');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'47374', N'Richmond', N'IN', N'Wayne');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'47906', N'West Lafayette', N'IN', N'Tippecanoe');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'47401', N'Bloomington', N'IN', N'Monroe');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46060', N'Noblesville', N'IN', N'Hamilton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46032', N'Carmel', N'IN', N'Hamilton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'46143', N'Greenwood', N'IN', N'Johnson');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'47130', N'Jeffersonville', N'IN', N'Clark');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30301', N'Atlanta', N'GA', N'Fulton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30309', N'Atlanta', N'GA', N'Fulton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30318', N'Atlanta', N'GA', N'Fulton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30324', N'Atlanta', N'GA', N'DeKalb');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30340', N'Atlanta', N'GA', N'DeKalb');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30004', N'Alpharetta', N'GA', N'Fulton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30022', N'Alpharetta', N'GA', N'Fulton');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30060', N'Marietta', N'GA', N'Cobb');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30080', N'Smyrna', N'GA', N'Cobb');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30043', N'Lawrenceville', N'GA', N'Gwinnett');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30044', N'Lawrenceville', N'GA', N'Gwinnett');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30096', N'Duluth', N'GA', N'Gwinnett');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30188', N'Woodstock', N'GA', N'Cherokee');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30501', N'Gainesville', N'GA', N'Hall');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'31201', N'Macon', N'GA', N'Bibb');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'30901', N'Augusta', N'GA', N'Richmond');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'31601', N'Valdosta', N'GA', N'Lowndes');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'25301', N'Charleston', N'WV', N'Kanawha');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'25302', N'Charleston', N'WV', N'Kanawha');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'25314', N'Charleston', N'WV', N'Kanawha');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'25701', N'Huntington', N'WV', N'Cabell');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'26003', N'Wheeling', N'WV', N'Ohio');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'26501', N'Morgantown', N'WV', N'Monongalia');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'26505', N'Morgantown', N'WV', N'Monongalia');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'25401', N'Martinsburg', N'WV', N'Berkeley');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'26101', N'Parkersburg', N'WV', N'Wood');
INSERT INTO sdm.lkp_zip_city_state (zip, city, state, county) VALUES (N'25801', N'Beckley', N'WV', N'Raleigh');
END;

-- Tag vocabulary (synonym resolution for keyword search)

IF NOT EXISTS (SELECT 1 FROM sdm.tag_vocabulary)
BEGIN
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'pediatric', N'pediatric', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'pediatric', N'child', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'pediatric', N'children', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'pediatric', N'minor', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'pediatric', N'under-18', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'adult', N'adult', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'adult', N'working-age', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'senior', N'senior', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'senior', N'elderly', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'senior', N'65+', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'senior', N'medicare-age', N'age', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'new-member', N'new-member', N'tenure', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'new-member', N'new', N'tenure', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'new-member', N'recent', N'tenure', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'long-term', N'long-term', N'tenure', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'long-term', N'established', N'tenure', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'long-term', N'loyal', N'tenure', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'family', N'family', N'family', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'family', N'with-dependents', N'family', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'single', N'single', N'family', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'single', N'individual', N'family', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'single', N'no-dependents', N'family', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'dental', N'dental', N'plan', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'dental', N'dental-plan', N'plan', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'medical', N'medical', N'plan', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'medical', N'med', N'plan', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'vision', N'vision', N'plan', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'vision', N'eye', N'plan', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'medicaid', N'medicaid', N'lob', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'medicare', N'medicare', N'lob', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'medicare-advantage', N'medicare-advantage', N'lob', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'medicare-advantage', N'ma', N'lob', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'exchange', N'exchange', N'lob', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'exchange', N'marketplace', N'lob', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'exchange', N'aca', N'lob', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'edge-case', N'edge-case', N'quality', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'edge-case', N'rare', N'quality', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'edge-case', N'anomaly', N'quality', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'edge-case', N'outlier', N'quality', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'edge-case', N'unusual', N'quality', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'high-volume', N'high-volume', N'quality', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'high-volume', N'common', N'quality', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'high-volume', N'dominant', N'quality', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'ohio', N'ohio', N'state', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'ohio', N'oh', N'state', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'kentucky', N'kentucky', N'state', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'kentucky', N'ky', N'state', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'indiana', N'indiana', N'state', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'indiana', N'in', N'state', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'georgia', N'georgia', N'state', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'georgia', N'ga', N'state', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'west-virginia', N'west-virginia', N'state', 0);
INSERT INTO sdm.tag_vocabulary (canonical_tag, synonym, category, sme_confirmed) VALUES (N'west-virginia', N'wv', N'state', 0);
END;



-- ============================================================================
-- VERIFICATION
-- ============================================================================

SELECT 'lkp_first_names' AS table_name, COUNT(*) AS row_count FROM sdm.lkp_first_names
UNION ALL SELECT 'lkp_last_names', COUNT(*) FROM sdm.lkp_last_names
UNION ALL SELECT 'lkp_street_names', COUNT(*) FROM sdm.lkp_street_names
UNION ALL SELECT 'lkp_zip_city_state', COUNT(*) FROM sdm.lkp_zip_city_state
UNION ALL SELECT 'tag_vocabulary', COUNT(*) FROM sdm.tag_vocabulary
UNION ALL SELECT 'generation_rules', COUNT(*) FROM sdm.generation_rules;

PRINT 'SDM Platform database setup complete.';
GO


-- ============================================================================
-- VALIDATION RULES — Configurable rules for validating generated data
-- ============================================================================

IF OBJECT_ID('sdm.validation_rules', 'U') IS NULL
CREATE TABLE sdm.validation_rules (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    rule_name       NVARCHAR(200)   NOT NULL,
    field_name      NVARCHAR(100)   NOT NULL,
    rule_type       NVARCHAR(50)    NOT NULL,   -- required, range, regex, cross_field, values
    expression      NVARCHAR(MAX)   NOT NULL,   -- JSON: {"min":0,"max":115} or {"pattern":"^[A-Z]"} or {"values":["M","F"]}
    severity        NVARCHAR(20)    NOT NULL DEFAULT 'error',  -- error or warning
    active          BIT             NOT NULL DEFAULT 1,
    created_by      NVARCHAR(100)   DEFAULT 'system',
    created_at      DATETIME2       NOT NULL DEFAULT GETDATE(),
    updated_at      DATETIME2       NOT NULL DEFAULT GETDATE()
);
GO

-- ============================================================================
-- GENERATION RUNS — Store parameters for replay / audit
-- ============================================================================

IF OBJECT_ID('sdm.generation_runs', 'U') IS NULL
CREATE TABLE sdm.generation_runs (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    run_name        NVARCHAR(200)   NOT NULL,
    pattern_ids     NVARCHAR(MAX)   NOT NULL,   -- JSON array
    data_type       NVARCHAR(50)    NOT NULL,   -- members, enrollments, claims
    volume          INT             NOT NULL,
    seed            INT             NOT NULL,   -- random seed for reproducibility
    rules_snapshot  NVARCHAR(MAX),              -- JSON snapshot of generation_rules at time of run
    filters_json    NVARCHAR(MAX),              -- JSON of hierarchy filters per pattern
    weighting       NVARCHAR(MAX),              -- JSON of pattern weights
    validation_result NVARCHAR(MAX),            -- JSON summary of validation pass/fail
    created_by      NVARCHAR(100)   DEFAULT 'user',
    created_at      DATETIME2       NOT NULL DEFAULT GETDATE()
);
GO


-- The source plans of Source setup: one row per source, its plan's snapshot and
-- hash, submitted, approved and built (sdg/accelerator/approval.py, which
-- holds the same statement). Add-only.
IF OBJECT_ID(N'sdm.source_plans', N'U') IS NULL
CREATE TABLE sdm.source_plans (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    source        NVARCHAR(32)  NOT NULL,
    pack_id       NVARCHAR(32)  NULL,
    status        NVARCHAR(20)  NOT NULL DEFAULT 'draft',
    plan_json     NVARCHAR(MAX) NOT NULL,
    plan_hash     CHAR(64)      NOT NULL,
    submitted_by  NVARCHAR(100) NULL,
    submitted_at  DATETIME2     NULL,
    approved_by   NVARCHAR(100) NULL,
    approved_at   DATETIME2     NULL,
    confirmed_by  NVARCHAR(100) NULL,
    built_hash    CHAR(64)      NULL,
    built_at      DATETIME2     NULL,
    built_plan_hash CHAR(64)    NULL,
    CONSTRAINT UQ_source_plans_source UNIQUE (source),
    CONSTRAINT CK_source_plans_status
        CHECK (status IN ('draft', 'pending_review', 'approved'))
);
GO
IF COL_LENGTH(N'sdm.source_plans', N'built_plan_hash') IS NULL
    ALTER TABLE sdm.source_plans ADD built_plan_hash CHAR(64) NULL;
GO
IF COL_LENGTH(N'sdm.source_plans', N'loaded_hash') IS NULL
    ALTER TABLE sdm.source_plans ADD loaded_hash CHAR(64) NULL;
GO

-- ============================================================================
-- Created on demand, deliberately not here
-- ============================================================================
-- sdm.layouts and sdm.pattern_baseline are created by the analyzer itself, on
-- first use, from the DDL that lives beside the code reading them. Declaring
-- them here as well produced two definitions that drifted apart: the copy in
-- this file was missing the columns the service selects, and because the table
-- then existed, the auto-create path saw nothing to do and the screens failed
-- on a column name instead. One definition, next to its reader.

-- ============================================================================
-- sdm.patterns, brought up to what the code reads
-- ============================================================================
-- The analyzer's setup script is older than the analyzer. Its sdm.patterns is
-- missing columns the service selects by name, and the database it was running
-- against had clearly been altered by hand -- the cached results carried
-- cspi_id, pdpd_id, saved_by and saved_at, none of which the script creates.
-- The provenance and certification columns shipped as a separate migration.
--
-- Everything below is guarded, so this runs on a fresh schema and on one that
-- has already been upgraded.

-- Hierarchy levels the pattern was found at. The script stops at plan
-- category; the code selects plan and product too.
IF COL_LENGTH('sdm.patterns', 'cspi_id') IS NULL
    ALTER TABLE sdm.patterns ADD cspi_id NVARCHAR(20) NULL;
GO
IF COL_LENGTH('sdm.patterns', 'pdpd_id') IS NULL
    ALTER TABLE sdm.patterns ADD pdpd_id NVARCHAR(20) NULL;
GO
IF COL_LENGTH('sdm.patterns', 'pdds_desc') IS NULL
    ALTER TABLE sdm.patterns ADD pdds_desc NVARCHAR(200) NULL;
GO

-- Who kept the pattern, and when. The saved screen reads both, and the change
-- counter uses COUNT(saved_at) to notice a save that did not change the row
-- count.
IF COL_LENGTH('sdm.patterns', 'saved_by') IS NULL
    ALTER TABLE sdm.patterns ADD saved_by NVARCHAR(100) NULL;
GO
IF COL_LENGTH('sdm.patterns', 'saved_at') IS NULL
    ALTER TABLE sdm.patterns ADD saved_at DATETIME2 NULL;
GO

-- The entity a pattern describes. Members and claims both persist here, and
-- without this they collide on one row: the dedup key is built from hierarchy
-- plus cluster, which is identical across domains for the same group.
IF COL_LENGTH('sdm.patterns', 'entity') IS NULL
    ALTER TABLE sdm.patterns ADD entity NVARCHAR(20) NOT NULL
        CONSTRAINT DF_patterns_entity DEFAULT 'members';
GO

-- The scope a pattern was found in or defined over, in the business pack's
-- own words: {"entity": <record key>, "filters": {<filter key>: [values]},
-- "labels": {<filter key>: <name>}}. Any pack's scope, where the columns
-- above hold only the health pack's four levels. NULL on a row written
-- before it: that row's scope is read from those columns.
IF COL_LENGTH('sdm.patterns', 'scope_json') IS NULL
    ALTER TABLE sdm.patterns ADD scope_json NVARCHAR(MAX) NULL;
GO

-- Provenance and certification, from SDM_Migration_Pattern_Provenance.sql.
-- A discovered pattern is evidence; a user-defined one is a hypothesis an SME
-- stated, and the tool cannot vouch for it.
IF COL_LENGTH('sdm.patterns', 'origin') IS NULL
    ALTER TABLE sdm.patterns ADD origin NVARCHAR(20) NOT NULL
        CONSTRAINT DF_patterns_origin DEFAULT 'discovered';
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

-- The build of a generated business pack a pattern was found under (the source
-- accelerator's pack id and build hash), and when it was archived: a load of
-- another build of the pack archives the patterns of the earlier one, kept
-- read-only. NULL for a pattern of a pack no build wrote.
IF COL_LENGTH('sdm.patterns', 'pack_id') IS NULL
    ALTER TABLE sdm.patterns ADD pack_id NVARCHAR(32) NULL;
GO

IF COL_LENGTH('sdm.patterns', 'plan_version') IS NULL
    ALTER TABLE sdm.patterns ADD plan_version NVARCHAR(64) NULL;
GO

IF COL_LENGTH('sdm.patterns', 'archived_at') IS NULL
    ALTER TABLE sdm.patterns ADD archived_at DATETIME2 NULL;
GO

-- ----------------------------------------------------------------------------
-- sdm.pattern_tags: the column the code reads is called `source`
-- ----------------------------------------------------------------------------
-- The setup script creates it as `tag_source`; every query in db.py selects and
-- inserts `source`. Nothing reads `tag_source` at all, so the script is simply
-- out of step with the code it ships beside -- which only surfaces when tagging
-- is reached, deep inside Story to Data.

IF COL_LENGTH('sdm.pattern_tags', 'tag_source') IS NOT NULL
   AND COL_LENGTH('sdm.pattern_tags', 'source') IS NULL
    EXEC sp_rename 'sdm.pattern_tags.tag_source', 'source', 'COLUMN';
GO
IF COL_LENGTH('sdm.pattern_tags', 'source') IS NULL
    ALTER TABLE sdm.pattern_tags ADD source NVARCHAR(20) NULL;
GO

-- ============================================================================
-- The denormalized views the analyzer reads
-- ============================================================================
-- The analyzer was written against two flat tables that something else had to
-- keep in step with the source. They are views here instead, so there is no
-- copy to refresh and no moment when the analyzer is describing data the
-- generator has already moved on from.

IF OBJECT_ID('sdm.member_denorm', 'V') IS NOT NULL DROP VIEW sdm.member_denorm;
-- A table of that name is someone's data, not this view: stop, never drop it.
IF OBJECT_ID('sdm.member_denorm', 'U') IS NOT NULL
    THROW 50001, N'sdm.member_denorm is a table in this database, so the view was not created. Run the view part of this script in the source database, not the analysis store.', 1;
-- Set again here: seed\bootstrap.py runs this part on its own, from the DROP
-- VIEW line above, and a view keeps the ANSI_NULLS and QUOTED_IDENTIFIER it was made with.
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET ARITHABORT ON;
SET NUMERIC_ROUNDABORT OFF;
GO
-- The customer's member_denorm, column for column, from the demo FACETS. What
-- the demo source does not hold is NULL, as it is wherever a source lacks it.
CREATE VIEW sdm.member_denorm AS
SELECT
    CAST(m.MEME_CK AS int)               AS MEME_CK,
    CAST(m.SBSB_CK AS int)               AS SBSB_CK,
    CAST(s.GRGR_CK AS int)               AS GRGR_CK,
    CAST(m.MEME_SFX AS int)              AS MEME_SFX,
    m.MEME_REL,
    m.MEME_LAST_NAME,
    m.MEME_FIRST_NAME,
    m.MEME_MID_INIT,
    m.MEME_SEX,
    m.MEME_BIRTH_DT,
    CAST(m.MEME_SSN AS bigint)           AS MEME_SSN,
    m.MEME_MCTR_STS,
    m.MEME_ORIG_EFF_DT,
    m.MEME_MARITAL_STATUS,
    CAST(NULL AS nvarchar(50))           AS MEME_MEDCD_NO,
    CAST(NULL AS nvarchar(50))           AS MEME_HICN,
    CAST(NULL AS nvarchar(50))           AS MEME_MCTR_RACE_NVL,
    CAST(NULL AS nvarchar(50))           AS MEME_MCTR_ETHN_NVL,
    s.SBSB_ID,
    s.SBSB_LAST_NAME,
    s.SBSB_FIRST_NAME,
    s.SBSB_ORIG_EFF_DT,
    s.SBSB_MCTR_STS,
    s.SBSB_EMPLOY_ID,
    CAST(s.SGSG_CK AS int)               AS SGSG_CK,
    sg.SGSG_ORIG_EFF_DT                  AS SBSG_EFF_DT,
    sg.SGSG_TERM_DT                      AS SBSG_TERM_DT,
    CAST(NULL AS nvarchar(10))           AS SBSG_MCTR_TRSN,
    sg.SGSG_ID,
    sg.SGSG_NAME,
    sg.CSCS_ID,
    sg.SGSG_STATE,
    sg.SGSG_STS,
    sg.SGSG_ORIG_EFF_DT,
    sg.SGSG_TERM_DT,
    g.GRGR_ID,
    g.GRGR_NAME,
    g.GRGR_STATE,
    g.GRGR_COUNTY,
    g.GRGR_STS,
    g.GRGR_ORIG_EFF_DT,
    g.GRGR_TERM_DT,
    g.GRGR_MCTR_TYPE,
    CAST(NULL AS int)                    AS PAGR_CK,
    p.CSPI_ID,
    p.CSPD_CAT,
    p.CSPI_EFF_DT,
    p.CSPI_TERM_DT,
    p.PDPD_ID,
    p.CSPI_SEL_IND,
    CAST(NULL AS nvarchar(50))           AS CSPI_HIOS_ID_NVL,
    c.CSPD_CAT_DESC,
    c.CSPD_TYPE,
    CAST(NULL AS nvarchar(50))           AS LOBD_ID,
    CAST(CASE WHEN pr.PDPD_TYPE = 'M' THEN 'Y' ELSE 'N' END AS nvarchar(10)) AS PDPD_RISK_IND,
    CAST(pr.PDPD_TYPE AS nvarchar(50))   AS PDPD_MCTR_CCAT,
    pl.PLDS_DESC,
    pd.PDDS_DESC,
    CAST(NULL AS varchar(200))           AS EMAIL
FROM dbo.CMC_MEME_MEMBER     AS m
JOIN dbo.CMC_SBSB_SUBSC      AS s  ON s.SBSB_CK = m.SBSB_CK
JOIN dbo.CMC_GRGR_GROUP      AS g  ON g.GRGR_CK = s.GRGR_CK
JOIN dbo.CMC_SGSG_SUBGROUP   AS sg ON sg.SGSG_CK = s.SGSG_CK
JOIN dbo.CMC_CSPI_CS_PLAN    AS p  ON p.CSPI_ID = s.CSPI_ID
JOIN dbo.CMC_CSPD_CAT_DESC   AS c  ON c.CSPD_CAT = p.CSPD_CAT
JOIN dbo.CMC_PDPD_PRODUCT    AS pr ON pr.PDPD_ID = p.PDPD_ID
JOIN dbo.CMC_PLDS_PLAN_DESC  AS pl ON pl.CSPI_ID = p.CSPI_ID
JOIN dbo.CMC_PDDS_PROD_DESC  AS pd ON pd.PDPD_ID = p.PDPD_ID;
GO

IF OBJECT_ID('sdm.claims_denorm', 'V') IS NOT NULL DROP VIEW sdm.claims_denorm;
IF OBJECT_ID('sdm.claims_denorm', 'U') IS NOT NULL
    THROW 50001, N'sdm.claims_denorm is a table in this database, so the view was not created. Run the view part of this script in the source database, not the analysis store.', 1;
GO
-- The customer's claims_denorm, column for column, one row per claim line.
CREATE VIEW sdm.claims_denorm AS
SELECT
    cl.CLCL_ID,
    CAST(l.CDML_SEQ_NO AS smallint)      AS CDML_SEQ_NO,
    cl.CLCL_CL_TYPE,
    CAST(NULL AS char(1))                AS CLCL_CL_SUB_TYPE,
    cl.CLCL_CUR_STS,
    CAST(NULL AS char(4))                AS CLST_MCTR_REAS,
    cl.CLCL_NTWK_IND,
    CAST(cl.CLCL_RECD_DT AS datetime)    AS CLCL_INPUT_DT,
    CAST(cl.CLCL_RECD_DT AS datetime)    AS CLCL_RECD_DT,
    CAST(cl.CLCL_PAID_DT AS datetime)    AS CLCL_PAID_DT,
    CAST(cl.CLCL_SVC_DT AS datetime)     AS CLCL_LOW_SVC_DT,
    CAST(cl.CLCL_SVC_DT AS datetime)     AS CLCL_HIGH_SVC_DT,
    CAST(cl.CLCL_TOT_CHG AS money)       AS CLCL_TOT_CHG,
    CAST(NULL AS money)                  AS CLCL_TOT_PAYABLE,
    CAST(cl.MEME_CK AS int)              AS MEME_CK,
    CAST(m.SBSB_CK AS int)               AS SBSB_CK,
    -- Age at the date of service, which is what a claims pattern is about.
    CAST(DATEDIFF(year, m.MEME_BIRTH_DT, cl.CLCL_SVC_DT) AS smallint) AS CLCL_ME_AGE,
    m.MEME_SEX,
    m.MEME_REL,
    CAST(NULL AS char(1))                AS MEPE_FI,
    CAST(s.GRGR_CK AS int)               AS GRGR_CK,
    g.GRGR_NAME,
    CAST(s.SGSG_CK AS int)               AS SGSG_CK,
    p.CSPD_CAT,
    p.CSPI_ID,
    pl.PLDS_DESC,
    p.PDPD_ID,
    CAST(pr.PDPD_TYPE AS char(1))        AS PDDS_PROD_TYPE,
    cl.CLCL_IDCD_ID                      AS IDCD_ID_PRIMARY,
    d.IDCD_DESC                          AS IDCD_DESC_PRIMARY,
    CAST(NULL AS varchar(10))            AS IDCD_ID_ADMIT,
    CAST(1 AS smallint)                  AS CLMD_DIAG_COUNT,
    l.CDML_SVC_CD                        AS IPCD_ID,
    ip.IPCD_DESC,
    CAST(NULL AS char(2))                AS IPCD_TYPE,
    CAST(l.CDML_UNITS AS smallint)       AS CDML_UNITS,
    CAST(l.CDML_UNITS AS smallint)       AS CDML_UNITS_ALLOW,
    CAST(NULL AS char(4))                AS PSCD_ID,
    CAST(NULL AS char(4))                AS RCRC_ID,
    CAST(NULL AS char(4))                AS LOBD_ID,
    cl.PRPR_ID,
    CAST(NULL AS varchar(60))            AS PRPR_NAME,
    pv.PRPR_NPI,
    CAST(pv.PRPR_SPECIALTY AS char(4))   AS PRPR_MCTR_SPEC,
    cl.NWNW_ID,
    CAST(l.CDML_CHG_AMT AS money)        AS CDML_CHG_AMT,
    CAST(NULL AS money)                  AS CDML_ALLOW,
    CAST(NULL AS money)                  AS CDML_PAID_AMT,
    CAST(NULL AS money)                  AS CDML_DED_AMT,
    CAST(NULL AS money)                  AS CDML_COPAY_AMT,
    CAST(NULL AS money)                  AS CDML_COINS_AMT,
    CAST(NULL AS money)                  AS CDML_DISALL_AMT,
    CAST(NULL AS money)                  AS CDML_DISC_AMT,
    CAST(NULL AS money)                  AS CDML_TOT_PA_LIAB,
    CAST(cl.CLCL_SVC_DT AS datetime)     AS CDML_FROM_DT,
    CAST(cl.CLCL_SVC_DT AS datetime)     AS CDML_TO_DT,
    cl.CLCL_CUR_STS                      AS CDML_CUR_STS,
    CAST(NULL AS char(18))               AS CLCL_EXT_AUTH_NO,
    CAST(NULL AS char(1))                AS CDML_PRE_AUTH_IND,
    CAST(NULL AS char(12))               AS CLCL_PRPR_ID_REF,
    CAST(NULL AS char(12))               AS CLCL_PRPR_ID_PCP,
    CAST(NULL AS char(1))                AS CLCL_PCP_IND
FROM dbo.CMC_CDML_CL_LINE     AS l
JOIN dbo.CMC_CLCL_CLAIM       AS cl ON cl.CLCL_ID = l.CLCL_ID
JOIN dbo.CMC_MEME_MEMBER      AS m  ON m.MEME_CK = cl.MEME_CK
JOIN dbo.CMC_SBSB_SUBSC       AS s  ON s.SBSB_CK = m.SBSB_CK
JOIN dbo.CMC_GRGR_GROUP       AS g  ON g.GRGR_CK = s.GRGR_CK
JOIN dbo.CMC_CSPI_CS_PLAN     AS p  ON p.CSPI_ID = s.CSPI_ID
JOIN dbo.CMC_PDPD_PRODUCT     AS pr ON pr.PDPD_ID = p.PDPD_ID
JOIN dbo.CMC_PLDS_PLAN_DESC   AS pl ON pl.CSPI_ID = p.CSPI_ID
JOIN dbo.CMC_IDCD_DIAG_CODE   AS d  ON d.IDCD_ID = cl.CLCL_IDCD_ID
JOIN dbo.CMC_PRPR_PROV        AS pv ON pv.PRPR_ID = cl.PRPR_ID
LEFT JOIN dbo.CMC_IPCD_PROC_CODE AS ip ON ip.IPCD_ID = l.CDML_SVC_CD;
GO
