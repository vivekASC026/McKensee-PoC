@echo off
setlocal
rem The free-threaded Python (python3.14t) runs with the GIL on, as pyodbc needs.
set PYTHON_GIL=1
cd /d "%~dp0.."

rem Installs the tool without internet and without the company's package
rem mirror: every package comes from the wheels folder. Copy the parts
rem sdm-wheels-cp315.001, .002, ... (for this machine's Python) into the SDM
rem folder first; this joins and unpacks them into wheels.

rem A .venv whose Python was uninstalled or replaced (3.14 by 3.15) cannot
rem start; it is moved aside and made again with the Python there is now.
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys" >nul 2>&1 && goto unpack
  echo The .venv's Python is no longer installed; moving it to .venv-old and making it again.
  if exist .venv-old rmdir /s /q .venv-old
  move .venv .venv-old >nul || exit /b 1
)

set "PY="
py -3.13 -c "import sys" >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3 -c "import sys; assert (3, 12) <= sys.version_info[:2] <= (3, 15)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; assert (3, 12) <= sys.version_info[:2] <= (3, 15)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo Python 3.12 to 3.15 was not found. Install Python 3.13 from
  echo python.org, with "py launcher" ticked, then run this again.
  exit /b 1
)
echo Creating .venv with %PY%
%PY% -m venv .venv || exit /b 1

:unpack
if exist sdm-wheels-*.001 (
  .venv\Scripts\python.exe scripts\unpack_wheels.py %* || exit /b 1
) else if not exist wheels (
  echo.
  echo No sdm-wheels-*.001 parts in this folder. Copy the parts for this
  echo machine's Python into it, then run this again.
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements-runtime.txt || exit /b 1
rem The web server's speed-ups: not every Python has a build of them, and the
rem tool runs without them.
for %%p in (httptools watchfiles websockets) do (
  .venv\Scripts\python.exe -m pip install --no-index --find-links wheels -c requirements-optional.txt %%p >nul 2>&1 || echo %%p is not built for this Python; the tool runs without it.
)
rem Offline is the customer's install: its settings, not the demo's.
if not exist .env ( copy scripts\customer\customer.env.example .env )
echo.
echo Setup complete. Fill in the ^<...^> parts of .env, then run scripts\customer\setup_tables.cmd
