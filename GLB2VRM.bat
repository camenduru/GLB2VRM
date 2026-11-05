@echo off
rem GLB2VRM launcher: double-click to open the GLB -> VRM UI in the browser.
rem Blender 4.x must be installed; set GLB2VRM_BLENDER if it is not found.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3 was not found.
    echo Install Python 3.10 or newer from https://www.python.org/ and run this file again.
    pause
    exit /b
)
start "" /b python -c "import time,webbrowser; time.sleep(1.5); webbrowser.open('http://127.0.0.1:8020')"
python glb2vrm\server.py --port 8020
pause
