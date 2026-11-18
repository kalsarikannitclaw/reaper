@echo off
rem Run any bot command:  cnb key set | cnb setup | cnb daily | cnb checkurl URL ...
set "PYTHONPATH=%~dp0src"
set "NO_PROXY=localhost,127.0.0.1"
set "PYTHONUTF8=1"
"%~dp0.venv\Scripts\python.exe" -m cnb.cli %*
