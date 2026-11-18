@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 install.py %* & goto :end)
where python >nul 2>nul && (python install.py %* & goto :end)
echo Python 3 not found. Install Python 3.9+ from https://www.python.org/downloads/ (tick "Add to PATH").
:end
pause
