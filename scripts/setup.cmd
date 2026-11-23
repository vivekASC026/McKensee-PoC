@echo off
setlocal
cd /d "%~dp0.."

if exist .venv\Scripts\python.exe goto install

rem The py launcher first: it is installed with Python from python.org and
rem finds it even when python.exe is not on PATH. A bare `python` on a fresh
rem Windows is often the Microsoft Store stub, which prints an error and
rem creates nothing.
set "PY="
py -3.13 -c "import sys" >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3 -c "import sys; assert sys.version_info >= (3, 13)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; assert sys.version_info >= (3, 13)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo Python 3.13 or later was not found. Install it from python.org, with
  echo "py launcher" ticked, then run this again.
  exit /b 1
)
echo Creating .venv with %PY%
%PY% -m venv .venv || exit /b 1

:install
.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel || exit /b 1
.venv\Scripts\python.exe -m pip install -c requirements.lock -e ".[dev]" || exit /b 1
if not exist .env ( copy .env.example .env )
echo.
echo Setup complete. Edit .env with your SQL Server password.
echo On a demo or development machine, then run scripts\seed.cmd.
echo At a customer, do not: follow scripts\customer\README.md, step 2.
