@echo off
setlocal
rem The free-threaded Python (python3.14t) runs with the GIL on, as pyodbc needs.
set PYTHON_GIL=1
cd /d "%~dp0.."
if not exist ui\dist (
  echo Building the interface...
  pushd ui
  call npm install
  if errorlevel 1 (
    popd
    echo npm install failed, so the interface was not built. Fix the error above and run this again.
    exit /b 1
  )
  call npm run build
  if errorlevel 1 (
    popd
    if exist ui\dist rmdir /s /q ui\dist
    echo The interface build failed. Fix the error above and run this again.
    exit /b 1
  )
  popd
)
.venv\Scripts\python.exe -c "import sys" >nul 2>&1 || (
  echo.
  echo The Python this SDM was set up with is no longer installed.
  echo Copy the sdm-wheels parts for the Python there is now into this folder
  echo and run scripts\setup_offline.cmd, then run this again.
  exit /b 1
)
.venv\Scripts\python.exe -m sdg.cli.main ui %*
