@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Kachalka - make installer

if not exist "dist\Kachalka\Kachalka.exe" (
  echo [!] First build the app: run build_exe.bat
  echo     Need folder: dist\Kachalka\Kachalka.exe
  pause
  exit /b 1
)

set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not defined ISCC (
  echo [!] Inno Setup 6 not found.
  echo     Install: https://jrsoftware.org/isinfo.php
  echo     Then run this bat again, or open installer.iss in Compiler.
  pause
  exit /b 1
)

echo Compiling installer with:
echo   %ISCC%
echo.
"%ISCC%" "%~dp0installer.iss"
if errorlevel 1 (
  echo.
  echo [!] Compile failed - see messages above.
  pause
  exit /b 1
)

echo.
echo OK: dist\Kachalka-Setup.exe
if exist "dist\Kachalka-Setup.exe" dir "dist\Kachalka-Setup.exe"
pause
