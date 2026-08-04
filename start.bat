@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Kachalka
if not exist ".venv\Scripts\python.exe" goto :no_venv

echo Starting Kachalka desktop window...
".venv\Scripts\python.exe" desktop_main.py
if errorlevel 1 goto :fail
goto :eof

:no_venv
echo [!] First run install.bat
pause
exit /b 1

:fail
echo.
echo [!] Failed to start. Run install.bat again.
echo     Web mode fallback: start_web.bat
pause
exit /b 1
