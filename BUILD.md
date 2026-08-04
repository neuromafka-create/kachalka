# Сборка десктопного приложения «Качалка»

Приложение — **обычное окно Windows** (WebView2 / Edge), не Chrome и не вкладка браузера.

## Для себя (разработка)

1. `install.bat` — один раз  
2. `start.bat` — окно приложения  
3. `start_web.bat` — запасной режим в браузере (отладка)

## exe без установки Python у пользователя

1. `install.bat`  
2. `build_exe.bat`  
3. Запуск: `dist\Kachalka\Kachalka.exe`

Папка `dist\Kachalka\` — portable: можно скопировать на флешку.

## Инсталлятор Setup.exe

Порядок **строго такой** (иначе Inno Setup пишет *No files found matching dist\Kachalka\\**):

1. `build_exe.bat` — должен появиться `dist\Kachalka\Kachalka.exe`  
2. Установи [Inno Setup 6](https://jrsoftware.org/isinfo.php)  
3. `make_installer.bat` — или открой `installer.iss` → Compile  
4. Готово: `dist\Kachalka-Setup.exe`

Установка идёт в `%LocalAppData%\Kachalka` **без прав администратора**.

### Частая ошибка

```
No files found matching "...\dist\Kachalka\*"
```

Значит, **ещё не собрали exe**. Сначала `build_exe.bat`, потом инсталлятор.

## Что внутри окна

Тот же интерфейс: ссылка, видео/mp3, качество, папка, прогресс.  
Сервер крутится локально на `127.0.0.1:8031`, снаружи не торчит.

## Требования у пользователя

- Windows 10/11  
- [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) — обычно уже стоит с Edge  

## Размер

Ожидай **100–200+ МБ** (Python + yt-dlp + ffmpeg). Это нормально.
