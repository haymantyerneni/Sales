@echo off
rem Serves the dashboard at http://localhost:5555 on this machine only.
rem Started at boot by the "Harvey Dashboard" scheduled task. Open it from a
rem browser inside the Remote Desktop session; it has no login, so never
rem expose port 5555 to the internet.

cd /d "%~dp0..\.."
set PYTHONUTF8=1
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
if not exist data mkdir data

:loop
".venv\Scripts\harvey.exe" dashboard >> data\dashboard.log 2>&1
ping -n 61 127.0.0.1 > nul
goto loop
