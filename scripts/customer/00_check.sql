-- 00_check.sql: read-only. Nothing is created, changed or removed.
--
-- For the customer's SDM platform database. Written for schema sdm; if the
-- tool is configured for another schema (SDG_DENORM_SCHEMA), replace "sdm."
-- and 'sdm' throughout before running.
--
-- Run first and keep the output: it says which tables exist, whether each id
-- is an IDENTITY, which columns have defaults, and which columns the tool's
-- upgrade (01_sdm_upgrade.sql) would add. The tool works either way; this is
-- so nobody has to guess what the database is like.

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


-- Every table in the schema, its rows, and whether its id is an IDENTITY.
SELECT t.name AS table_name,
       SUM(p.rows) AS row_count,
       MAX(CASE WHEN c.name = 'id' THEN CAST(c.is_identity AS int) END) AS id_is_identity,
       MAX(CASE WHEN k.type = 'PK' THEN 1 ELSE 0 END) AS has_primary_key
FROM sys.tables t
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
LEFT JOIN sys.columns c ON c.object_id = t.object_id AND c.name = 'id'
LEFT JOIN sys.key_constraints k ON k.parent_object_id = t.object_id AND k.type = 'PK'
WHERE SCHEMA_NAME(t.schema_id) = 'sdm'
GROUP BY t.name
ORDER BY t.name;

-- Columns with a default, and the defaults.
SELECT t.name AS table_name, c.name AS column_name, d.definition AS default_value
FROM sys.default_constraints d
JOIN sys.columns c ON c.object_id = d.parent_object_id AND c.column_id = d.parent_column_id
JOIN sys.tables t ON t.object_id = d.parent_object_id
WHERE SCHEMA_NAME(t.schema_id) = 'sdm'
ORDER BY t.name, c.name;

-- Indexes on the denorm tables.
SELECT t.name AS table_name, i.name AS index_name, i.type_desc,
       STRING_AGG(c.name, ', ') WITHIN GROUP (ORDER BY ic.key_ordinal) AS key_columns
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE SCHEMA_NAME(t.schema_id) = 'sdm' AND t.name IN ('member_denorm', 'claims_denorm')
  AND ic.is_included_column = 0
GROUP BY t.name, i.name, i.type_desc
ORDER BY t.name, i.name;
