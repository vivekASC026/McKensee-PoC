@echo off
setlocal enabledelayedexpansion
rem Start, stop, or check SDM.
rem   scripts\sdm.cmd start     SDM in a window of its own, and the browser on it
rem   scripts\sdm.cmd stop      stops the SDM listening on the port
rem   scripts\sdm.cmd status    says whether it is running
rem A second argument names the port (8777 unless given): scripts\sdm.cmd stop 8780
cd /d "%~dp0.."

set "ACTION=%~1"
set "PORT=%~2"
if "%PORT%"=="" set "PORT=8777"

if /i "%ACTION%"=="start" goto start
if /i "%ACTION%"=="stop" goto stop
if /i "%ACTION%"=="status" goto status
echo Usage: scripts\sdm.cmd start ^| stop ^| status [port]
exit /b 1

:start
call :listening
if defined PID (
  echo SDM is already running on http://127.0.0.1:%PORT% ^(process !PID!^).
  start "" "http://127.0.0.1:%PORT%"
  exit /b 0
)
echo Starting SDM on http://127.0.0.1:%PORT% ...
start "SDM on port %PORT%" /min cmd /c ""%~dp0ui.cmd" --port %PORT%"
rem Wait up to a minute for it to answer.
for /l %%i in (1,1,60) do (
  call :listening
  if defined PID (
    echo SDM is running ^(process !PID!^). Stop it with: scripts\sdm.cmd stop
    exit /b 0
  )
  rem A second: timeout needs a console, which a scheduled job has not.
  ping -n 2 127.0.0.1 >nul
)
echo SDM did not start within a minute. The "SDM on port %PORT%" window says why.
exit /b 1

:stop
call :listening
if not defined PID (
  echo SDM is not running on port %PORT%.
  exit /b 0
)
rem Only SDM's own Python is stopped, never another program on the port.
tasklist /fi "PID eq %PID%" /fo csv /nh | findstr /i "python" >nul || (
  echo Port %PORT% is used by process %PID%, which is not SDM's Python; it was left running.
  exit /b 1
)
taskkill /pid %PID% /t /f >nul 2>&1
for /l %%i in (1,1,10) do (
  call :listening
  if not defined PID (
    echo SDM stopped.
    exit /b 0
  )
  rem A second: timeout needs a console, which a scheduled job has not.
  ping -n 2 127.0.0.1 >nul
)
echo SDM did not stop; process %PID% is still on port %PORT%.
exit /b 1

:status
call :listening
if defined PID (
  echo SDM is running on http://127.0.0.1:%PORT% ^(process %PID%^).
) else (
  echo SDM is not running on port %PORT%.
)
exit /b 0

rem PID: the process listening on the port, or nothing.
:listening
set "PID="
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /r /c:":%PORT% .*LISTENING"') do (
  if not defined PID set "PID=%%p"
)
exit /b 0
