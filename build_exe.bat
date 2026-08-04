@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Kachalka - build exe

if not exist ".venv\Scripts\python.exe" (
  echo [!] First run install.bat
  pause
  exit /b 1
)

if not exist "ffmpeg\bin\ffmpeg.exe" (
  echo [!] ffmpeg missing - run install.bat first
  pause
  exit /b 1
)

echo [1/3] Install build tools...
".venv\Scripts\python.exe" -m pip install -q pyinstaller pywebview
echo [2/3] Build Kachalka.exe - several minutes, please wait...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean kachalka.spec
if errorlevel 1 (
  echo [!] Build failed
  pause
  exit /b 1
)

echo [3/3] Done.
echo.
echo   Folder: dist\Kachalka\
echo   Run:    dist\Kachalka\Kachalka.exe
echo.
echo   Next: make_installer.bat  - makes dist\Kachalka-Setup.exe
echo         (needs Inno Setup 6)
echo.
pause
