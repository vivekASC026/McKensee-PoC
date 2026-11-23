@echo off
setlocal
cd /d "%~dp0.."
rem Bring a machine that already runs SDM up to this version, after the new
rem files have been unpacked over the old ones. Keeps .env, saved patterns,
rem layouts and generation rules. Never drops or reseeds FACETS_QA or MDM_REF.
rem Safe to re-run. Close the interface (scripts\ui.cmd) before running it.

if not exist .env (
  echo No .env here. This is for a machine already set up; see DEPLOY.md for a new one.
  exit /b 1
)

rem Demo machines only: at a customer an upgrade would re-extract the demo
rem source over their store (ENVIRONMENTS.md).
.venv\Scripts\python.exe -m sdg.cli.main demo-check || exit /b 1

echo === packages
.venv\Scripts\python.exe -m pip install --quiet -c requirements.lock -e ".[dev]" || exit /b 1

.venv\Scripts\python.exe -m sdg.cli.main work-check || exit /b 1

echo === database schema: new tables and columns, extract views
.venv\Scripts\python.exe seed\bootstrap.py || exit /b 1

rem A demo source from before the customer's codes is rebuilt at its own size.
set SCALE=
for /f %%s in ('.venv\Scripts\python.exe seed\bootstrap.py --previous-source') do set SCALE=%%s
if defined SCALE (
  echo === the demo source is from the previous version: rebuilding at scale %SCALE%
  .venv\Scripts\python.exe seed\build.py --scale %SCALE% || exit /b 1
)

echo === refresh the analysis store, the pipeline and the rule repository
call scripts\first-run.cmd || exit /b 1

echo.
echo Upgrade complete. Start the interface with scripts\ui.cmd
