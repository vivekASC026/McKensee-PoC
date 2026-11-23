@echo off
setlocal
rem The free-threaded Python (python3.14t) runs with the GIL on, as pyodbc needs.
set PYTHON_GIL=1
cd /d "%~dp0..\.."
rem Empties the SDM platform denorm tables and fills them again from the source
rem SDG_EXTRACT_SOURCE names in .env, else the active pack's default (health: FACETS).
rem The source is read through SDG_MSSQL_* / SDG_DB_SOURCE; the denorm is written
rem through SDG_DENORM_*. The SELECTs are in the active pack's extract folder:
rem sdg\packs\health\extract\FACETS.sql for FACETS.
rem Add --table member_denorm (repeatable) to refresh only some tables.
.venv\Scripts\python.exe -m sdg.cli.main extract --configured --keep-going %*
if errorlevel 1 exit /b 1
echo.
echo Extract complete. Patterns can be found on the refreshed data.
