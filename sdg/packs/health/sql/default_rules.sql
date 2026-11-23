-- default_rules.sql (health pack): the generation rules a new health store starts with.
--
-- Names for the member and subscriber name columns, a street, a ZIP and a
-- phone, always synthetic. Run after SDM's own tables (setup_tables.cmd runs
-- it); each rule only when absent, so running it again changes nothing.
-- Written for schema sdm.

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

-- Default generation rules (names and addresses — always synthetic)
-- Each only when absent, so a rebuild neither duplicates nor overwrites them.
IF NOT EXISTS (SELECT 1 FROM sdm.generation_rules WHERE field_name = N'MEME_FIRST_NAME')
INSERT INTO sdm.generation_rules (field_name, field_label, field_category, gen_method, data_type, length, lookup_source, updated_by)
VALUES (N'MEME_FIRST_NAME', N'Member First Name', N'lookup', N'lookup', N'text', 0, N'first_names', N'system');

IF NOT EXISTS (SELECT 1 FROM sdm.generation_rules WHERE field_name = N'MEME_LAST_NAME')
INSERT INTO sdm.generation_rules (field_name, field_label, field_category, gen_method, data_type, length, lookup_source, updated_by)
VALUES (N'MEME_LAST_NAME', N'Member Last Name', N'lookup', N'lookup', N'text', 0, N'last_names', N'system');

IF NOT EXISTS (SELECT 1 FROM sdm.generation_rules WHERE field_name = N'SBSB_FIRST_NAME')
INSERT INTO sdm.generation_rules (field_name, field_label, field_category, gen_method, data_type, length, lookup_source, updated_by)
VALUES (N'SBSB_FIRST_NAME', N'Subscriber First Name', N'lookup', N'lookup', N'text', 0, N'first_names', N'system');

IF NOT EXISTS (SELECT 1 FROM sdm.generation_rules WHERE field_name = N'SBSB_LAST_NAME')
INSERT INTO sdm.generation_rules (field_name, field_label, field_category, gen_method, data_type, length, lookup_source, updated_by)
VALUES (N'SBSB_LAST_NAME', N'Subscriber Last Name', N'lookup', N'lookup', N'text', 0, N'last_names', N'system');

IF NOT EXISTS (SELECT 1 FROM sdm.generation_rules WHERE field_name = N'STREET_1')
INSERT INTO sdm.generation_rules (field_name, field_label, field_category, gen_method, data_type, length, lookup_source, updated_by)
VALUES (N'STREET_1', N'Street Address 1', N'lookup', N'lookup', N'text', 0, N'street_names', N'system');

IF NOT EXISTS (SELECT 1 FROM sdm.generation_rules WHERE field_name = N'ZIP_CODE')
INSERT INTO sdm.generation_rules (field_name, field_label, field_category, gen_method, data_type, length, lookup_source, updated_by)
VALUES (N'ZIP_CODE', N'ZIP Code', N'lookup', N'lookup', N'text', 5, N'zip_city_state', N'system');

IF NOT EXISTS (SELECT 1 FROM sdm.generation_rules WHERE field_name = N'PHONE')
INSERT INTO sdm.generation_rules (field_name, field_label, field_category, element, gen_method, data_type, length, format_pattern, updated_by)
VALUES (N'PHONE', N'Phone Number', N'contact', N'PHONE', N'pattern', N'text', 0, N'\([2-9]\d{2}\) [2-9]\d{2}-\d{4}', N'system');
