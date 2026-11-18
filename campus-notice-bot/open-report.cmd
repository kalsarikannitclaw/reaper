@echo off
rem ONE program: refresh, then calendar + chat (+ by-chat-group list) on http://127.0.0.1:8790/r/calendar.html
rem Lives in the package root, next to .venv and reports. Keep the window open; close it to stop.
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
set "PYTHONUTF8=1"
"%~dp0.venv\Scripts\python.exe" -m cnb.cli daily
"%~dp0.venv\Scripts\python.exe" -m cnb.cli talk --open
echo.
echo The bot has stopped (or could not start). Read the lines above, then press a key.
pause >nul
