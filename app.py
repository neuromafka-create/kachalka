# Файл: app.py
# «Качалка» — локальное веб-приложение: ссылка → видео.
# Тот же формат и ffmpeg, что в kachalka.py; прогресс через progress hooks.
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yt_dlp
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

def _app_dir() -> Path:
    """Папка приложения: рядом с .exe (сборка) или с исходниками (разработка).
    Сюда пишем «Скачанное» — должна быть доступна на запись.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_dir() -> Path:
    """Ресурсы только для чтения: static, ffmpeg (в onefile — sys._MEIPASS)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return _app_dir()


BASE = _app_dir()
RESOURCE = _resource_dir()
DEFAULT_OUT = BASE / "Скачанное"
# ffmpeg: сначала рядом с exe, потом внутри бандла
_FFMPEG_CANDIDATES = (
    BASE / "ffmpeg" / "bin",
    RESOURCE / "ffmpeg" / "bin",
)
FFMPEG_BIN = next(
    (p for p in _FFMPEG_CANDIDATES if (p / "ffmpeg.exe").exists()),
    RESOURCE / "ffmpeg" / "bin",
)
STATIC = RESOURCE / "static"
if not STATIC.is_dir():
    STATIC = BASE / "static"

# Форматы видео: лучший mp4 со склейкой + ограничения по высоте
QUALITY_BEST = "best"
QUALITY_720 = "720"
QUALITY_480 = "480"
FORMAT_BY_QUALITY: dict[str, str] = {
    # Как в kachalka.py — лучшее mp4, иначе что есть
    QUALITY_BEST: "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
    # Не выше 720p: mp4 если можно, иначе любой <=720
    QUALITY_720: (
        "bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
        "b[height<=720][ext=mp4]/"
        "bv*[height<=720]+ba/"
        "b[height<=720]"
    ),
    # Не выше 480p — совсем лёгкое
    QUALITY_480: (
        "bv*[height<=480][ext=mp4]+ba[ext=m4a]/"
        "b[height<=480][ext=mp4]/"
        "bv*[height<=480]+ba/"
        "b[height<=480]"
    ),
}
MODE_VIDEO = "video"
MODE_AUDIO = "audio"

app = FastAPI(title="Качалка")

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "idle",  # idle | downloading | done | error
    "percent": 0.0,
    "speed": "",
    "size": "",
    "title": "",
    "filename": "",
    "error": "",
    "out_dir": str(DEFAULT_OUT),
    "warning": "",
    "mode": MODE_VIDEO,
    "quality": QUALITY_BEST,
}


def _format_bytes(n: float | int | None) -> str:
    if n is None:
        return ""
    x = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(x) < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(x)} {unit}"
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} ТБ"


def _format_speed(n: float | int | None) -> str:
    if not n:
        return ""
    return f"{_format_bytes(n)}/с"


def _human_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    low = msg.lower()
    if "unsupported url" in low or "no suitable extractor" in low:
        return "Ссылка не открылась — такой сайт не поддерживается"
    if "private video" in low or "sign in to confirm" in low or "login required" in low:
        return "Видео недоступно — оно закрыто или требует входа"
    if (
        "video unavailable" in low
        or "not available" in low
        or "has been removed" in low
        or "is not available" in low
    ):
        return "Видео недоступно"
    if "http error 404" in low or "unable to download webpage" in low:
        return "Ссылка не открылась — страница не найдена"
    if "http error 403" in low:
        return "Видео недоступно — доступ запрещён"
    if any(
        k in low
        for k in (
            "urlopen",
            "network",
            "timed out",
            "timeout",
            "connection",
            "getaddrinfo",
            "name or service not known",
            "failed to resolve",
        )
    ):
        return "Нет связи с сайтом — проверь интернет"
    if "ffmpeg" in low:
        return (
            "Ошибка ffmpeg при обработке файла. "
            "Запусти install.bat ещё раз, чтобы поставить ffmpeg"
        )
    if "invalid url" in low or "url is not valid" in low:
        return "Ссылка не открылась — проверь адрес"
    short = msg.split("\n")[0][:200]
    return f"Не получилось скачать: {short}"


def _resolve_out_dir(folder: str | None) -> tuple[Path, str]:
    """Вернуть папку для сохранения и предупреждение (если откат на умолчание)."""
    raw = (folder or "").strip()
    if not raw:
        path = DEFAULT_OUT
        warning = ""
    else:
        try:
            path = Path(raw).expanduser()
            # Относительные пути — от папки приложения
            if not path.is_absolute():
                path = (BASE / path).resolve()
            else:
                path = path.resolve()
            warning = ""
        except (OSError, RuntimeError, ValueError):
            path = DEFAULT_OUT
            warning = (
                f"Путь «{raw}» кривой — сохраняю в папку по умолчанию «Скачанное»"
            )

    try:
        path.mkdir(parents=True, exist_ok=True)
        # Проверка, что можно писать
        probe = path / ".kachalka_write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            warning = (
                f"В «{path}» нельзя писать — сохраняю в папку по умолчанию «Скачанное»"
            )
            path = DEFAULT_OUT
            path.mkdir(parents=True, exist_ok=True)
    except OSError:
        warning = (
            f"Не удалось создать «{path}» — сохраняю в папку по умолчанию «Скачанное»"
        )
        path = DEFAULT_OUT
        path.mkdir(parents=True, exist_ok=True)

    return path, warning


def _progress_hook(d: dict[str, Any]) -> None:
    with _lock:
        if d.get("status") == "downloading":
            _state["status"] = "downloading"
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            if total:
                _state["percent"] = round(downloaded / total * 100, 1)
                _state["size"] = f"{_format_bytes(downloaded)} / {_format_bytes(total)}"
            else:
                _state["percent"] = 0.0
                _state["size"] = _format_bytes(downloaded) if downloaded else ""
            _state["speed"] = _format_speed(d.get("speed"))
            info = d.get("info_dict") or {}
            title = info.get("title")
            if title:
                _state["title"] = title
            filename = d.get("filename")
            if filename:
                _state["filename"] = Path(filename).name
        elif d.get("status") == "finished":
            _state["percent"] = 100.0
            _state["speed"] = ""
            filename = d.get("filename")
            if filename:
                _state["filename"] = Path(filename).name
            info = d.get("info_dict") or {}
            if info.get("title"):
                _state["title"] = info["title"]


def _normalize_mode(mode: str | None) -> str:
    m = (mode or MODE_VIDEO).strip().lower()
    if m in ("audio", "mp3", "sound", "music"):
        return MODE_AUDIO
    return MODE_VIDEO


def _normalize_quality(quality: str | None) -> str:
    q = (quality or QUALITY_BEST).strip().lower()
    if q in ("720", "720p", "hd"):
        return QUALITY_720
    if q in ("480", "480p", "sd", "light"):
        return QUALITY_480
    return QUALITY_BEST


def _ydl_opts(
    out_dir: Path,
    mode: str = MODE_VIDEO,
    quality: str = QUALITY_BEST,
) -> dict[str, Any]:
    """Опции yt-dlp: видео (mp4 + качество) или только звук (mp3)."""
    opts: dict[str, Any] = {
        "outtmpl": str(out_dir / "%(title).120s.%(ext)s"),
        "noplaylist": True,
        "progress_hooks": [_progress_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    # ffmpeg нужен и для склейки mp4, и для конвертации в mp3
    if (FFMPEG_BIN / "ffmpeg.exe").exists():
        opts["ffmpeg_location"] = str(FFMPEG_BIN)

    if mode == MODE_AUDIO:
        # Эквивалент CLI: -x --audio-format mp3
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        q = _normalize_quality(quality)
        opts["format"] = FORMAT_BY_QUALITY.get(q, FORMAT_BY_QUALITY[QUALITY_BEST])
        opts["merge_output_format"] = "mp4"

    return opts


def _final_path(ydl: yt_dlp.YoutubeDL, info: dict[str, Any], mode: str) -> Path:
    """Файл после скачивания/конвертации (учёт .mp4 / .mp3)."""
    prepared = Path(ydl.prepare_filename(info))
    candidates: list[Path] = [prepared]
    if mode == MODE_AUDIO:
        candidates.extend(
            [
                prepared.with_suffix(".mp3"),
                Path(str(prepared.with_suffix("")) + ".mp3"),
            ]
        )
        # yt-dlp иногда кладёт путь в requested_downloads
        for item in info.get("requested_downloads") or []:
            fp = item.get("filepath") or item.get("filename")
            if fp:
                candidates.append(Path(fp))
                candidates.append(Path(fp).with_suffix(".mp3"))
    else:
        candidates.append(prepared.with_suffix(".mp4"))

    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return prepared


def _do_download(
    url: str,
    folder: str | None,
    mode: str = MODE_VIDEO,
    quality: str = QUALITY_BEST,
) -> None:
    mode = _normalize_mode(mode)
    quality = _normalize_quality(quality)
    try:
        out_dir, warning = _resolve_out_dir(folder)
        if mode == MODE_AUDIO and not (FFMPEG_BIN / "ffmpeg.exe").exists():
            warning = (
                (warning + " ") if warning else ""
            ) + "ffmpeg не найден — mp3 может не получиться. Запусти install.bat."

        with _lock:
            _state.update(
                {
                    "status": "downloading",
                    "percent": 0.0,
                    "speed": "",
                    "size": "",
                    "title": "",
                    "filename": "",
                    "error": "",
                    "out_dir": str(out_dir),
                    "warning": warning,
                    "mode": mode,
                    "quality": quality,
                }
            )

        with yt_dlp.YoutubeDL(_ydl_opts(out_dir, mode, quality)) as ydl:
            info = ydl.extract_info(url, download=True)
            default_title = "Аудио" if mode == MODE_AUDIO else "Видео"
            title = (info or {}).get("title") or default_title
            path = _final_path(ydl, info or {}, mode)

            with _lock:
                _state["status"] = "done"
                _state["title"] = title
                _state["filename"] = path.name if path.exists() else path.name
                _state["percent"] = 100.0
                _state["speed"] = ""
                _state["out_dir"] = str(out_dir)
                _state["mode"] = mode
                _state["quality"] = quality
                if path.exists():
                    _state["size"] = _format_bytes(path.stat().st_size)
    except Exception as exc:  # noqa: BLE001 — показываем ошибку на странице, сервер жив
        with _lock:
            _state["status"] = "error"
            _state["error"] = _human_error(exc)
            _state["speed"] = ""
            _state["percent"] = 0.0


class DownloadRequest(BaseModel):
    url: str
    folder: str | None = None
    mode: str | None = MODE_VIDEO  # video | audio
    quality: str | None = QUALITY_BEST  # best | 720 | 480


class OpenFolderRequest(BaseModel):
    folder: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "default_folder": str(DEFAULT_OUT),
        "default_folder_label": "Скачанное",
    }


def _browse_folder_dialog(initial: str) -> dict[str, Any]:
    """Системный выбор папки через отдельный GUI-процесс.

    Важно: не использовать CREATE_NO_WINDOW — из‑за него диалог
    мелькает и сразу пропадает. tkinter/WinForms нужен «живой» desktop.
    Результат читаем из временного файла (надёжнее stdout).

    Сборка (frozen): тот же exe с флагом --pick-folder.
    Разработка: pythonw pick_folder.py.
    """
    if not initial or not Path(initial).is_dir():
        initial = str(DEFAULT_OUT) if DEFAULT_OUT.is_dir() else str(BASE)

    result_path = (
        Path(tempfile.gettempdir())
        / f"kachalka_folder_{os.getpid()}_{threading.get_ident()}.txt"
    )
    try:
        if result_path.exists():
            result_path.unlink()
    except OSError:
        pass

    if getattr(sys, "frozen", False):
        # Собранное приложение: отдельный процесс того же exe
        cmd = [sys.executable, "--pick-folder", initial, str(result_path)]
        cwd = str(BASE)
    else:
        picker = BASE / "pick_folder.py"
        if not picker.is_file():
            picker = RESOURCE / "pick_folder.py"
        if not picker.is_file():
            return {
                "ok": False,
                "error": "Не найден pick_folder.py — впиши путь вручную.",
            }
        exe = sys.executable
        if sys.platform == "win32":
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            if pythonw.is_file():
                exe = str(pythonw)
        cmd = [exe, str(picker), initial, str(result_path)]
        cwd = str(BASE)

    try:
        # Без CREATE_NO_WINDOW и без capture_output — иначе GUI гаснет
        proc = subprocess.run(cmd, timeout=600, cwd=cwd)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Диалог выбора папки не ответил"}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"Не удалось открыть выбор папки: {exc}. Впиши путь вручную.",
        }

    # Иногда файл дописывается чуть позже закрытия процесса
    for _ in range(10):
        if result_path.is_file():
            break
        time.sleep(0.05)

    if not result_path.is_file():
        return {
            "ok": False,
            "error": (
                "Диалог не вернул путь. Впиши папку вручную "
                f"(код процесса: {proc.returncode})."
            ),
        }

    try:
        chosen = result_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return {"ok": False, "error": f"Не прочитать результат: {exc}"}
    finally:
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass

    if chosen:
        return {"ok": True, "folder": chosen}
    return {"ok": False, "cancelled": True}


class BrowseFolderRequest(BaseModel):
    folder: str | None = None


@app.post("/api/browse-folder")
def browse_folder(req: BrowseFolderRequest | None = None) -> dict[str, Any]:
    """Открыть системный диалог выбора папки (Windows)."""
    initial = (req.folder if req else None) or None
    if not initial:
        with _lock:
            initial = str(_state.get("out_dir") or DEFAULT_OUT)
    return _browse_folder_dialog(str(initial))


@app.post("/api/download")
def start_download(req: DownloadRequest) -> dict[str, Any]:
    url = (req.url or "").strip()
    if not url.startswith("http"):
        return {
            "ok": False,
            "error": "Это не похоже на ссылку. Вставь адрес вида https://…",
        }

    with _lock:
        if _state["status"] == "downloading":
            return {
                "ok": False,
                "error": "Уже идёт скачивание — дождись окончания",
            }

    mode = _normalize_mode(req.mode)
    quality = _normalize_quality(req.quality)
    # Предпросмотр папки (создастся ещё раз в потоке скачивания)
    out_dir, warning = _resolve_out_dir(req.folder)
    if mode == MODE_AUDIO and not (FFMPEG_BIN / "ffmpeg.exe").exists():
        extra = "ffmpeg не найден — mp3 может не получиться. Запусти install.bat."
        warning = f"{warning} {extra}".strip() if warning else extra

    thread = threading.Thread(
        target=_do_download,
        args=(url, req.folder, mode, quality),
        daemon=True,
    )
    thread.start()
    return {
        "ok": True,
        "out_dir": str(out_dir),
        "warning": warning,
        "mode": mode,
        "quality": quality,
    }


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


@app.post("/api/reset")
def reset_status() -> dict[str, Any]:
    with _lock:
        if _state["status"] == "downloading":
            return {"ok": False, "error": "Скачивание ещё идёт"}
        out_dir = _state.get("out_dir") or str(DEFAULT_OUT)
        _state.update(
            {
                "status": "idle",
                "percent": 0.0,
                "speed": "",
                "size": "",
                "title": "",
                "filename": "",
                "error": "",
                "out_dir": out_dir,
                "warning": "",
            }
        )
    return {"ok": True}


@app.post("/api/open-folder")
def open_folder(req: OpenFolderRequest | None = None) -> dict[str, Any]:
    try:
        folder = (req.folder if req else None) or None
        with _lock:
            if not folder:
                folder = _state.get("out_dir") or str(DEFAULT_OUT)
        path, _warning = _resolve_out_dir(folder)
        os.startfile(str(path))  # Windows: открыть в Проводнике
        return {"ok": True, "folder": str(path)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Не удалось открыть папку: {exc}"}


@app.get("/api/history")
def history(
    folder: str | None = Query(default=None),
) -> dict[str, Any]:
    path, _warning = _resolve_out_dir(folder)
    files: list[dict[str, Any]] = []
    try:
        for p in path.iterdir():
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": p.name,
                    "size": _format_bytes(st.st_size),
                    "mtime": st.st_mtime,
                }
            )
    except OSError:
        pass
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": files[:30], "folder": str(path)}


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8031, reload=False)
