@echo off
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo Setup complete. See README.md for ExifTool, FFmpeg, and optional AI models.
pause
