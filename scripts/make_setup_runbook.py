"""Write docs/SDM-New-Setup-Runbook.docx: installing SDM for a new source
database and running the day-0 accelerator. Placeholders only; no real
server, database or login names."""

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Inches

OUT = Path(__file__).resolve().parent.parent / "docs" / "SDM-New-Setup-Runbook.docx"
ACCENT = RGBColor(0x1F, 0x4E, 0x79)

doc = Document()
for section in doc.sections:
    section.left_margin = section.right_margin = Inches(0.9)
    section.top_margin = section.bottom_margin = Inches(0.8)

base = doc.styles["Normal"]
base.font.name = "Calibri"
base.font.size = Pt(10.5)
for name, size in (("Heading 1", 15), ("Heading 2", 12.5), ("Heading 3", 11)):
    style = doc.styles[name]
    style.font.name = "Calibri"
    style.font.size = Pt(size)
    style.font.color.rgb = ACCENT


def shade(cell, hex_fill: str) -> None:
    props = cell._tc.get_or_add_tcPr()
    fill = OxmlElement("w:shd")
    fill.set(qn("w:val"), "clear")
    fill.set(qn("w:color"), "auto")
    fill.set(qn("w:fill"), hex_fill)
    props.append(fill)


def para(text: str = "", bold_lead: str | None = None, style: str | None = None):
    p = doc.add_paragraph(style=style)
    if bold_lead:
        p.add_run(bold_lead).bold = True
    p.add_run(text)
    p.paragraph_format.space_after = Pt(4)
    return p


def bullets(items, style="List Bullet"):
    for item in items:
        if isinstance(item, tuple):
            para(item[1], bold_lead=item[0], style=style)
        else:
            para(item, style=style)


def numbered(items):
    bullets(items, style="List Number")


def code(text: str) -> None:
    for line in text.strip("\n").splitlines():
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.left_indent = Inches(0.25)
        run = p.add_run(line if line else " ")
        run.font.name = "Consolas"
        run.font.size = Pt(9)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def table(header: list[str], rows: list[list[str]], widths: list[float] | None = None):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(header):
        cell = t.rows[0].cells[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(h)
        run.bold = True
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        shade(cell, "1F4E79")
    for r in rows:
        cells = t.add_row().cells
        for i, value in enumerate(r):
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            if value.startswith("`") and value.endswith("`"):
                run = p.add_run(value.strip("`"))
                run.font.name = "Consolas"
                run.font.size = Pt(9)
            else:
                p.add_run(value)
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Inches(w)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t


def note(text: str, lead: str = "Note: ") -> None:
    t = doc.add_table(rows=1, cols=1)
    t.style = "Table Grid"
    cell = t.rows[0].cells[0]
    shade(cell, "EAF1F8")
    cell.text = ""
    p = cell.paragraphs[0]
    p.add_run(lead).bold = True
    p.add_run(text)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


# --- Title -----------------------------------------------------------------------------

title = doc.add_paragraph()
title.alignment = WD_ALIGN_PARAGRAPH.LEFT
run = title.add_run("SDM: Setup and Run Guide")
run.bold = True
run.font.size = Pt(22)
run.font.color.rgb = ACCENT
sub = doc.add_paragraph()
sub.add_run("Configuring SDM and running the day-0 accelerator: a team trial first, then the "
            "customer").italic = True
meta = table(["Item", "Value"], [
    ["Applies to", "SDM builds from 2026-11-22 on (VERSION.txt in the SDM folder says which)"],
    ["Audience", "Part A: team members trying SDM on the sample bank. Part B: the customer's "
     "IT/DBA (one-time setup) and SDM user (day 0 and refresh)"],
    ["Ships as", "sdm-<build>-neutral.zip, plus the sdm-wheels parts for an offline install"],
    ["Source databases", "Microsoft SQL Server"],
    ["Document date", "2026-11-22"],
], [1.6, 5.0])

# --- 1 ----------------------------------------------------------------------------------

doc.add_heading("1. What SDM does, and what a person does", level=1)
para("SDM connects to a source database, reads its tables, keys and data profile, and proposes "
     "the denormalized (denorm) tables pattern analysis needs, the extract that fills them, and "
     "a privacy class for every column. It is not fully automatic: a person reviews and "
     "approves the proposal before anything is built or loaded. This is deliberate, because "
     "SDM reads production data and the approval decides which personal columns are withheld "
     "or replaced by surrogates.")
table(["Step", "Who", "Automatic?"], [
    ["Install and configure (.env, logins, store database)", "Customer IT / DBA", "No: one time"],
    ["Read the source and propose the denorm tables", "SDM", "Yes"],
    ["Review names, levels, features, privacy", "SDM user", "No: on screen"],
    ["Submit and approve", "SDM user (and second approver, if set)", "No: one click each"],
    ["Build the pack, create the tables, load the data", "SDM", "Yes: one click each"],
    ["Point SDM at the new pack and restart", "SDM user / IT", "No: one setting"],
    ["Refresh the data", "Scheduler (e.g., Tidal) or SDM user", "Yes, once scheduled"],
], [3.3, 2.0, 1.3])

doc.add_heading("How to use this guide", level=2)
table(["Part", "Who", "Follow"], [
    ["A. Team trial", "Team members, on a test SQL Server",
     "Sections 2 and 3, then Appendix A (instead of section 4), then sections 4.4, 4.5, 5 "
     "and 6. Send back what Appendix B asks."],
    ["B. Customer setup", "The customer's IT/DBA and SDM user",
     "Sections 2 to 8 in order, the checklist in section 11, and section 12 when SDM is "
     "already installed there."],
], [1.4, 2.0, 3.2])
note("Never point a trial at a production database. The trial builds its own made-up source "
     "(the sample bank) and its own store, both named SDMT_..., which can be dropped "
     "afterwards.", "Trial safety: ")

# --- 2 ----------------------------------------------------------------------------------

doc.add_heading("2. Prerequisites", level=1)
doc.add_heading("2.1 Machine", level=2)
bullets([
    ("Windows, 64-bit. ", "32-bit Python cannot run SDM."),
    ("Python 3.13 or 3.14, 64-bit ", "(python.org installer, with the py launcher)."),
    ("Microsoft ODBC Driver 18 for SQL Server.", ""),
    ("Network access ", "from this machine to the source SQL Server and to the SDM store SQL Server."),
])
doc.add_heading("2.2 Databases", level=2)
table(["Database", "Purpose", "SDM's access"], [
    ["Source database (one per pack)", "The customer's system of record that SDM reads",
     "Read only. Never written."],
    ["SDM store database (schema sdm)",
     "Everything SDM keeps: the denorm tables, patterns, approvals, logs",
     "Create tables in schema sdm, then read and write"],
], [2.0, 2.8, 1.8])
doc.add_heading("2.3 Logins and rights", level=2)
table(["Login", "Rights needed", "Notes"], [
    ["Source login", "Read (SELECT) on the tables to be analyzed; VIEW DEFINITION for metadata",
     "Read only is expected. SDM checks the login's rights and refuses a login that can write, "
     "unless the user explicitly allows it on the screen."],
    ["Store login", "db_ddladmin + db_datareader + db_datawriter on the store database, "
     "or owner of schema sdm",
     "Without create rights, a DBA runs the generated SQL once (section 7) and SDM needs only "
     "read and write."],
], [1.3, 2.6, 2.7])
note("On a customer machine (SDG_ENV=customer) the source must have its own login: SDM never "
     "uses the store's login to read a production source.", "Important: ")

# --- 3 ----------------------------------------------------------------------------------

doc.add_heading("3. Install", level=1)
numbered([
    "Unpack the SDM zip to a folder, for example C:\\SDM.",
    "Open a command prompt in that folder and run the installer:",
])
code(r"""
scripts\setup.cmd
""")
para("No internet, or a company package mirror that blocks pip: copy the six "
     "sdm-wheels-<python tag>.001 to .006 parts and their .sha256 file into the SDM folder, "
     "unchanged, and run:")
code(r"""
scripts\setup_offline.cmd
""")
para("Both create .venv and install the packages. Neither touches a database.")
para("Which build is installed: open VERSION.txt in the SDM folder (for example "
     "\"SDM build d52f1f0 (customer-neutral)\"). Quote it when reporting a problem.")
note("Do not run scripts\\upgrade.cmd, first-run.cmd, seed.cmd or bootstrap.cmd on a customer "
     "machine. They are for demo machines and refuse to run when SDG_ENV=customer.", "Warning: ")

# --- 4 ----------------------------------------------------------------------------------

doc.add_heading("4. Configure (.env)", level=1)
para("Copy scripts\\customer\\customer.env.example to .env in the SDM folder and fill in "
     "every <...> placeholder. "
     "Never commit .env or send it by email: it holds logins.")
doc.add_heading("4.1 Settings", level=2)
table(["Setting", "Meaning", "Example / value"], [
    ["`SDG_ENV`", "Marks the customer machine (demo scripts refuse to run)", "`customer`"],
    ["`SDG_PROTECTED_DATABASES`", "REQUIRED. The production databases SDM must never write to, "
     "separated by commas. SDM refuses to work on a customer machine without it, and writes "
     "these names into its SQL scripts' guards when it runs them.", "`<production databases>`"],
    ["`SDG_DB_DENORM`", "The SDM store database", "`<store database>`"],
    ["`SDG_DENORM_SCHEMA`", "Schema SDM uses in the store", "`sdm`"],
    ["`SDG_DENORM_SERVER` / `_PORT`", "Store SQL Server", "`<store server>` / `1433`"],
    ["`SDG_DENORM_USER` / `_PASSWORD`", "Store login (or `SDG_DENORM_TRUSTED=yes`)", "`<store login>`"],
    ["`SDG_DENORM_DRIVER` / `_TRUST_CERT`", "ODBC driver; trust the server certificate",
     "`ODBC Driver 18 for SQL Server` / `yes`"],
    ["`SDG_SOURCE_<NAME>_SERVER`", "The source's SQL Server. <NAME> is a short name you choose, "
     "e.g. CORE", "`<source server>`"],
    ["`SDG_SOURCE_<NAME>_DATABASE`", "The source database (required when it is on the protected "
     "list)", "`<source database>`"],
    ["`SDG_SOURCE_<NAME>_USER` / `_PASSWORD`", "The source's own login (or `_TRUSTED=yes`)",
     "`<source login>`"],
    ["`SDG_SOURCE_<NAME>_PORT` / `_DRIVER` / `_TRUST_CERT` / `_ENCRYPT`",
     "Optional connection details for the source", "as for the store"],
    ["`SDG_PACKS_DIR`", "Full path of a folder OUTSIDE the SDM folder where built packs are kept. "
     "Required before Build.", "`D:\\SDM-packs`"],
    ["`SDG_EXTRACT_KEY`", "Private secret (16+ characters) that surrogate IDs are made from. "
     "Keep it the same forever: changing it changes every surrogate.",
     "`<from the secrets vault>`"],
    ["`SDG_PACK`", "The pack SDM runs on. Set after the first build (section 6).", "`<pack id>`"],
    ["`SDG_EXTRACT_SOURCE`", "The source the scheduled extract reads. Set after the first build.",
     "`<NAME>`"],
    ["`SDG_NAMING_DROP_WORDS`", "Optional. Words taken out of group names when patterns are "
     "named (the company's own name, which every group name carries)", "`<company name>`"],
    ["`SDG_PROFILE_MAX_MINUTES`", "Time limit on profiling a source (0 = no limit)", "`60`"],
    ["`SDG_WHOLE_READ_MAX_ROWS`", "Larger tables are sampled, not read whole (0 = read all)",
     "`50000000`"],
    ["`SDG_DB_TARGET`", "Where Generation writes. Must never be a production database; name one "
     "that does not exist until a test database is agreed.", "`SDM_NO_GENERATION`"],
    ["`SDG_DATABASE_GENERATION`", "Allow Generation to load a database", "`no`"],
], [2.1, 2.9, 1.6])
doc.add_heading("4.2 Example .env (placeholders)", level=2)
code(r"""
SDG_ENV=customer

# Production databases SDM never writes to (required)
SDG_PROTECTED_DATABASES=<production database names>

# SDM store: everything SDM creates and reads
SDG_DB_DENORM=<store database>
SDG_DENORM_SCHEMA=sdm
SDG_DENORM_SERVER=<store server>
SDG_DENORM_PORT=1433
SDG_DENORM_TRUSTED=yes
SDG_DENORM_DRIVER=ODBC Driver 18 for SQL Server
SDG_DENORM_TRUST_CERT=yes

# Source CORE: read only
SDG_SOURCE_CORE_SERVER=<source server>
SDG_SOURCE_CORE_DATABASE=<source database>
SDG_SOURCE_CORE_USER=<read-only source login>
SDG_SOURCE_CORE_PASSWORD=<password>
SDG_SOURCE_CORE_TRUST_CERT=yes

# Accelerator
SDG_PACKS_DIR=D:\SDM-packs
SDG_EXTRACT_KEY=<16+ character secret from the vault>

# Generation off until a test database is agreed
SDG_DB_TARGET=SDM_NO_GENERATION
SDG_DATABASE_GENERATION=no

# Set after the first build (section 6):
# SDG_PACK=<pack id>
# SDG_EXTRACT_SOURCE=CORE
""")
doc.add_heading("4.3 Optional: second approver", level=2)
para("To require a named second person to approve a plan, open Layout approval in SDM's rail "
     "and enter the Second approver's name. Approvals then require that name to be typed. "
     "The same setting governs layout approvals.")

doc.add_heading("4.4 Create SDM's own tables (once)", level=2)
para("Before the first start, create the tables SDM keeps in the store (patterns, approvals, "
     "logs). In the SDM folder:")
code(r"""
scripts\customer\setup_tables.cmd
""")
para("It creates schema sdm if it is missing and adds only what is missing; running it again "
     "changes nothing. Without create rights, the DBA runs scripts\\customer\\setup_tables.sql "
     "and then sdg\\packs\\health\\sql\\setup_tables.sql instead (see section 7 about the "
     "placeholder in their first lines). It also creates the built-in health pack's empty "
     "tables, which a new source's pack does not use.")
doc.add_heading("4.5 Start SDM", level=2)
code(r"""
scripts\ui.cmd
""")
para("The browser opens SDM at http://127.0.0.1:8777. To run it in a window of its own: "
     "scripts\\sdm.cmd start (and scripts\\sdm.cmd stop, scripts\\sdm.cmd status). SDM answers "
     "only on this machine.")

# --- 5 ----------------------------------------------------------------------------------

doc.add_heading("5. Day 0: from source to loaded tables", level=1)
para("In SDM's left menu, open Source setup. Work through its tabs in order.")
table(["Tab", "What the user does", "Result"], [
    ["Sources", "Add the source by its <NAME> (settings come from .env; no password is typed "
     "on screen). Press Read. If SDM reports the login can write, confirm only if that is "
     "acceptable: it is still only read.",
     "SDM reads tables, keys and a data profile, then proposes a plan"],
    ["Landscape", "Review what was read: tables, row counts, keys, relationships",
     "Confidence the right database was read"],
    ["Hierarchy", "Confirm the record types (one primary, one child, e.g. customers and "
     "accounts), their names in the customer's words, and the hierarchy levels",
     "The grain and the drill-down levels"],
    ["Tables", "Confirm the tables each record type is built from", "The tables the extract reads"],
    ["Criteria", "Choose any criteria that limit which records are read (e.g. active only)",
     "The WHERE clauses of the extract"],
    ["Structure", "Review every denorm column and its privacy class: kept, withheld (empty), "
     "or surrogate (keyed replacement). Confirm keys flagged as possibly personal.",
     "The denorm table layout and privacy"],
    ["Approval", "Submit, then Approve (the second approver types their name if one is set)",
     "The plan is locked as approved"],
    ["Build", "Press Build", "The pack is written to SDG_PACKS_DIR\\<pack id>: config, "
     "store SQL, index SQL, extract SQL and a README"],
    ["Load", "Press Create tables (or give sql\\store_tables.sql to the DBA), then Load",
     "The denorm tables are created and filled; checks run; indexes made when the login "
     "may make them"],
], [1.1, 3.4, 2.1])
note("Load reads every table the extract names. Run it off-peak or against a replica. It uses "
     "at most two of the source's cores per statement and gives way on locks rather than "
     "blocking the source's own work.", "Load impact: ")
note("New rows are loaded beside the live tables and swapped in together, so analysis keeps "
     "reading the old rows until the load completes. A failed or stopped load leaves every "
     "table as it was.", "Safety: ")

# --- 6 ----------------------------------------------------------------------------------

doc.add_heading("6. Activate the new pack", level=1)
numbered([
    "In .env, set SDG_PACK=<pack id> and SDG_EXTRACT_SOURCE=<NAME> (both are shown on the "
    "Build tab and in the pack's README.txt).",
    "Restart SDM (close it and run scripts\\ui.cmd again).",
    "Open Patterns and find patterns on the new record types.",
])

# --- 7 ----------------------------------------------------------------------------------

doc.add_heading("7. Without create-table rights (DBA path)", level=1)
para("When the store login cannot create tables, give the DBA these files from "
     "SDG_PACKS_DIR\\<pack id>:")
table(["File", "When", "What it does"], [
    ["`sql\\store_tables.sql`", "Before the first load", "Creates the denorm tables. Creates "
     "only what is missing; refuses to run in the source or a protected database."],
    ["`sql\\store_indexes.sql`", "After the first load", "Creates the indexes pattern analysis "
     "reads by. Makes only the missing ones."],
], [2.0, 1.5, 3.1])
para("SDM's own tables are created by scripts\\customer\\setup_tables.sql (run once by the DBA "
     "if SDM cannot create them).")
note("SDM's static SQL scripts name no production database. Their first lines hold the "
     "placeholder {{PROTECTED_DATABASES}}: when SDM runs a script it writes the names from "
     "SDG_PROTECTED_DATABASES there. A DBA running one by hand replaces every "
     "{{PROTECTED_DATABASES}} (Find and Replace, all) with the production database names, "
     "separated by commas; left in anywhere, the script stops and changes nothing. Scripts a "
     "build writes (sql\\store_tables.sql, sql\\store_indexes.sql) already name the source "
     "and the protected databases.", "Run by hand: ")

# --- 8 ----------------------------------------------------------------------------------

doc.add_heading("8. Refresh and change", level=1)
doc.add_heading("8.1 Scheduled refresh", level=2)
para("Schedule this from the SDM folder (e.g., as a Tidal job), off-peak:")
code(r"""
scripts\customer\extract.cmd
""")
para("It runs the same approved load as the Load tab: the build is checked against the store, "
     "the tables are loaded beside the live ones and swapped in. It exits with an error code "
     "when a table fails. The Load tab can refresh on demand too.")
doc.add_heading("8.2 When the source changes", level=2)
bullets([
    ("Source columns renamed, removed or retyped: ", "the load refuses (drift) and lists them. "
     "Read the source again, review, approve and build again."),
    ("Plan changed after approval: ", "approve and build again; the screen says so."),
    ("After a new build is loaded: ", "patterns found on the earlier build are archived "
     "read-only. They stay readable but start no new work."),
    ("New column in a rebuild: ", "added to the existing table (empty until the load fills it). "
     "A retyped or unused column is listed for the DBA to alter or drop."),
])

# --- 9 ----------------------------------------------------------------------------------

doc.add_heading("9. Troubleshooting", level=1)
table(["Message (summary)", "Cause", "What to do"], [
    ["The login can write", "The source login has write rights", "Use a read-only login, or "
     "confirm on screen to go on (SDM still only reads). For the scheduled refresh: "
     "extract.cmd --allow-write-login."],
    ["SDG_PROTECTED_DATABASES is not set", "A customer machine names no production database",
     "Set SDG_PROTECTED_DATABASES in .env to the production database names, then run again."],
    ["This script names no production database yet", "A SQL script run by hand with "
     "{{PROTECTED_DATABASES}} left in", "Replace every {{PROTECTED_DATABASES}} with the "
     "production database names, comma separated, and run it again."],
    ["Source's own login is required", "SDG_ENV=customer and no SDG_SOURCE_<NAME>_USER or "
     "_TRUSTED", "Add the source's login to .env."],
    ["Protected database", "The source is on the protected list", "Name it explicitly in "
     "SDG_SOURCE_<NAME>_DATABASE."],
    ["Set SDG_PACKS_DIR", "No packs folder, or not a full path", "Set a full path outside the "
     "SDM folder."],
    ["Not approved / plan changed", "Build or Load before approval, or after a change",
     "Submit, approve and build again."],
    ["The source changed since the plan was built", "Drift", "Read again, review, approve, "
     "build."],
    ["Another extract into this store", "A load or extract is already running", "Wait for it "
     "to finish."],
    ["A reader held a table", "A long analysis held a table through four waits", "Load again "
     "when the analysis has finished; every table kept its rows."],
    ["SDM was rebuilt / load this build", "The pack folder or store holds another build",
     "Load this build, or restart SDM with the build that was loaded."],
], [1.9, 2.2, 2.5])

# --- 10 ---------------------------------------------------------------------------------

doc.add_heading("10. Current limits", level=1)
bullets([
    "SQL Server sources only; one source database per pack.",
    "One primary and one child record type per pack.",
    "A generated pack offers pattern analysis. Writing files from its patterns is available for "
    "the built-in health and banking packs only.",
    "Joins, criteria and the record table are chosen from what SDM proposes; stating a join by "
    "hand or a custom criterion is not available yet.",
])

# --- 11 ---------------------------------------------------------------------------------

doc.add_heading("11. Setup checklist", level=1)
table(["#", "Item", "Done"], [
    ["1", "Python 64-bit and ODBC Driver 18 installed", "☐"],
    ["2", "scripts\\setup.cmd (or setup_offline.cmd) completed", "☐"],
    ["3", "Store database and schema sdm exist; store login has the rights in 2.3", "☐"],
    ["4", "Source login is read-only and can reach the source", "☐"],
    ["5", ".env filled in: SDG_ENV, SDG_PROTECTED_DATABASES, SDG_DENORM_*, "
     "SDG_SOURCE_<NAME>_*, SDG_PACKS_DIR, SDG_EXTRACT_KEY", "☐"],
    ["6", "SDG_EXTRACT_KEY stored in the secrets vault", "☐"],
    ["7", "Second approver set (if required)", "☐"],
    ["8", "Source read, plan reviewed, submitted and approved", "☐"],
    ["9", "Pack built; tables created; first load succeeded with checks passing", "☐"],
    ["10", "Indexes created (by SDM or the DBA)", "☐"],
    ["11", "SDG_PACK and SDG_EXTRACT_SOURCE set; SDM restarted; patterns found", "☐"],
    ["12", "Refresh scheduled (extract.cmd), off-peak", "☐"],
], [0.4, 5.4, 0.8])

# --- 12 ---------------------------------------------------------------------------------

doc.add_heading("12. Upgrading an installation that runs SDM already", level=1)
numbered([
    "Stop SDM (close it, or scripts\\sdm.cmd stop).",
    "Keep .env and the work folder; unpack the new zip over the SDM folder, or into a new folder "
    "and copy .env and work across.",
    "Add to .env: SDG_PROTECTED_DATABASES=<production database names> (required since the "
    "2026-11-22 build: SDM refuses on a customer machine without it). Where pattern names "
    "used to leave out the company's own name, add SDG_NAMING_DROP_WORDS=<company name>.",
    "Run scripts\\customer\\setup_tables.cmd (adds only what is missing), then start SDM.",
])
note("Do not run scripts\\upgrade.cmd on a customer machine: it is for demo machines.",
     "Warning: ")

# --- Appendix A ---------------------------------------------------------------------------

doc.add_heading("Appendix A. Team trial on the sample bank", level=1)
para("A practice run of the whole day-0 journey on a made-up bank of about 2,000 customers, "
     "on a test SQL Server you may create databases on. Nothing here touches a customer's "
     "data.")
doc.add_heading("A.1 Settings for a trial machine", level=2)
para("Copy .env.example to .env (not the customer template) and set at least:")
code(r"""
# A trial machine (demo rules apply; the sample bank may be built here)
SDG_ENV=local

# The test SQL Server: the sample bank is built and read here
SDG_MSSQL_SERVER=<test server>
SDG_MSSQL_PORT=1433
SDG_MSSQL_TRUSTED=yes
SDG_MSSQL_DRIVER=ODBC Driver 18 for SQL Server
SDG_MSSQL_TRUST_CERT=yes

# The trial's own store, on the same server
SDG_DB_DENORM=SDMT_TRIAL_STORE
SDG_DENORM_SCHEMA=sdm

# Databases on that server SDM must never write to (any real ones there)
SDG_PROTECTED_DATABASES=<names, or leave the line out if there are none>

# Built packs: a folder outside SDM; a made-up key for the trial only
SDG_PACKS_DIR=<full path, e.g. C:\SDM-trial-packs>
SDG_EXTRACT_KEY=<any 16+ characters, for the trial only>
""")
para("On a trial machine a source needs no lines of its own: it is read through the "
     "SDG_MSSQL_* server, and its database is its name.")
doc.add_heading("A.2 Make the store and the sample source", level=2)
numbered([
    "Create an empty database SDMT_TRIAL_STORE on the test server (SSMS: New Database), then "
    "run scripts\\customer\\setup_tables.cmd (section 4.4).",
    "Build the sample bank as the source (it creates the database SDMT_BANK_SRC; it refuses "
    "any name not starting SDMT_ and any protected database):",
])
code(r"""
.venv\Scripts\python.exe -m seed.bank SDMT_BANK_SRC
""")
doc.add_heading("A.3 Run day 0", level=2)
numbered([
    "Start SDM (section 4.5) and open Source setup.",
    "Sources: add SDMT_BANK_SRC and press Read. The trial login can usually write: confirm to "
    "go on (SDM still only reads).",
    "Expect the proposal: customers (primary, dbo.CUSTOMER) and accounts (child, dbo.ACCOUNT, "
    "with transactions as lines), levels such as branch and product.",
    "Work through Landscape, Hierarchy, Tables, Criteria and Structure. On Structure, check the "
    "privacy class of names, emails and keys.",
    "Approval: Submit, then Approve. Build. Load: Create tables, then Load; every check should "
    "pass.",
    "Set SDG_PACK to the pack id the Build tab shows, restart SDM, and find patterns on "
    "customers and on accounts (Patterns).",
    "Optional: change a choice on Structure, approve and build again, and load: the earlier "
    "patterns show as Archived, read-only.",
])
doc.add_heading("A.4 Clean up", level=2)
para("Stop SDM, then drop the trial's databases by their exact names (SDMT_BANK_SRC, "
     "SDMT_TRIAL_STORE) and delete the trial packs folder. Never drop by a pattern.")

# --- Appendix B ---------------------------------------------------------------------------

doc.add_heading("Appendix B. What to send back after the trial", level=1)
table(["#", "Item"], [
    ["1", "The build (VERSION.txt) and the machine: Windows version, Python version"],
    ["2", "Each step of A.3: worked / did not, with the exact message for anything refused"],
    ["3", "Anything in this guide that was unclear, missing or wrong"],
    ["4", "Screens or wording that confused you"],
    ["5", "How long Read, Build and Load took"],
], [0.4, 6.2])
note("Do not send .env, logins, keys or screenshots that show them.", "Privacy: ")

OUT.parent.mkdir(parents=True, exist_ok=True)
doc.save(OUT)
print(OUT)
