# Customer deployment: SDM on the SDM platform database, loaded from FACETS

A new deployment. The `sdm` schema of the SDM platform database starts empty
(its old SDM tables dropped); the tool creates every table it needs there and
fills the denorm tables from FACETS.

Two databases, on two servers:

- **FACETS** is only ever read.
- **The SDM platform database** gets everything, in schema `sdm`: the denorm
  tables pattern analysis reads, the tool's own tables, and the reference
  lists.

Three commands, run from the SDM folder in a command prompt:

```
scripts\customer\setup_tables.cmd     (1) create every SDM table
scripts\customer\extract.cmd          (2) load FACETS into the denorm tables
scripts\ui.cmd                        (3) start the tool, then find patterns
```

**Do not run `scripts\upgrade.cmd`, `scripts\first-run.cmd`, `scripts\seed.cmd`
or `scripts\bootstrap.cmd` here.** They are for the demo laptops, and they
refuse to run on a `.env` made from `customer.env.example`.

What was done at the customer on 2026-10-08/09 (machines, Python, rights,
extract scope, fixes), and how an update reaches it, is in `ENVIRONMENTS.md`.

## Files you will use

All paths are inside the SDM folder (where the zip was unpacked).

| What | File |
|---|---|
| This runbook | `scripts\customer\README.md` |
| Install (internet) | `scripts\setup.cmd` |
| Install (offline, from the `sdm-wheels-cp3xx.001`... parts) | `scripts\setup_offline.cmd`, `scripts\unpack_wheels.py`, `requirements-runtime.txt`, `requirements-optional.txt` |
| Settings | `.env`, copied from `scripts\customer\customer.env.example` |
| (1) Create every SDM table | `scripts\customer\setup_tables.cmd` |
| (2) Load FACETS into the denorm | `scripts\customer\extract.cmd` (reads the source `SDG_EXTRACT_SOURCE` names in `.env`; when it is not set, the pack's default, FACETS for health) |
| The SELECT for each denorm table, against FACETS | `sdg\packs\health\extract\FACETS.sql` (read by `extract.cmd`; edit a SELECT here to change what is loaded) |
| (3) Start the tool | `scripts\ui.cmd` |
| Every FACETS source table, join and effective date, explained | `docs\facets-837-sources.md` |

## 0. Before you start

- Windows, with **Python 3.13 or 3.14**, 64-bit (python.org, "py launcher" ticked) and
  **ODBC Driver 18 for SQL Server**. At the customer: 3.14 free-threaded on the
  user's VDI (parts `cp314t`), 3.14.3 on the teammate's laptop (parts `cp314`).
- A login on the **SDM platform database** that can create tables in schema
  `sdm` and truncate and insert into them (db_ddladmin + db_datareader +
  db_datawriter, or owner of the schema). Without create rights (the case at
  the customer), a DBA or a Tidal SQL job runs `scripts\customer\setup_tables.sql`
  and then `sdg\packs\health\sql\setup_tables.sql` once instead of step 3
  (run by hand, each script's first lines hold `{{PROTECTED_DATABASES}}`:
  replace every one with the production database names, separated by commas,
  or the script stops; `setup_tables.cmd` fills them in from `.env`);
  everything after that needs only read and write.
- A login on **FACETS** that can read its `dbo.CMC_*` tables.
- The `sdm` schema emptied of the old SDM tables. If the schema itself was
  dropped, step 1 creates it.

## 1. Install

Unpack the zip to a folder, e.g. `C:\SDM`, then in a command prompt there:

```
scripts\setup.cmd
```

This creates `.venv` and installs the Python packages (needs internet for
pip). It does no database work. It leaves a `.env` to replace next: go on to
step 2 (never `seed.cmd`, which is for demo machines).

**No internet, or pip says "Looking in indexes: ..." and "could not find a
version that satisfies the requirement setuptools"** (a company package
mirror): install offline instead. The packages come as six parts of 15 MB
or less, one set per Python version: `sdm-wheels-cp314.001` to `.006` for
Python 3.14 (`cp313` for 3.13, `cp312` for 3.12; `py --version` tells). Copy
the six parts for the machine's Python, and the `sdm-wheels-<tag>.sha256` file
sent with them, into the SDM folder, as they are (do not unzip or rename them),
and run:

```
scripts\setup_offline.cmd
```

The parts are checked against the `.sha256` file before anything is unpacked.
Without it, give the SHA-256 from the message the parts came with:
`scripts\setup_offline.cmd --sha256 <hash>`.

On an ARM64 machine the parts are `sdm-wheels-cp314-arm64.001` to `.005`;
for the free-threaded Python (`py --version` or the line below says
"free-threading build") `sdm-wheels-cp314t.001` to `.006`.
32-bit Python cannot run the tool (pandas has no 32-bit build); install the
64-bit one. To see which you have:
`.venv\Scripts\python.exe -c "import sys,sysconfig;print(sys.version, sysconfig.get_platform())"`
(`win-amd64`, `win-arm64` or `win32`).

It joins the parts, checks them, unpacks them into `wheels`, and installs
every package from there, never contacting pip's index or the mirror, and
leaves a `.env` copied from `customer.env.example`. A
`.venv` left by a failed `setup.cmd` is reused. If a part is missing or
damaged it says which; copy it again and rerun.

## 2. Settings: `.env` in the SDM folder

Copy `scripts\customer\customer.env.example` to `.env` and fill in the
`<...>` parts; it already holds `SDG_ENV=customer`. On a `.env` made earlier, add the line `SDG_ENV=customer`.
What each line means: replace the database lines with these, filled in. Every `SDG_DB_` line must
name a database that exists here: the `FACETS_QA` / `MDM_REF` copied from
`.env.example` are demo names. `SDG_DB_TARGET` is where Generation writes:
never the production FACETS database. Until a test database is agreed, leave
it naming one that does not exist, so a run fails instead of writing.

```
# FACETS: read by the extract
SDG_MSSQL_SERVER=<FACETS server>
SDG_MSSQL_PORT=1433
SDG_MSSQL_USER=<FACETS login>               # or SDG_MSSQL_TRUSTED=yes for Windows login
SDG_MSSQL_PASSWORD=<password>
SDG_MSSQL_DRIVER=ODBC Driver 18 for SQL Server
SDG_MSSQL_TRUST_CERT=yes
SDG_DB_SOURCE=<FACETS database>
SDG_DB_REF=<FACETS database>                # only checked on Connect; must exist
SDG_DB_TARGET=<database generated rows are written into>

# Production databases SDM never writes to, separated by commas. Required on
# a customer machine: SDM refuses without it, and fills them into its SQL
# scripts' guards. Name the production FACETS database here.
SDG_PROTECTED_DATABASES=<production database names>

# Optional: words taken out of group names when patterns are named (the
# company's own name, which every group name carries), separated by commas.
# SDG_NAMING_DROP_WORDS=<company name>

# SDM platform database: everything SDM creates and reads
SDG_DB_DENORM=<SDM platform database>
SDG_DENORM_SCHEMA=sdm
SDG_DENORM_SERVER=<SDM platform server>
SDG_DENORM_PORT=1433
SDG_DENORM_USER=<SDM login>                 # or SDG_DENORM_TRUSTED=yes
SDG_DENORM_PASSWORD=<password>
SDG_DENORM_DRIVER=ODBC Driver 18 for SQL Server
SDG_DENORM_TRUST_CERT=yes
```

## 3. Create every SDM table

```
scripts\customer\setup_tables.cmd
```

Creates, in schema `sdm` of the SDM platform database:

- **Denorm tables:** `member_denorm` and `claims_denorm` (the same columns
  and types as before), and beside them the provider, plan and claim-detail
  tables: `provider_denorm`, `provider_address`, `provider_network`,
  `provider_relation`, `member_elig`, `subscriber_denorm`, `member_address`,
  `member_pcp`, `member_cob`, `plan_denorm`, `plan_network`,
  `plan_benefit_denorm`, `plan_accumulator`, `service_code_map`,
  `place_of_service`, `claims_header_ext_denorm`, `claims_detail_denorm`,
  `claims_diag`, `claims_icd_proc`, `claims_ub_code`, `claims_auth`.
- **Indexes** on the denorm tables for the reads pattern analysis makes
  (`--no-indexes` to leave them out).
- **The tool's own tables:** patterns, pattern rules and tags, generation
  rules, layouts, baselines, the Configure screen's choices.
- **Reference lists:** first and last names, street names, ZIP/city/state
  (a sample of 102 ZIPs; load your own into `sdm.lkp_zip_city_state` for
  full coverage), and the default generation rules.

It ends with every `sdm` table and its row count. Add-only: running it again
creates only what is missing and never empties a table. It uses only the SDM
platform connection; FACETS is not touched and no database is created.

## 4. Load FACETS into the denorm

```
scripts\customer\extract.cmd
```

Reads FACETS on its server and writes the SDM platform database, one table
at a time: `member_denorm`, `claims_denorm` and the 21 tables above. Each
table is emptied (TRUNCATE) and filled in one transaction, so a table that
fails keeps what it had; the run goes on to the next and lists the failures
at the end. Progress prints every 100,000 rows.

- **Which source:** FACETS (the health pack's `extract.default_source`),
  unless `.env` names another in `SDG_EXTRACT_SOURCE` (a pack built for
  another source). It runs `sdg extract --configured`.
- **Refresh later:** run it again; it replaces, never adds.
- **One table only:** `scripts\customer\extract.cmd --table member_denorm`
  (repeatable).
- **What is loaded:** `member_denorm` has one row per member and product
  category (the eligibility span in force today, else the latest);
  `claims_denorm` one row per claim line. The SELECTs are plain SQL in
  `sdg\packs\health\extract\FACETS.sql`; every table and column they use is
  in the FACETS data dictionary.

**Check against their FACETS first** (also at the top of `FACETS.sql`):

- `CLMD_TYPE` '01' is the primary diagnosis and 'AD' the admitting one.
- `MEPE_ELIG_IND = 'Y'` means eligible.
- The first procedure modifier is positions 6-7 of `IPCD_ID`.
- `PDBC_TYPE = 'SEDF'` is the plan's service-definition component.
- `member_denorm.CSPI_HIOS_ID_NVL` stays empty: the data dictionary has no
  source column for it.

If the first full load is slow, the four provider blocks in `FACETS.sql` each
include the full rosters of the plans' networks; removing that branch is the
first thing to cut.

## 5. Start and find patterns

```
scripts\ui.cmd
```

or `scripts\sdm.cmd start` to run it in a minimized window of its own;
`scripts\sdm.cmd stop` stops it and `scripts\sdm.cmd status` says whether it
is running (a second argument names another port).

The browser opens at http://127.0.0.1:8777, on **Patterns**: database loads
are off at a customer, so the Connect, Discover and Map steps on the left are
not needed. Then:

1. **Settings > Pattern Settings** (left menu): tick the features patterns are
   found on -- age, gender, plan, provider specialty, network, in or out of
   network, PCP... Save, for members and for claims.
2. **Patterns**, tab **Targeted Analysis**: pick a group (or none), then
   **Find Patterns**. **Save pattern** on the ones to keep.
3. **Generation** (left menu): "Written for" is the latest saved pattern;
   choose a file type's tab and **Write the file**. Every file is kept as a
   generation request, under the **Requests** tab, from where it can be
   written again.
4. **Settings > Generation Rules** (left menu): each business element and how
   it is generated.

Every extract is stamped (`sdm.extract_log`); analyses cached before a
refresh are not reused after it.

**Connect does not read FACETS.** On production it says so and offers
**Go to Patterns** instead of a read: pattern analysis reads only the denorm
tables the extract filled.

### Files from the command line

The same files, without the screens -- for a scheduled job, or a VDI where a
browser is a nuisance. Each is kept as a request, exactly as **Write the
file** keeps it, in `work\files` unless `--out` names a folder:

```
.venv\Scripts\python.exe -m sdg.cli.main write-file --pattern 12 --format edi:834 --members 500
.venv\Scripts\python.exe -m sdg.cli.main write-file --pattern 31 --format edi:837P --claims
.venv\Scripts\python.exe -m sdg.cli.main requests --pattern 12
.venv\Scripts\python.exe -m sdg.cli.main write-again 88
```

`requests` lists each request's files with their ids; `write-again` makes a
kept file again from its record alone and says whether it is the same file.

## Without create-table rights: Tidal jobs, and the screens from a VDI

Only `setup_tables.cmd` needs to create tables. After it has run once:

- **The screens** (`ui.cmd`) need only **db_datareader + db_datawriter** on
  the SDM platform database. They create and alter nothing, and they do not
  read FACETS. (Checked with such a login: every screen's calls, patterns
  found and kept, features saved, rules and layouts saved.)
- **The extract** needs read on FACETS and write on the SDM platform
  database. With ALTER on the `sdm` tables it empties them with TRUNCATE;
  without it, with DELETE (slower on large tables, same result).

So, on a machine the Tidal agent runs on (Python and the SDM folder installed
there as in step 1, `.env` with the job account: `..._TRUSTED=yes` for its
Windows login):

| Tidal job | Command | When |
|---|---|---|
| SDM setup | `cmd /c C:\SDM\scripts\customer\setup_tables.cmd` | once (and after an SDM update) |
| SDM extract | `cmd /c C:\SDM\scripts\customer\extract.cmd` | after setup, then on the refresh schedule |

The extract is extract and load in one: it reads FACETS and writes the
denorm tables. Both commands end with exit code 0 when everything worked
and 1 when anything failed, for Tidal to report; their output is the job
log. Nothing waits for a key press.

On the VDI: install as in step 1 (`setup_offline.cmd`), `.env` with the
user's own login (read and write is enough), then `scripts\ui.cmd` and use
the browser on that VDI. Do not run `setup_tables.cmd` or `extract.cmd`
there unless that login has the rights above.

## Providers

`PRPR` (`CMC_PRPR_PROV`) is the provider master. A member's PCP
(`CMC_MEPR_PRIM_PROV`) and a claim's servicing provider
(`CMC_CLCL_CLAIM.PRPR_ID`) both refer to it: `provider_denorm` holds it once,
and `member_pcp` and `claims_denorm` carry the provider id. Providers are
never generated.

## Reference: the SQL scripts

SDM's own are in this folder; the health business pack's (the denorm and 837
tables, their indexes and extract) are in `sdg\packs\health\sql`.
`00_check.sql`, `01_sdm_upgrade.sql` here and `02_denorm_indexes_optional.sql`
to `04_denorm_837_extract.sql` there are the same steps as plain T-SQL, for an
existing SDM platform database whose tables are kept (an upgrade in place) or
where FACETS is in the same database. A new deployment does not need them:
`setup_tables.cmd` and `extract.cmd` do the same.
