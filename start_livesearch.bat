@echo off
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "live.py"
) else (
    start "" python "live.py"
)
