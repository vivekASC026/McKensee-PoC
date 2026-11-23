-- truncate_extract_tables.sql: empty the 23 tables extract.cmd loads.
--
-- Run it IN THE SDM PLATFORM DATABASE (pick it in SSMS, or sqlcmd -d <db>).
-- It empties only the extract's tables in schema sdm. The tool's own tables
-- (patterns, generation_rules, layouts, discovery_features, extract_log, the
-- reference lists, ...) are not touched.
--
-- TRUNCATE needs ALTER on the table. Without that right, the table is emptied
-- with DELETE instead (slower on big tables, same result). A table that does
-- not exist is skipped. Each line printed says what happened.

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

DECLARE @tables TABLE (n int IDENTITY(1, 1), name sysname);
INSERT INTO @tables (name) VALUES
    ('member_denorm'), ('claims_denorm'),
    ('member_elig'), ('subscriber_denorm'), ('member_address'), ('member_pcp'), ('member_cob'),
    ('plan_denorm'), ('plan_network'), ('plan_benefit_denorm'), ('plan_accumulator'),
    ('service_code_map'), ('place_of_service'),
    ('claims_header_ext_denorm'), ('claims_detail_denorm'), ('claims_diag'),
    ('claims_icd_proc'), ('claims_ub_code'), ('claims_auth'),
    ('provider_relation'), ('provider_denorm'), ('provider_address'), ('provider_network');

DECLARE @i int = 1, @max int = (SELECT MAX(n) FROM @tables), @name sysname, @full nvarchar(300);
WHILE @i <= @max
BEGIN
    SELECT @name = name FROM @tables WHERE n = @i;
    SET @full = N'[sdm].' + QUOTENAME(@name);
    IF OBJECT_ID(@full, 'U') IS NULL
        PRINT @name + N': not found, skipped';
    ELSE
    BEGIN
        BEGIN TRY
            EXEC (N'TRUNCATE TABLE ' + @full);
            PRINT @name + N': truncated';
        END TRY
        BEGIN CATCH
            EXEC (N'DELETE FROM ' + @full);
            PRINT @name + N': emptied with DELETE (' + ERROR_MESSAGE() + N')';
        END CATCH
    END
    SET @i += 1;
END
PRINT 'Done. Next: scripts\customer\extract.cmd';
