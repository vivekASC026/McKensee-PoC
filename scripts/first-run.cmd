@echo off
setlocal
cd /d "%~dp0.."
rem Everything after seed.cmd that a new machine needs once: read the source
rem into the analysis store, describe it, run the pipeline end to end (which
rem leaves the plan the interface works from), apply the review decisions
rem already made on this project, and fill the rule repository from the
rem plan. Safe to re-run. Demo machines only (ENVIRONMENTS.md).
.venv\Scripts\python.exe -m sdg.cli.main demo-check || exit /b 1
set "SRC=FACETS_QA"
for /f "tokens=1,* delims==" %%a in ('findstr /b "SDG_DB_SOURCE=" .env 2^>nul') do set "SRC=%%b"
rem A name set in the environment wins over .env, as it does everywhere else.
if defined SDG_DB_SOURCE set "SRC=%SDG_DB_SOURCE%"

rem The pipeline below writes into work/. Stop now, not two minutes in, if the
rem interface is working on another database whose files are there.
.venv\Scripts\python.exe -m sdg.cli.main work-check || exit /b 1

echo === extract %SRC% into the analysis store
.venv\Scripts\python.exe -m sdg.cli.main extract %SRC% || exit /b 1
echo === landscape
.venv\Scripts\python.exe -m sdg.cli.main landscape %SRC% || exit /b 1
echo === pipeline, end to end
.venv\Scripts\python.exe -m sdg.cli.main demo || exit /b 1
echo === review decisions already made on this project
.venv\Scripts\python.exe seed\decisions.py || exit /b 1
echo === rule repository
.venv\Scripts\python.exe -m sdg.analyzer.services.seed_elements --apply || exit /b 1
echo.
echo First run complete. Start the interface with scripts\ui.cmd
