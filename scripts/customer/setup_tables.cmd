@echo off
setlocal
rem The free-threaded Python (python3.14t) runs with the GIL on, as pyodbc needs.
set PYTHON_GIL=1
cd /d "%~dp0..\.."
rem Creates every SDM table in the SDM platform database named in .env
rem (SDG_DENORM_* and SDG_DB_DENORM, schema SDG_DENORM_SCHEMA): the denorm
rem tables, the tools own tables and the reference lists. Add-only; safe to
rem run again. Does not touch FACETS and creates no database.
rem --no-indexes leaves the denorm tables without indexes.
.venv\Scripts\python.exe -m sdg.cli.main customer-tables %*
if errorlevel 1 exit /b 1
