@echo off
REM JSA proactive runner - point Task Scheduler at THIS file, not at jsa_proactive.py.
REM Pulls the latest committed code first (retries network blips), then runs it.
cd /d "%~dp0"
echo ==== %date% %time% ==== >> jsa_run.log
set TRIES=0
:pull
set /a TRIES+=1
git pull --ff-only >> jsa_run.log 2>&1
if not errorlevel 1 goto pulled
if %TRIES% GEQ 3 (
  echo git pull FAILED after 3 tries - not running stale code >> jsa_run.log
  exit /b 1
)
echo git pull failed, try %TRIES% of 3 - retrying in 60s >> jsa_run.log
ping -n 61 127.0.0.1 >nul
goto pull
:pulled
git log -1 --format="running commit %%h %%s" >> jsa_run.log
REM -u = unbuffered, so progress reaches the log live and survives a crash or kill
"C:\Users\jglen\AppData\Local\Python\bin\python.exe" -u jsa_proactive.py >> jsa_run.log 2>&1
echo finished with exit code %errorlevel% >> jsa_run.log
