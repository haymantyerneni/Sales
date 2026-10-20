@echo off
rem Keeps Harvey running unattended on Windows: restarts it 60 seconds after
rem any exit or crash. Started at boot by the "Harvey" scheduled task (see
rem install-tasks.ps1). Output goes to data\harvey.log, which the dashboard's
rem log view also reads.

cd /d "%~dp0..\.."
rem Log files are UTF-8 (the dashboard reads them as UTF-8).
set PYTHONUTF8=1
rem The Claude Code installer puts claude.exe here; a scheduled task may not
rem have the interactive PATH.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
if not exist data mkdir data

:loop
echo [%date% %time%] Starting Harvey>> data\harvey.log
".venv\Scripts\harvey.exe" run >> data\harvey.log 2>&1
echo [%date% %time%] Harvey exited with code %errorlevel%; restarting in 60 seconds>> data\harvey.log
rem `timeout` needs a console, which a scheduled task doesn't have.
ping -n 61 127.0.0.1 > nul
goto loop
