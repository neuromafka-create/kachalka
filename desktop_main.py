# Файл: desktop_main.py
# Десктопное окно «Качалка» (pywebview + встроенный сервер).
# Не открывает браузер — своё окно приложения.
from __future__ import annotations

import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
# Базовый порт; если занят — ищем свободный рядом
PREFERRED_PORT = 8031
PORT_RANGE = range(8031, 8051)

# Заполняется при старте
PORT = PREFERRED_PORT
URL = f"http://{HOST}:{PORT}"

_server_error: str | None = None
_server_ready = threading.Event()

WEBVIEW2_URL = (
    "https://developer.microsoft.com/microsoft-edge/webview2/"
    "#download-the-webview2-runtime"
)


def _fix_stdio() -> None:
    """В .exe без консоли (console=False) sys.stdout/stderr = None.
    uvicorn падает на isatty() — подставляем «пустышки».
    """
    import os

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _log_path() -> Path:
    return _app_dir() / "kachalka_error.log"


def _write_log(text: str) -> None:
    try:
        path = _log_path()
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _msgbox(text: str, title: str = "Качалка", flags: int = 0x10) -> int:
    """MessageBoxW. flags: 0x10=error, 0x40=info, 0x1=OKCancel → 1=OK, 2=Cancel."""
    try:
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(0, text, title, flags))
    except Exception:
        print(text, file=sys.stderr)
        return 1


def _pick_folder_mode() -> int:
    """Режим выбора папки: Kachalka.exe --pick-folder <dir> <result.txt>"""
    if len(sys.argv) < 4:
        return 2
    sys.argv = [sys.argv[0], sys.argv[2], sys.argv[3]]
    from pick_folder import main as pick_main

    return int(pick_main())


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _can_bind(host: str, port: int) -> bool:
    """Проверка, что порт свободен для прослушивания."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _pick_port() -> int:
    for port in PORT_RANGE:
        if _can_bind(HOST, port):
            return port
    return PREFERRED_PORT


def _run_uvicorn(port: int) -> None:
    """Поток сервера: импортируем app-объект (не строку 'app:app' — в .exe так надёжнее)."""
    global _server_error
    try:
        _fix_stdio()
        import asyncio

        # В потоке нужен свой event loop (Windows + frozen)
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:
            pass

        import uvicorn
        from app import app as fastapi_app

        config = uvicorn.Config(
            fastapi_app,
            host=HOST,
            port=port,
            log_level="warning",
            access_log=False,
            use_colors=False,
        )
        server = uvicorn.Server(config)
        server.run()
    except Exception:
        _server_error = traceback.format_exc()
        _write_log(_server_error)
        _server_ready.set()  # разблокировать ожидание — с ошибкой


def _wait_server(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _server_error:
            return False
        if _port_open(HOST, port):
            return True
        if _server_ready.is_set() and _port_open(HOST, port):
            return True
        time.sleep(0.1)
    return _port_open(HOST, port)


def _resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


def _ensure_sys_path() -> None:
    """В frozen-сборке модули лежат в _MEIPASS."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass = str(sys._MEIPASS)
        if meipass not in sys.path:
            sys.path.insert(0, meipass)


def _start_server() -> bool:
    """Поднятие uvicorn. True, если URL доступен."""
    global PORT, URL

    if _port_open(HOST, PREFERRED_PORT):
        PORT = PREFERRED_PORT
        URL = f"http://{HOST}:{PORT}"
        return True

    PORT = _pick_port()
    URL = f"http://{HOST}:{PORT}"
    _server_ready.clear()
    thread = threading.Thread(
        target=_run_uvicorn, args=(PORT,), name="uvicorn", daemon=True
    )
    thread.start()
    if not _wait_server(PORT):
        detail = _server_error or (
            "Сервер не ответил вовремя.\n"
            f"Порт: {PORT}\n"
            f"Лог: {_log_path()}"
        )
        short = detail if len(detail) <= 900 else detail[:900] + "\n…"
        _msgbox(
            "Не удалось запустить Качалку.\n\n"
            f"{short}\n\n"
            "Что попробовать:\n"
            "• Закрой все окна Качалки в Диспетчере задач\n"
            "• Перезагрузи ПК\n"
            f"• Пришли файл {_log_path().name} из папки программы"
        )
        return False
    return True


def _open_browser_fallback() -> int:
    """Запасной режим: открыть в системном браузере, сервер держим живым."""
    try:
        webbrowser.open(URL)
    except Exception:
        _msgbox(f"Открой вручную в браузере:\n{URL}")
        return 1

    _msgbox(
        "Окно приложения недоступно — открыла Качалку в браузере.\n\n"
        f"{URL}\n\n"
        "Чтобы было своё окно, поставь Microsoft Edge WebView2 Runtime:\n"
        f"{WEBVIEW2_URL}\n\n"
        "Не закрывай это сообщение, пока пользуешься Качалкой — "
        "после «OK» программа завершится.\n"
        "(Или просто пользуйся вкладкой браузера; сервер работает, "
        "пока открыт процесс Качалки.)",
        flags=0x40,  # info
    )
    # Держим процесс, пока пользователь не закроет — иначе daemon-сервер умрёт.
    # Простой цикл: пока порт жив и процесс не убьют.
    try:
        while _port_open(HOST, PORT):
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    return 0


def _looks_like_webview2_missing(err: BaseException | str) -> bool:
    text = str(err).lower()
    keys = (
        "webview2",
        "web view2",
        "edgechromium",
        "microsoft edge",
        "environment variable",
        "could not find",
        "runtime",
        "0x80070002",  # file not found
        "0x8007139f",
    )
    return any(k in text for k in keys)


def _start_native_window() -> bool:
    """Открыть pywebview. False — не вышло (можно fallback)."""
    try:
        import webview
    except ImportError as exc:
        _write_log(traceback.format_exc())
        _msgbox(
            "Не удалось загрузить интерфейс (pywebview).\n"
            "Переустанови Качалку из Kachalka-Setup.exe.\n\n"
            f"{exc}"
        )
        return False

    # Только параметры, которые поддерживает pywebview 4/5/6
    window_kwargs = {
        "title": "Качалка",
        "url": URL,
        "width": 640,
        "height": 980,
        "min_size": (480, 640),
        "background_color": "#0f1117",
        "text_select": True,
    }

    try:
        webview.create_window(**window_kwargs)
        # Явно Edge WebView2 на Windows; если параметр не принят — без gui=
        try:
            webview.start(gui="edgechromium", debug=False)
        except Exception:
            webview.start(debug=False)
        return True
    except Exception as exc:
        err = traceback.format_exc()
        _write_log(err)

        if _looks_like_webview2_missing(exc) or _looks_like_webview2_missing(err):
            choice = _msgbox(
                "Для окна Качалки нужен Microsoft Edge WebView2 Runtime.\n\n"
                "OK — открыть страницу загрузки WebView2\n"
                "Отмена — открыть Качалку в обычном браузере\n\n"
                f"Лог: {_log_path()}",
                flags=0x31,  # OK Cancel + error icon
            )
            if choice == 1:  # OK
                try:
                    webbrowser.open(WEBVIEW2_URL)
                except Exception:
                    pass
            return False

        # Другая ошибка — показать текст + fallback
        short = str(exc)
        if len(short) > 400:
            short = short[:400] + "…"
        _msgbox(
            "Не удалось открыть окно Качалки.\n\n"
            f"{short}\n\n"
            f"Подробности: {_log_path()}\n"
            "Открою в браузере."
        )
        return False


def _set_app_user_model_id() -> None:
    """Чтобы панель задач брала иконку exe, а не «питоновскую дискету»."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "NeyroVibe.Kachalka.1.0"
        )
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--pick-folder":
        return _pick_folder_mode()

    _fix_stdio()
    _ensure_sys_path()
    _set_app_user_model_id()

    if not _start_server():
        return 1

    if _start_native_window():
        return 0

    # Запасной режим — браузер (программа всё равно работает)
    return _open_browser_fallback()


if __name__ == "__main__":
    raise SystemExit(main())
