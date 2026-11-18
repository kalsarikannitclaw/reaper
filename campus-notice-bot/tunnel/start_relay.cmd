@echo off
rem Starts the fetch relay (tunnel). Windows. Leave this window open; close it to stop the relay.
title Campus Notice Bot relay
where py >nul 2>nul
if errorlevel 1 goto usepython
py -3 "%~dp0relay.py" %*
goto done
:usepython
python "%~dp0relay.py" %*
:done
pause
