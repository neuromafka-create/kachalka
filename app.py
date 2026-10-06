# Файл: app.py
# «Качалка» — локальное веб-приложение: ссылка → видео.
# Тот же формат и ffmpeg, что в kachalka.py; прогресс через progress hooks.
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yt_dlp
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

import page_videos
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


def _configure_ssl_certs() -> None:
    """Подключить Mozilla CA (certifi), иначе на Windows OpenSSL часто
    не находит issuer для vk.com и падает с CERTIFICATE_VERIFY_FAILED.
    """
    try:
        import certifi
        import ssl
    except ImportError:
        return

    ca = certifi.where()
    if not Path(ca).is_file():
        return

    # yt-dlp / urllib / OpenSSL читают эти переменные
    os.environ.setdefault("SSL_CERT_FILE", ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
    os.environ.setdefault("CURL_CA_BUNDLE", ca)

    # urllib в Python берёт default context один раз — зафиксируем cafile явно
    def _https_context() -> ssl.SSLContext:
        return ssl.create_default_context(cafile=ca)

    ssl._create_default_https_context = _https_context  # type: ignore[assignment]


_configure_ssl_certs()


# С июля 2026 VK рекомендует vk.ru вместо vk.com («быстрее и надёжнее»).
# Старые ссылки .com и vkvideo.ru по-прежнему принимаем.
VK_PREFERRED_HOST = "vk.ru"
VK_API_HOSTS = ("vk.ru", "vk.com")  # порядок: сначала .ru, запасной .com


def _patch_yt_dlp_vk_prefer_ru() -> None:
    """yt-dlp ходит в al_video.php на vk.com — переключаем на vk.ru с fallback."""
    try:
        from yt_dlp.extractor.vk import VKBaseIE
        from yt_dlp.utils import ExtractorError, clean_html, urlencode_postdata
    except ImportError:
        return
    if getattr(VKBaseIE, "_kachalka_vk_ru", False):
        return

    def _download_payload(self, path, video_id, data, fatal=True):  # noqa: ANN001
        payload_data = dict(data)
        payload_data["al"] = 1
        last_err: BaseException | None = None
        for i, host in enumerate(VK_API_HOSTS):
            endpoint = f"https://{host}/{path}.php"
            is_last = i == len(VK_API_HOSTS) - 1
            try:
                resp = self._download_json(
                    endpoint,
                    video_id,
                    data=urlencode_postdata(payload_data),
                    fatal=fatal if is_last else False,
                    headers={
                        "Referer": endpoint,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                if not resp or "payload" not in resp:
                    continue
                code, payload = resp["payload"]
                if code == "3":
                    self.raise_login_required()
                elif code == "8":
                    raise ExtractorError(
                        clean_html(payload[0][1:-1]), expected=True
                    )
                return payload
            except ExtractorError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        if fatal and last_err is not None:
            raise last_err
        if fatal:
            raise ExtractorError("Unable to download VK JSON metadata")
        return None

    VKBaseIE._download_payload = _download_payload  # type: ignore[method-assign]
    VKBaseIE._kachalka_vk_ru = True


_patch_yt_dlp_vk_prefer_ru()

# Плеер GetCourse в yt-dlp 2026.07.04 узнаётся только на player02.getcourse.ru
# и cf-api-2.vhcdn.com. Школы на своём домене отдают vh-api-*.gceuproxy.com.
_GETCOURSE_PLAYER_URL_RE = (
    r"https?://[^/?#\s]+/sign-player/?(?:\?|/)(?:[^#\s]*&)?json=[^#&\s]+"
)


def _patch_yt_dlp_getcourse_player() -> None:
    """Расширить адрес плеера GetCourse, не трогая сам разбор плейлиста."""
    try:
        from yt_dlp.extractor.getcourseru import GetCourseRuPlayerIE
        from yt_dlp.extractor.lazy_extractors import (
            GetCourseRuPlayerIE as LazyGetCourseRuPlayerIE,
        )
    except ImportError:
        return
    if getattr(GetCourseRuPlayerIE, "_kachalka_broad_player", False):
        return
    for cls in (GetCourseRuPlayerIE, LazyGetCourseRuPlayerIE):
        cls._VALID_URL = _GETCOURSE_PLAYER_URL_RE
        if "_VALID_URL_RE" in cls.__dict__:
            delattr(cls, "_VALID_URL_RE")
    GetCourseRuPlayerIE._EMBED_REGEX = [
        rf'<iframe[^>]+\bsrc=[\'"](?P<url>{_GETCOURSE_PLAYER_URL_RE}[^\'"]*)'
    ]
    GetCourseRuPlayerIE._kachalka_broad_player = True


_patch_yt_dlp_getcourse_player()


def _yandex_user_data_dir() -> str:
    """Профиль Яндекс.Браузера: обычное дерево Chromium, ключ DPAPI пользователя."""
    local = os.environ.get("LOCALAPPDATA") or ""
    return os.path.join(local, "Yandex", "YandexBrowser", "User Data")


def _patch_yt_dlp_browser_cookies() -> None:
    """Яндекс.Браузер в cookies-from-browser и пропуск cookies с префиксом v20.

    v20 — это app-bound шифрование Chrome/Edge: другая программа его не читает.
    Одна такая запись не должна обрывать весь список. Сам v20 не расшифровываем.
    """
    try:
        import yt_dlp.cookies as cookies
        from yt_dlp.utils import DownloadError
    except ImportError:
        return
    if getattr(cookies, "_kachalka_browser_cookies", False):
        return

    cookies.CHROMIUM_BASED_BROWSERS.add("yandex")
    cookies.SUPPORTED_BROWSERS.add("yandex")

    orig_settings = cookies._get_chromium_based_browser_settings

    def _settings(browser_name: str):
        if browser_name == "yandex" and sys.platform in ("win32", "cygwin"):
            return {
                "browser_dir": _yandex_user_data_dir(),
                "keyring_name": "Yandex",
                "supports_profiles": True,
            }
        return orig_settings(browser_name)

    cookies._get_chromium_based_browser_settings = _settings

    orig_decrypt = cookies.WindowsChromeCookieDecryptor.decrypt

    def _decrypt(self, encrypted_value):  # noqa: ANN001
        prefix = encrypted_value[:3] if encrypted_value else b""
        if prefix == b"v20":
            self._cookie_counts["v20"] = self._cookie_counts.get("v20", 0) + 1
            cookies._kachalka_skipped_v20 = getattr(cookies, "_kachalka_skipped_v20", 0) + 1
            return None
        return orig_decrypt(self, encrypted_value)

    cookies.WindowsChromeCookieDecryptor.decrypt = _decrypt

    orig_extract = cookies.extract_cookies_from_browser

    def _extract(
        browser_name,
        profile=None,
        logger=cookies.YDLLogger(),
        *,
        keyring=None,
        container=None,
    ):
        cookies._kachalka_skipped_v20 = 0
        try:
            jar = orig_extract(
                browser_name,
                profile,
                logger,
                keyring=keyring,
                container=container,
            )
        except DownloadError as exc:
            low = str(exc).lower()
            if "could not copy" in low and "cookie" in low:
                raise DownloadError(
                    f"could not copy {browser_name} cookie database"
                ) from exc
            raise
        if cookies._kachalka_skipped_v20 and not any(True for _ in jar):
            raise DownloadError(
                f"{browser_name} cookies are app-bound (v20) and were not shared"
            )
        return jar

    cookies.extract_cookies_from_browser = _extract
    cookies._kachalka_browser_cookies = True


_patch_yt_dlp_browser_cookies()


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

# Форматы видео: progressive mp4 (VK url720/url1080) + DASH/HLS fallback
QUALITY_BEST = "best"
QUALITY_720 = "720"
QUALITY_480 = "480"
FORMAT_BY_QUALITY: dict[str, str] = {
    # Лучшее: progressive mp4, иначе склейка DASH/HLS, иначе что угодно
    QUALITY_BEST: (
        "b[ext=mp4]/"
        "bv*[ext=mp4]+ba[ext=m4a]/"
        "bv*+ba/"
        "b"
    ),
    # Не выше 720p
    QUALITY_720: (
        "b[height<=720][ext=mp4]/"
        "bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
        "bv*[height<=720]+ba/"
        "b[height<=720]/"
        "b"
    ),
    # Не выше 480p — совсем лёгкое
    QUALITY_480: (
        "b[height<=480][ext=mp4]/"
        "bv*[height<=480][ext=mp4]+ba[ext=m4a]/"
        "bv*[height<=480]+ba/"
        "b[height<=480]/"
        "b"
    ),
}
MODE_VIDEO = "video"
MODE_AUDIO = "audio"

# Браузеры для cookies-from-browser (yt-dlp). Яндекс — первый: его cookies
# читаются ключом пользователя. Chrome и Edge отдают только v20.
BROWSER_CHOICES = ("yandex", "chrome", "edge", "firefox", "opera", "brave", "chromium")
BROWSER_TITLES = {
    "yandex": "Яндекс.Браузер",
    "chrome": "Chrome",
    "edge": "Edge",
    "firefox": "Firefox",
    "opera": "Opera",
    "brave": "Brave",
    "chromium": "Chromium",
}
# Автоповтор закрытого ВК. Chrome и Edge здесь бесполезны: все их cookies — v20.
VK_COOKIE_RETRY = ("yandex", "firefox", "opera", "brave")
# Имена файла cookies (Netscape) рядом с exe / в «Скачанное»
COOKIES_FILE_NAMES = (
    "cookies.txt",
    "vk_cookies.txt",
    "youtube_cookies.txt",
)

# Реалистичный UA — VK иногда режет «голый» Python
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# id вида -123_456 или 123_456
_VK_VIDEO_ID_RE = re.compile(
    r"(?:video|clip)(-?\d+_\d+)|z=video(-?\d+_\d+)",
    re.IGNORECASE,
)
# vk.com, vk.ru, m.vk.*, new.vk.*, vkvideo.ru, vksport.vkvideo.ru, …
_VK_HOST_RE = re.compile(
    r"(?:^|\.)(?:vk\.(?:com|ru)|vkvideo\.ru)$",
    re.IGNORECASE,
)

app = FastAPI(title="Качалка")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"(chrome-extension|extension)://.+",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

_lock = threading.Lock()
_pending: list[dict[str, Any]] = []
_MAX_BATCH = 12
_MAX_QUEUE = 24
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
    "queued": 0,
    "queue_note": "",
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


def _is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    return (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host.endswith("youtube-nocookie.com")
    )


def _youtube_cdn_blocked(exc: BaseException) -> bool:
    """YouTube описал ролик, но CDN не отдал сам файл (часто у открытых видео)."""
    low = str(exc).lower()
    return "http error 403" in low or "403: forbidden" in low


def _is_vk_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    # vk.com / vk.ru / m.vk.ru / new.vk.com / vkvideo.ru / vksport.vkvideo.ru
    return bool(_VK_HOST_RE.search(host))


def _normalize_url(url: str) -> str:
    """Почистить вставку из буфера: пробелы, кавычки, текст вокруг, //vk…, z=video.
    Канон для ВК — vk.ru (с июля 2026 основной домен; .com ещё жив как зеркало).
    """
    s = (url or "").strip().strip("\"'<>")
    if not s:
        return s

    # Вытащить первый URL, если скопировали с подписью
    m = re.search(r"https?://[^\s<>\"']+", s)
    if m:
        s = m.group(0)
    else:
        m = re.search(
            r"(?:(?:m|new|vksport)\.)?vk(?:video)?\.(?:com|ru)/[^\s<>\"']+",
            s,
            re.IGNORECASE,
        )
        if m:
            s = "https://" + m.group(0).lstrip("/")

    s = s.rstrip(").,;]'\"")

    # Ссылка без схемы
    if s.startswith("//"):
        s = "https:" + s
    elif re.match(r"^(?:[\w-]+\.)?(?:vk|vkvideo)\.(?:com|ru)/", s, re.I):
        s = "https://" + s

    # Старые .com → .ru (тот же путь; видео/клипы/wall)
    try:
        parsed = urlparse(s)
        host = (parsed.hostname or "").lower()
    except ValueError:
        parsed = None
        host = ""
    if host in ("vk.com", "m.vk.com", "new.vk.com", "www.vk.com"):
        new_host = host.replace("vk.com", VK_PREFERRED_HOST)
        s = parsed._replace(netloc=new_host).geturl()  # type: ignore[union-attr]

    # VK: z=video-123_456 / clip… → канонический video-id на vk.ru
    if _is_vk_url(s):
        vid = None
        found = _VK_VIDEO_ID_RE.search(s)
        if found:
            vid = found.group(1) or found.group(2)
        if not vid:
            qs = parse_qs(urlparse(s).query)
            z = unquote((qs.get("z") or [""])[0])
            zm = re.search(r"video(-?\d+_\d+)", z, re.I)
            if zm:
                vid = zm.group(1)
        if vid:
            # list= полезен для части удалённых/плейлистных роликов
            list_id = None
            lm = re.search(r"[?&]list=([^&]+)", s)
            if lm:
                list_id = unquote(lm.group(1))
            base = f"https://{VK_PREFERRED_HOST}/video{vid}"
            if list_id:
                base += f"?list={list_id}"
            return base

    return s


def _find_cookies_file() -> Path | None:
    """cookies.txt рядом с программой или в папке «Скачанное»."""
    dirs = (BASE, DEFAULT_OUT)
    for d in dirs:
        for name in COOKIES_FILE_NAMES:
            p = d / name
            if p.is_file() and p.stat().st_size > 0:
                return p
    return None


def _normalize_browser(browser: str | None) -> str | None:
    if not browser:
        return None
    b = browser.strip().lower()
    if b in ("", "none", "off", "0", "false", "no"):
        return None
    if b in BROWSER_CHOICES:
        return b
    return None


def _browser_title(browser: str | None) -> str:
    if not browser:
        return ""
    return BROWSER_TITLES.get(browser, browser)


def _exc_search_text(exc: BaseException) -> str:
    """Текст исключения вместе с причиной: yt-dlp прячет её в __context__."""
    parts = [str(exc)]
    seen = {id(exc)}
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        nxt = current.__cause__ or current.__context__
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        parts.append(str(nxt))
        current = nxt
    return "\n".join(parts)


def _human_error(exc: BaseException, *, url: str = "") -> str:
    msg = str(exc).strip()
    low = _exc_search_text(exc).lower()
    vk = (
        _is_vk_url(url)
        or "[vk]" in low
        or "vk.com" in low
        or "vk.ru" in low
        or "vkvideo" in low
    )

    if "unsupported url" in low or "no suitable extractor" in low:
        return "Ссылка не открылась — такой сайт не поддерживается"
    if "could not copy" in low and "cookie" in low:
        if "could not copy yandex cookie" in low:
            return (
                "Яндекс.Браузер держит файл cookies. "
                "Закрой его полностью, в том числе значок в трее, и попробуй снова."
            )
        return (
            "Браузер держит файл cookies. "
            "Закрой его полностью, в том числе значок в трее, и попробуй снова."
        )
    if "app-bound" in low or "(v20)" in low:
        if "yandex cookies are app-bound" in low:
            return (
                "Яндекс.Браузер не отдал cookies этой программе. "
                "Положи cookies.txt рядом с Качалкой "
                "(экспорт расширением «Get cookies.txt LOCALLY»)."
            )
        return (
            "Этот браузер не отдаёт cookies другим программам. "
            "Войди в ВК в Яндекс.Браузере, закрой его полностью "
            "и выбери «Войти через Яндекс.Браузер». "
            "Либо положи cookies.txt рядом с Качалкой "
            "(экспорт расширением «Get cookies.txt LOCALLY»)."
        )
    if "failed to decrypt with dpapi" in low:
        return (
            "Не удалось прочитать cookies браузера. "
            "Закрой его полностью, в том числе значок в трее, и попробуй снова, "
            "либо положи cookies.txt рядом с Качалкой "
            "(экспорт расширением «Get cookies.txt LOCALLY»)."
        )
    if "could not find" in low and "cookie" in low:
        if "yandex cookies database" in low:
            return (
                "Не нашла cookies Яндекс.Браузера. "
                "Проверь, что он установлен и что ты в нём входила в ВК."
            )
        return (
            "Не нашла cookies выбранного браузера. "
            "Проверь, что он установлен и что ты в нём входила на сайт."
        )
    if (
        "private video" in low
        or "sign in to confirm" in low
        or "login required" in low
        or "only available for registered" in low
        or "access restricted" in low
        or "access denied" in low
    ):
        if vk:
            return (
                "ВК: видео закрыто или нужен вход. "
                "Выбери «Войти через Яндекс.Браузер» и закрой его перед скачиванием, "
                "или положи cookies.txt рядом с Качалкой"
            )
        return "Видео недоступно — оно закрыто, ограничено по региону или требует входа"
    if "not available in your region" in low or "недоступно в вашем регионе" in low:
        return "Видео недоступно в твоём регионе"
    if (
        "video unavailable" in low
        or "not available" in low
        or "has been removed" in low
        or "is not available" in low
        or "was removed" in low
        or "author has been blocked" in low
    ):
        return "Видео недоступно — удалено или автор заблокирован"
    if "http error 404" in low or "unable to download webpage" in low:
        return "Ссылка не открылась — страница не найдена"
    if "http error 403" in low or "403: forbidden" in low:
        if _is_youtube_url(url) or "[youtube]" in low:
            return (
                "YouTube не отдал файл. Ролик может быть открытым — "
                "так сайт режет скачивание (ошибка 403). Попробуй ещё раз чуть позже"
            )
        return "Видео недоступно — доступ запрещён"
    if (
        "certificate_verify_failed" in low
        or "certificate verify failed" in low
        or "ssl: certificate" in low
        or "unable to get local issuer certificate" in low
    ):
        return (
            "Не удалось проверить защищённое соединение (SSL). "
            "Часто помогает: перезапусти install.bat или отключи "
            "«проверку HTTPS» в антивирусе"
        )
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
    *,
    cookies_file: Path | None = None,
    cookies_browser: str | None = None,
    player_clients: list[str] | None = None,
) -> dict[str, Any]:
    """Опции yt-dlp: видео (mp4 + качество) или только звук (mp3)."""
    opts: dict[str, Any] = {
        "outtmpl": str(out_dir / "%(title).120s.%(ext)s"),
        "noplaylist": True,
        "progress_hooks": [_progress_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "http_headers": dict(_HTTP_HEADERS),
        # Чуть устойчивее к обрывам (VK/CDN)
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
    }
    # ffmpeg нужен и для склейки mp4, и для конвертации в mp3
    if (FFMPEG_BIN / "ffmpeg.exe").exists():
        opts["ffmpeg_location"] = str(FFMPEG_BIN)

    # Cookies: файл важнее (явный экспорт), иначе браузер
    if cookies_file is not None and cookies_file.is_file():
        opts["cookiefile"] = str(cookies_file)
    elif cookies_browser:
        # yt-dlp: cookiesfrombrowser = (browser, profile, keyring, container)
        opts["cookiesfrombrowser"] = (cookies_browser,)

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

    if player_clients:
        # Значения extractor_args — всегда списки строк.
        # continuedl выключен: оборванный первый файл нельзя докачивать другим клиентом.
        opts["extractor_args"] = {"youtube": {"player_client": list(player_clients)}}
        opts["continuedl"] = False

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


def _cookie_failure_rank(exc: BaseException) -> int:
    """Какую ошибку cookies показать, если перебор браузеров не удался.

    Закрытый файл Яндекс.Браузера важнее, чем «браузер не установлен».
    """
    low = _exc_search_text(exc).lower()
    if "could not copy" in low and "cookie" in low:
        return 3
    if "app-bound" in low or "failed to decrypt with dpapi" in low:
        return 2
    if "could not find" in low and "cookie" in low:
        return 0
    return 1


def _needs_auth_retry(exc: BaseException) -> bool:
    low = str(exc).lower()
    return any(
        k in low
        for k in (
            "access restricted",
            "access denied",
            "login required",
            "only available for registered",
            "private video",
            "sign in",
        )
    )


def _do_download(
    url: str,
    folder: str | None,
    mode: str = MODE_VIDEO,
    quality: str = QUALITY_BEST,
    cookies_browser: str | None = None,
) -> None:
    mode = _normalize_mode(mode)
    quality = _normalize_quality(quality)
    url = _normalize_url(url)
    browser = _normalize_browser(cookies_browser)
    cookies_file = _find_cookies_file()

    try:
        out_dir, warning = _resolve_out_dir(folder)
        if mode == MODE_AUDIO and not (FFMPEG_BIN / "ffmpeg.exe").exists():
            warning = (
                (warning + " ") if warning else ""
            ) + "ffmpeg не найден — mp3 может не получиться. Запусти install.bat."
        if cookies_file is not None:
            hint = f"Cookies: {cookies_file.name}"
            warning = f"{warning} {hint}".strip() if warning else hint
        elif browser:
            hint = f"Cookies из браузера «{_browser_title(browser)}»"
            warning = f"{warning} {hint}".strip() if warning else hint

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

        def _run(
            *,
            use_file: Path | None,
            use_browser: str | None,
            player_clients: list[str] | None = None,
        ) -> tuple[Any, Path]:
            opts = _ydl_opts(
                out_dir,
                mode,
                quality,
                cookies_file=use_file,
                cookies_browser=use_browser,
                player_clients=player_clients,
            )
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = _final_path(ydl, info or {}, mode)
                return info, path

        try:
            info, path = _run(use_file=cookies_file, use_browser=browser)
        except Exception as first_exc:  # noqa: BLE001
            # YouTube: страница открытая, а файл по обычной ссылке CDN режет 403.
            # Клиент android отдаёт цельный mp4 (часто 360p) без этого отказа.
            if _is_youtube_url(url) and _youtube_cdn_blocked(first_exc):
                with _lock:
                    _state["warning"] = (
                        (warning + " " if warning else "")
                        + "YouTube не отдал полное качество, беру запасной файл…"
                    ).strip()
                info, path = _run(
                    use_file=cookies_file,
                    use_browser=browser,
                    player_clients=["android"],
                )
                height = int((info or {}).get("height") or 0)
                note = (
                    f"Файл скачан в {height}p: более высокое качество YouTube сейчас не отдаёт."
                    if height
                    else "Файл скачан в запасном качестве: более высокое YouTube сейчас не отдаёт."
                )
                warning = f"{warning} {note}".strip() if warning else note
                with _lock:
                    _state["warning"] = warning
            # ВК: закрытое видео — авто-повтор с cookies. Сначала Яндекс.Браузер.
            elif (
                _is_vk_url(url)
                and _needs_auth_retry(first_exc)
                and cookies_file is None
                and browser is None
            ):
                best_exc: BaseException = first_exc
                best_rank = _cookie_failure_rank(first_exc)
                for b in VK_COOKIE_RETRY:
                    try:
                        with _lock:
                            _state["warning"] = (
                                (warning + " " if warning else "")
                                + f"Пробую браузер «{_browser_title(b)}»…"
                            ).strip()
                        info, path = _run(use_file=None, use_browser=b)
                        best_exc = None  # type: ignore[assignment]
                        with _lock:
                            _state["warning"] = (
                                (warning + " " if warning else "")
                                + f"Вошёл через «{_browser_title(b)}»"
                            ).strip()
                        break
                    except Exception as retry_exc:  # noqa: BLE001
                        rank = _cookie_failure_rank(retry_exc)
                        if rank >= best_rank:
                            best_exc = retry_exc
                            best_rank = rank
                        continue
                if best_exc is not None:
                    if best_exc is first_exc:
                        raise best_exc
                    raise best_exc from first_exc
            else:
                raise

        default_title = "Аудио" if mode == MODE_AUDIO else "Видео"
        title = (info or {}).get("title") or default_title

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
            _state["queued"] = len(_pending)
    except Exception as exc:  # noqa: BLE001 — показываем ошибку на странице, сервер жив
        with _lock:
            _state["status"] = "error"
            _state["error"] = _human_error(exc, url=url)
            _state["speed"] = ""
            _state["percent"] = 0.0
            _state["queued"] = len(_pending)
            if _pending:
                _state["queue_note"] = _state["error"]
    finally:
        _pump_queue()


def _pump_queue() -> None:
    """Запустить следующий ролик из очереди, если сейчас никто не качается."""
    with _lock:
        if _state["status"] == "downloading" or not _pending:
            _state["queued"] = len(_pending)
            return
        item = _pending.pop(0)
        _state["queued"] = len(_pending)
        _state["status"] = "downloading"
        _state["percent"] = 0.0
        _state["speed"] = ""
        _state["size"] = ""
        _state["error"] = ""
        _state["title"] = ""
        _state["filename"] = ""
    threading.Thread(
        target=_do_download,
        args=(
            item["url"],
            item["folder"],
            item["mode"],
            item["quality"],
            item["cookies_browser"],
        ),
        daemon=True,
    ).start()


def _enqueue_downloads(
    urls: list[str],
    folder: str | None,
    mode: str,
    quality: str,
    cookies_browser: str | None,
) -> dict[str, Any]:
    """Поставить ролики в очередь. Первый стартует сразу, если загрузка свободна."""
    clean: list[str] = []
    seen: set[str] = set()
    with _lock:
        already = {str(item.get("url") or "") for item in _pending}
    for raw in list(urls)[:_MAX_BATCH]:
        url = _normalize_url(str(raw or ""))
        if not url.startswith("http") or url in seen or url in already:
            continue
        seen.add(url)
        clean.append(url)
    if not clean:
        return {"ok": False, "error": "Не выбрано ни одного ролика"}
    with _lock:
        room = _MAX_QUEUE - len(_pending)
        if room <= 0:
            return {
                "ok": False,
                "error": "Очередь занята — дождись, пока скачается текущее",
            }
        accepted = clean[:room]
        if _state["status"] != "downloading":
            _state["queue_note"] = ""
        for url in accepted:
            _pending.append(
                {
                    "url": url,
                    "folder": folder,
                    "mode": mode,
                    "quality": quality,
                    "cookies_browser": cookies_browser,
                }
            )
        _state["queued"] = len(_pending)
    _pump_queue()
    with _lock:
        return {
            "ok": True,
            "accepted": len(accepted),
            "queued": int(_state.get("queued") or 0),
        }


class DownloadRequest(BaseModel):
    url: str
    folder: str | None = None
    mode: str | None = MODE_VIDEO  # video | audio
    quality: str | None = QUALITY_BEST  # best | 720 | 480
    # none | chrome | edge | firefox | opera | brave | chromium
    cookies_browser: str | None = None


class InspectRequest(BaseModel):
    url: str


class DownloadManyRequest(BaseModel):
    urls: list[str]
    folder: str | None = None
    mode: str | None = MODE_VIDEO
    quality: str | None = QUALITY_BEST
    cookies_browser: str | None = None


def _present_found(videos: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Повторно прогнать найденные адреса через нормализацию ВК и убрать дубли."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in videos:
        url = _normalize_url(str(item.get("url") or ""))
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        title = str(item.get("title") or "").strip() or "Видео"
        out.append(
            {
                "url": url,
                "title": title[:140],
                "kind": str(item.get("kind") or ""),
            }
        )
        if len(out) >= 12:
            break
    return out


class OpenFolderRequest(BaseModel):
    folder: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    cookies = _find_cookies_file()
    return {
        "default_folder": str(DEFAULT_OUT),
        "default_folder_label": "Скачанное",
        "cookies_file": str(cookies) if cookies else None,
        "cookies_file_name": cookies.name if cookies else None,
        "browsers": list(BROWSER_CHOICES),
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


@app.post("/api/inspect")
def inspect_link(req: InspectRequest) -> dict[str, Any]:
    """Найти ролики, встроенные в обычную страницу.

    Прямую ссылку на ролик возвращает как mode=direct — её качаем как раньше.
    """
    url = _normalize_url(req.url or "")
    if not url.startswith("http"):
        return {
            "ok": False,
            "error": "Это не похоже на ссылку. Вставь адрес вида https://…",
        }
    found = page_videos.inspect_page(url)
    videos = _present_found(list(found.get("videos") or []))
    mode = str(found.get("mode") or "direct")
    if mode == "embeds" and not videos:
        mode = "direct"
    return {
        "ok": True,
        "mode": mode,
        "videos": videos,
        "scanned": bool(found.get("scanned")),
        "page_url": url,
    }


@app.post("/api/download")
def start_download(req: DownloadRequest) -> dict[str, Any]:
    url = _normalize_url(req.url or "")
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
    cookies_browser = _normalize_browser(req.cookies_browser)
    cookies_file = _find_cookies_file()
    # Предпросмотр папки (создастся ещё раз в потоке скачивания)
    out_dir, warning = _resolve_out_dir(req.folder)
    if mode == MODE_AUDIO and not (FFMPEG_BIN / "ffmpeg.exe").exists():
        extra = "ffmpeg не найден — mp3 может не получиться. Запусти install.bat."
        warning = f"{warning} {extra}".strip() if warning else extra
    if cookies_file is not None:
        extra = f"Найден {cookies_file.name} — использую для входа"
        warning = f"{warning} {extra}".strip() if warning else extra
    elif cookies_browser:
        extra = f"Буду брать cookies из браузера «{_browser_title(cookies_browser)}»"
        warning = f"{warning} {extra}".strip() if warning else extra

    # Статус до запуска потока: иначе первый опрос ещё видит idle
    # и окно прячет прогресс, будто кнопка ничего не сделала.
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
                "queue_note": "",
            }
        )
    thread = threading.Thread(
        target=_do_download,
        args=(url, req.folder, mode, quality, cookies_browser),
        daemon=True,
    )
    thread.start()
    return {
        "ok": True,
        "out_dir": str(out_dir),
        "warning": warning,
        "mode": mode,
        "quality": quality,
        "url": url,
        "cookies_browser": cookies_browser,
        "cookies_file": str(cookies_file) if cookies_file else None,
    }


@app.post("/api/download-many")
def download_many(req: DownloadManyRequest) -> dict[str, Any]:
    """Очередь из расширения: несколько роликов качаются друг за другом."""
    mode = _normalize_mode(req.mode)
    quality = _normalize_quality(req.quality)
    cookies_browser = _normalize_browser(req.cookies_browser)
    folder = (req.folder or "").strip() or None
    if folder is None:
        with _lock:
            folder = str(_state.get("out_dir") or "") or None
    return _enqueue_downloads(
        list(req.urls or []),
        folder,
        mode,
        quality,
        cookies_browser,
    )


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


@app.post("/api/reset")
def reset_status() -> dict[str, Any]:
    with _lock:
        if _state["status"] == "downloading":
            return {"ok": False, "error": "Скачивание ещё идёт"}
        _pending.clear()
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
                "queued": 0,
                "queue_note": "",
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
