@echo off
cd /d "%~dp0"

echo Installing dependencies for PoE Live Search...
python -m pip install --upgrade pip
python -m pip install requests curl_cffi browser_cookie3
if errorlevel 1 (
    echo.
    echo Installation failed. See the errors above.
    pause
    exit /b 1
)

echo.
echo Done. You can now run start_livesearch.bat
pause
