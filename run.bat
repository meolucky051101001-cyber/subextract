@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Chưa có môi trường Python. Tạo bằng:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
start "" http://127.0.0.1:8787
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8787
pause
