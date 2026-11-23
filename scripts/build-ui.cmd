@echo off
setlocal
cd /d "%~dp0..\ui"
call npm install
if errorlevel 1 (
  echo npm install failed; the interface was not built. Fix the error above and run this again.
  exit /b 1
)
call npm run build
if errorlevel 1 (
  echo The interface build failed. Fix the error above and run this again.
  exit /b 1
)
