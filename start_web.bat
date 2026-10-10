@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 未找到 Python 环境，请先运行 setup.bat。
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "scripts\launch_web.py" %*
if errorlevel 1 (
    echo.
    echo 启动失败，请查看上方错误信息。
    pause
    exit /b 1
)
