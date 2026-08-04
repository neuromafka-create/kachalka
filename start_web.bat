@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Kachalka web
if not exist ".venv\Scripts\python.exe" (
  echo [!] First run install.bat
  pause
  exit /b 1
)
echo Kachalka web mode: http://localhost:8031
echo Stop: close this window or Ctrl+C
echo.
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:8031"
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8031
if errorlevel 1 (
  echo [!] Server failed. Run install.bat again.
  pause
)
