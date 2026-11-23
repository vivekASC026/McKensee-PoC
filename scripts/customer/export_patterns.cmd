@echo off
setlocal
rem The free-threaded Python (python3.14t) runs with the GIL on, as pyodbc needs.
set PYTHON_GIL=1
cd /d "%~dp0..\.."
rem Writes every saved pattern to exports\patterns-<date>-<time>.xlsx, or to the
rem file named after the command. Reads only the sdm pattern tables of the SDM
rem platform database; writes nothing to any database.
.venv\Scripts\python.exe scripts\customer\export_patterns.py %*
