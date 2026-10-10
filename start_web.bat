@echo off
cd /d %~dp0
echo photo-curator Web 工作台: http://127.0.0.1:8618  (Ctrl+C 停止)
.venv\Scripts\python.exe -m uvicorn scripts.webapp:app --host 127.0.0.1 --port 8618 --app-dir scripts
