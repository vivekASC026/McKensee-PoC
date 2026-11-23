-- Indexes on the denorm tables, for the reads pattern analysis makes.
--
-- Optional, and separate from the tables: an index adds no column and changes
-- no row, but it is still a change to tables the customer owns, so they decide.
-- Each is created only if absent. Written for schema sdm; to run on another,
-- replace "sdm." throughout.
--
-- What the tool reads, and which index serves it:
--   * a scope -- group, plan category, plan, product -- loaded for analysis:
--       WHERE GRGR_CK / CSPD_CAT / CSPI_ID / PDPD_ID IN (...)
--   * one row per member, the latest enrollment:
--       ROW_NUMBER() OVER (PARTITION BY MEME_CK ORDER BY MEME_ORIG_EFF_DT DESC)
--   * a pattern's members read back by key: WHERE MEME_CK IN (...)
--   * claim lines in claim order: ORDER BY CLCL_ID, CDML_SEQ_NO, and a claims
--     scope by group, plan, product and claim type.
-- Aggregate reads -- counts by feature, combinations of codes -- scan once and
-- are not helped by narrow indexes; they are cached by the tool instead.

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

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_member_denorm_meme_latest'
               AND object_id = OBJECT_ID('sdm.member_denorm'))
CREATE INDEX ix_member_denorm_meme_latest
    ON sdm.member_denorm (MEME_CK, MEME_ORIG_EFF_DT DESC);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_member_denorm_scope'
               AND object_id = OBJECT_ID('sdm.member_denorm'))
CREATE INDEX ix_member_denorm_scope
    ON sdm.member_denorm (GRGR_CK, CSPD_CAT, CSPI_ID, PDPD_ID) INCLUDE (MEME_CK);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_member_denorm_plan'
               AND object_id = OBJECT_ID('sdm.member_denorm'))
CREATE INDEX ix_member_denorm_plan
    ON sdm.member_denorm (CSPI_ID) INCLUDE (MEME_CK, CSPD_CAT, PDPD_ID);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_claims_denorm_line'
               AND object_id = OBJECT_ID('sdm.claims_denorm'))
CREATE INDEX ix_claims_denorm_line
    ON sdm.claims_denorm (CLCL_ID, CDML_SEQ_NO);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_claims_denorm_scope'
               AND object_id = OBJECT_ID('sdm.claims_denorm'))
CREATE INDEX ix_claims_denorm_scope
    ON sdm.claims_denorm (GRGR_CK, CSPI_ID, PDPD_ID, CLCL_CL_TYPE);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_claims_denorm_member'
               AND object_id = OBJECT_ID('sdm.claims_denorm'))
CREATE INDEX ix_claims_denorm_member
    ON sdm.claims_denorm (MEME_CK);
GO
