@echo off
cd /d "%~dp0"
chcp 65001 >nul
title Качалка — проверка («доктор»)
echo ============================================
echo   Доктор: проверяю, всё ли готово к работе
echo ============================================
set OK=1

where python >nul 2>nul
if errorlevel 1 (
  echo [!!] Python НЕ найден. Скачай: https://www.python.org/downloads/
  echo      и поставь галочку "Add Python to PATH" при установке.
  set OK=0
) else (
  for /f "tokens=*" %%v in ('python --version') do echo [OK] %%v
)

if exist ".venv\Scripts\python.exe" (
  echo [OK] Виртуальное окружение создано
) else (
  echo [!!] Окружения нет — запусти install.bat
  set OK=0
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m yt_dlp --version >nul 2>nul
  if errorlevel 1 (
    echo [!!] yt-dlp не установлен — запусти install.bat
    set OK=0
  ) else (
    echo [OK] yt-dlp установлен
  )
  ".venv\Scripts\python.exe" -c "import certifi; import pathlib; assert pathlib.Path(certifi.where()).is_file()" >nul 2>nul
  if errorlevel 1 (
    echo [!?] certifi нет — HTTPS/VK могут падать. Запусти install.bat
  ) else (
    echo [OK] certifi ^(SSL-сертификаты^) на месте
  )
)

if exist "ffmpeg\bin\ffmpeg.exe" (
  echo [OK] ffmpeg на месте — видео будет в лучшем качестве
) else (
  echo [!?] ffmpeg не скачан — Качалка работает, но качество ниже.
  echo      Запусти install.bat ещё раз, чтобы докачать ^(зеркала, ~90 МБ^).
)

ping -n 2 8.8.8.8 >nul 2>nul
if errorlevel 1 (
  echo [!?] Интернет не отвечает — проверь подключение
) else (
  echo [OK] Интернет есть
)

echo.
if "%OK%"=="1" (
  echo ВСЁ ГОТОВО! Запускай start.bat
) else (
  echo Есть проблемы — смотри строки [!!] выше. Если непонятно, скопируй
  echo весь этот текст и отправь нейросети: «объясни и скажи, что делать».
)
pause
