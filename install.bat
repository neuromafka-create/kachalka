@echo off
cd /d "%~dp0"
chcp 65001 >nul
title Качалка — установка
echo ============================================
echo   Качалка — установка (займёт 2-3 минуты)
echo ============================================
where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python не найден — это язык, на котором написана Качалка.
  echo     Скачай с https://www.python.org/downloads/
  echo     При установке ОБЯЗАТЕЛЬНО поставь галочку "Add Python to PATH",
  echo     потом запусти install.bat ещё раз.
  pause
  exit /b 1
)
echo.
echo [1/3] Создаю рабочее окружение...
echo   Зачем: отдельная папка .venv — Качалка живёт в ней и ничего
echo   не трогает в системе. Идёт молча ~30-60 секунд, это нормально.
if not exist ".venv\Scripts\python.exe" python -m venv .venv
echo.
echo [2/3] Ставлю yt-dlp, fastapi, uvicorn, pywebview, certifi...
echo   Зачем: yt-dlp — скачивание; fastapi/uvicorn — движок окна;
echo   pywebview — привычное окно программы без браузера;
echo   certifi — сертификаты HTTPS (без них VK и др. сайты могут падать по SSL).
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install yt-dlp fastapi "uvicorn[standard]" pywebview certifi -q
echo.
echo [3/3] Скачиваю портативный ffmpeg (~90 МБ, один раз)...
echo   Зачем: склеивает видеодорожку со звуком — без него YouTube отдаёт
echo   файл похуже качеством. Качаю с быстрых зеркал, с прогрессом.
".venv\Scripts\python.exe" get_ffmpeg.py
echo.
echo Установка завершена — запускаю проверку...
echo.
call "%~dp0check.bat"
