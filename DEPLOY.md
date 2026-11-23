# Setting SDM up on another machine

For a Windows machine with **nothing on it**: no SQL Server, no databases, no
Python. Everything is built there from what is in this repository -- the
databases, the demo data in them, the Python environment. Nothing reaches back
to the machine it came from.

Allow about an hour, most of it installers. The SDM steps themselves take about
five minutes.

> This is for a demo laptop with its own SQL Server and demo data. **At a
> customer, follow `scripts/customer/README.md` instead**: it points the tool
> at their SDM platform database and loads it from their FACETS.
> `ENVIRONMENTS.md` says which machine is which and how each one is updated.

## 0. What you need

| | |
|---|---|
| Windows | 10 or 11, 64-bit, with administrator rights for the installs |
| Disk | about 10 GB free: SQL Server, and about 3 GB of databases to start |
| Internet | for the installers and `pip`. See *No internet* at the end otherwise |

## 1. Install SQL Server

Download **SQL Server Developer edition** from Microsoft (search "SQL Server
Developer download"). It is free for anything that is not production.

- Choose **Basic** install. This gives a *default instance*, which is reached as
  `localhost`, and makes you -- the Windows user running the installer -- an
  administrator of it. SDM signs in as you; no SQL password is needed.
- Express edition also works, but it caps each database at 10 GB, and the
  analysis store grows with use. If you use Express anyway, it installs as a
  *named instance*: use `localhost\SQLEXPRESS` as the server in step 5.

## 2. Install the ODBC driver

Download **ODBC Driver 18 for SQL Server** (x64) from Microsoft and install it.
This is the step people forget, and without it every error points at the
connection rather than the driver. Check it:

```
powershell "Get-OdbcDriver | Where-Object Name -like '*SQL Server*' | Select Name"
```

`ODBC Driver 18 for SQL Server` must be in the list.

## 3. Install Python

**Python 3.13** from python.org. In the installer, leave **"py launcher"**
ticked; "Add python.exe to PATH" is optional. Check it:

```
py -3.13 --version
```

Node is **not** needed; the interface is shipped prebuilt.

## 4. Get the code

Either clone it:

```
git clone <repository URL> C:\SDM
```

or download the zip and unpack it to `C:\SDM`. The rest of this assumes
`C:\SDM`; any folder works.

## 5. Install SDM and point it at SQL Server

```
cd C:\SDM
scripts\setup.cmd
```

Creates `.venv`, installs the exact package versions the test suite last passed
on (`requirements.lock`), and copies `.env.example` to `.env`. It stops with a
message if it cannot find Python 3.13.

Then edit `.env`. For SQL Server installed as above, signed in as yourself:

```
SDG_ENV=laptop
SDG_MSSQL_SERVER=localhost
SDG_MSSQL_TRUSTED=yes
```

(`localhost\SQLEXPRESS` for Express.) Leave the database names as they are:
they do not exist yet, and the next step creates them under those names.

To sign in with a SQL login instead, leave `SDG_MSSQL_TRUSTED=no` and set
`SDG_MSSQL_USER` and `SDG_MSSQL_PASSWORD`. That login needs permission to
create databases.

## 6. Create the databases and the demo data

```
scripts\bootstrap.cmd --check     optional: lists what is missing, creates nothing
scripts\seed.cmd                  about 20 seconds
```

Creates three databases and fills them:

| | |
|---|---|
| `FACETS_QA` | the demo source: 20,000 subscribers, 46,026 members, 210,603 claims, 648,128 claim lines |
| `MDM_REF` | reference data |
| `SDM_DENORM` | the analysis store and SDM's own tables: patterns, rules, layouts |

`seed.cmd` **drops and rebuilds the tables in `FACETS_QA` and `MDM_REF`**. On a
fresh machine that is what you want; never point it at a database you care
about. It never empties `SDM_DENORM`.

## 7. First run

```
scripts\first-run.cmd             about 2 minutes
```

Once, in order: reads the source into the analysis store, describes what is
there, runs the whole pipeline end to end (discover, profile, map, review,
plan, generate, validate, then removes what it generated), applies the Review
decisions already made on this project (`seed/decisions.py`), and fills the
rule repository from the plan. It ends with a table of success measures; "post-run
validation: passed" is the line that matters.

## 8. Start it

```
scripts\ui.cmd
```

Open <http://127.0.0.1:8777>. The interface and the API are one process; close
the window to stop it.

## 9. Check it works

```
scripts\test.cmd                  about 15 minutes
```

Expect every test to pass. The suite writes under its own seed and removes
exactly what it wrote, so it can be run at any time; it fails if any saved
pattern it did not create is changed or deleted. It includes scenario tests
that run the setup scripts on throwaway `SDMT_*` databases, and browser tests
that drive the screens in the Edge (or Chrome) already on the machine -- no
browser is downloaded. The account running it needs permission to create
databases.

## Upgrading a machine already set up

For a laptop that already runs SDM from an earlier zip:

1. Close the interface window.
2. Unpack the new zip over the old folder, replacing files. `.env`, `.venv`
   and `work\` are not in the zip, so they are kept.
3. Add `SDG_ENV=laptop` to `.env` if it is not there yet, then run:

```
scripts\upgrade.cmd               about 2 minutes
```

`VERSION.txt` in the SDM folder says which build is now there.

It installs any new packages, adds new tables and columns to `SDM_DENORM`
(saved patterns, layouts and rules are kept), brings the extract views in
`FACETS_QA` up to date, refreshes the analysis store from `FACETS_QA`, and
reruns the pipeline. It never drops or reseeds `FACETS_QA` or `MDM_REF`. It ends
with "Upgrade complete".

If the interface is working on a database other than `FACETS_QA`, the pipeline
step stops rather than overwrite that database's files. Choose `FACETS_QA` on
Connect, close the interface, and run `upgrade.cmd` again.

## Optional: SNIP checks and code lists

The EDI screen's SNIP checks read rule files and need no setup. To adapt them
for a customer -- add rules, switch one off, add a trading partner's rules --
put files in `C:\SDM\work\snip\`; nothing in the code changes, and an
upgrade keeps them. `sdg\writers\snip\rules\README.md` explains every rule.

For full code-set checking (SNIP level 5), download from CMS and save, one
code per line without the dot:

| File | From |
|---|---|
| `work\snip\codesets\icd10cm.txt` | ICD-10-CM code descriptions (cms.gov) |
| `work\snip\codesets\hcpcs.txt` | HCPCS Level II quarterly file (cms.gov) |

Without them, level 5 is reported as partly checked rather than passed.

## If something goes wrong

| Symptom | Cause |
|---|---|
| `Data source name not found and no default driver specified` | ODBC Driver 18 is missing (step 2), or `.env` names a different driver |
| `Login failed for user` | with `SDG_MSSQL_TRUSTED=yes`: you are not the user who installed SQL Server. Run as that user, or add yours in SSMS as a sysadmin. With a SQL login: wrong password, or SQL logins are disabled on the server |
| `A network-related or instance-specific error` | SQL Server service not running, or the server name is wrong. Check *Services* for `SQL Server (MSSQLSERVER)`, or `(SQLEXPRESS)` |
| `certificate chain was issued by an authority that is not trusted` | `SDG_MSSQL_TRUST_CERT=yes` in `.env` |
| `setup.cmd` says Python was not found | step 3. A bare `python` on a new Windows is often the Microsoft Store stub, which is why setup uses `py` |
| port 8777 busy | `scripts\ui.cmd --port 8778` |

## What is not in the repository, and why

| | |
|---|---|
| `.env` | holds your connection settings and possibly a password. Copied from `.env.example` |
| `.venv` | 400 MB of platform-specific packages. `setup.cmd` builds it |
| `work/` | one installation's working state: plans, mappings, decisions. `first-run.cmd` builds it |
| `ui/node_modules` | only needed to change the interface |

The built interface, `ui/dist`, **is** in the repository, so neither a clone
nor the zip needs Node. Rebuild it only after changing `ui/src`.

## Where things are

| | |
|---|---|
| `STATUS.md` | current state and every known gap. **Read this first** |
| `README.md` | how the thing works |
| `sdg/` | the tool |
| `seed/` | the demo fixture and the schema it builds |
| `ui/src/` | the interface source (`npm install && npm run build` in `ui/`) |

## No internet

Only `setup.cmd` needs it. On a machine that does have internet, the same
Windows and Python 3.13:

```
py -3.13 -m pip download -d wheelhouse -r requirements.lock
```

Copy `wheelhouse\` into `C:\SDM`, then on the target, instead of `setup.cmd`:

```
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --no-index --find-links wheelhouse -c requirements.lock -e ".[dev]"
copy .env.example .env
```

Everything after that is local.
