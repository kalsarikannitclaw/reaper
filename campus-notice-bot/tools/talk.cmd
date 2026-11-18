@echo off
rem Local chat with the Campus Notice Bot (Windows). Extra arguments go to talk.py, e.g. --terminal
cd /d "%~dp0.."
where py >nul 2>nul
if errorlevel 1 goto usepython
py -3 tools\talk.py %*
goto :eof
:usepython
python tools\talk.py %*
