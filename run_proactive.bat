@echo off
REM JSA daily runner - Task Scheduler points at THIS file. One run per weekday.
REM 1) pull latest code  2) check the token  3) start the email sweep on GitHub  4) run the proactive search
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

REM Token check: 200 = good. 401 = the token saved on this laptop is expired or wrong.
curl.exe -s -o nul -w "token check: HTTP %%{http_code}\n" -H "Authorization: token %JSA_GH_TOKEN%" https://api.github.com/repos/jglenn2nd-L-ohist/Toolkit >> jsa_run.log

REM Start the email sweep on GitHub now (204 = started). Its scheduled run is only a backup.
curl.exe -s -o nul -w "email sweep dispatch: HTTP %%{http_code}\n" -X POST -H "Authorization: token %JSA_GH_TOKEN%" -H "Accept: application/vnd.github+json" https://api.github.com/repos/jglenn2nd-L-ohist/Toolkit/actions/workflows/jsa_harvest.yml/dispatches -d "{\"ref\":\"main\"}" >> jsa_run.log

REM -u = unbuffered, so progress reaches the log live and survives a crash or kill
"C:\Users\jglen\AppData\Local\Python\bin\python.exe" -u jsa_proactive.py >> jsa_run.log 2>&1
echo finished with exit code %errorlevel% >> jsa_run.log
