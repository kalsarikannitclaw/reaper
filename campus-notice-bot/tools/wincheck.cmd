@echo off
rem Windows self-test (native Windows package). Double-click it. It changes nothing and starts nothing:
rem   - not NapCat, not QQ, not a scheduled task. It only READS files, tries to bind free ports for a second, and runs the offline tests.
rem   - It makes NO network call unless you start it as:  wincheck.cmd --model   (then ONE tiny paid call to your own chat provider; the key is never printed).
rem Result: data\wincheck.txt (no keys and no QQ numbers; an error message may show a folder name). Send that file back if something fails.
cd /d "%~dp0.."
set "PYTHONPATH=%~dp0..\src"
set "PYTHONUTF8=1"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if "%PY%"=="python" where py >nul 2>nul && set "PY=py -3"
if not exist data mkdir data
echo Windows check %DATE% %TIME% > data\wincheck.txt

echo [1] Python >> data\wincheck.txt
%PY% -c "import sys,platform;print(sys.version.split()[0],platform.platform());print('OK' if sys.version_info>=(3,9) else 'TOO OLD: need 3.9+')" >> data\wincheck.txt 2>&1
%PY% -c "import zoneinfo;zoneinfo.ZoneInfo('Asia/Shanghai');print('time zones: OK')" >> data\wincheck.txt 2>&1
%PY% -c "import websockets;print('websockets', websockets.__version__)" >> data\wincheck.txt 2>&1

echo [2] Tesseract (picture text; optional) >> data\wincheck.txt
%PY% -c "import sys;sys.path.insert(0,'capture');import ocr;b=ocr.tesseract_bin();print('found' if b else 'NOT FOUND: install the UB-Mannheim build with Chinese, or set CNB_TESSERACT')" >> data\wincheck.txt 2>&1

echo [3] NapCat folder, config, ports >> data\wincheck.txt
%PY% stack\native_win.py --check >> data\wincheck.txt 2>&1
%PY% stack\native_win.py --selftest >> data\wincheck.txt 2>&1

echo [4] Bridge guard status (read only) >> data\wincheck.txt
if exist capture\bridge_status.json %PY% -c "import json;d=json.load(open('capture/bridge_status.json',encoding='utf-8'));print('state',d.get('state'),'backend',d.get('backend'),'logins_24h',d.get('logins_24h'))" >> data\wincheck.txt 2>&1
if not exist capture\bridge_status.json echo no status file yet: the guard has not run >> data\wincheck.txt

echo [5] Test suites >> data\wincheck.txt
for %%T in (core s2 s2a s2b s2d s3 s4 s5 s6b s8 s8b s9 s10 s10b) do (
  if exist tests\test_%%T.py (
    %PY% tests\test_%%T.py >data\wincheck_%%T.log 2>&1
    if errorlevel 1 (echo FAIL %%T >> data\wincheck.txt) else (echo PASS %%T >> data\wincheck.txt)
  )
)

echo [6] Chat program >> data\wincheck.txt
%PY% tools\talk.py --terminal --no-llm --db data\wincheck_empty.db < nul >> data\wincheck.txt 2>&1
if /i "%~1"=="--model" %PY% tools\talk.py --check >> data\wincheck.txt 2>&1
type data\wincheck.txt
echo.
echo Details: data\wincheck_*.log. Send data\wincheck.txt back (it holds no key).
pause
