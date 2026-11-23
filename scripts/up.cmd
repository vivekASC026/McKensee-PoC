@echo off
setlocal
cd /d "%~dp0.."
rem Demo machines only: refuses on a customer machine (ENVIRONMENTS.md).
.venv\Scripts\python.exe -m sdg.cli.main demo-check || exit /b 1
.venv\Scripts\python.exe seed\create_databases.py %* || exit /b 1
.venv\Scripts\python.exe -m sdg.cli.main connect
