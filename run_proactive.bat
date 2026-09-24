@echo off
REM JSA proactive runner — point Task Scheduler at THIS file, not at jsa_proactive.py.
REM It pulls the latest committed code first, so what runs = what's in the repo.
REM Set "Start in" to your local Toolkit folder.

cd /d "%~dp0"
echo ==== %date% %time% ==== >> jsa_run.log
git pull --ff-only >> jsa_run.log 2>&1
if errorlevel 1 (
  echo git pull FAILED - not running stale code >> jsa_run.log
  exit /b 1
)
git log -1 --format="running commit %%h %%s" >> jsa_run.log
"C:\Users\jglen\AppData\Local\Python\bin\python.exe" jsa_proactive.py >> jsa_run.log 2>&1
