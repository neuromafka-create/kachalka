# Файл: app.py
# «Качалка» — локальное веб-приложение: ссылка → видео.
# Тот же формат и ffmpeg, что в kachalka.py; прогресс через progress hooks.
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yt_dlp
from yt_dlp.downloader.http import HttpFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import ContentTooShortError, DownloadError, parse_http_range
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


# Номер в окне, в установщике и в README должен совпадать с этой строкой.
APP_VERSION = "1.3.9"
_UPDATE_REPO = "neuromafka-create/kachalka"
_UPDATE_API = f"https://api.github.com/repos/{_UPDATE_REPO}/releases/latest"
_RELEASE_PREFIX = f"/{_UPDATE_REPO}/releases"

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

# Kinescope нет в yt-dlp 2026.07.04. Школы вставляют embed на свою страницу.
_KINESCOPE_URL_RE = (
    r"https?://(?:www\.)?kinescope\.io/(?:embed/)?"
    r"(?P<id>[0-9A-Za-z]{10,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?(?:[?#]|$)"
)


def _patch_yt_dlp_kinescope() -> None:
    """Узнать embed Kinescope и взять его открытый плейлист, не качая ролик заранее."""
    try:
        from yt_dlp.extractor.common import InfoExtractor
        from yt_dlp.extractor import import_extractors
        from yt_dlp.globals import extractors as extractors_ctx
        from yt_dlp.utils import ExtractorError
    except ImportError:
        return
    import_extractors()
    if "KinescopeIE" in extractors_ctx.value:
        return

    class KinescopeIE(InfoExtractor):
        IE_NAME = "kinescope"
        _VALID_URL = _KINESCOPE_URL_RE

        def _real_extract(self, url: str) -> dict[str, Any]:
            video_id = self._match_id(url)
            embed = f"https://kinescope.io/embed/{video_id}"
            webpage = self._download_webpage(
                embed,
                video_id,
                headers={"Referer": "https://kinescope.io/"},
            )
            found = re.search(
                r"var\s+playerOptions\s*=\s*(\{.*?\})\s*;\s*(?:var\s|function\s)",
                webpage,
                re.S,
            )
            if not found:
                raise ExtractorError("video unavailable", expected=True, video_id=video_id)
            try:
                options = json.loads(found.group(1))
            except json.JSONDecodeError as exc:
                raise ExtractorError("video unavailable", expected=True, video_id=video_id) from exc
            playlist = options.get("playlist") if isinstance(options, dict) else None
            item = playlist[0] if isinstance(playlist, list) and playlist else None
            if not isinstance(item, dict):
                raise ExtractorError("private video", expected=True, video_id=video_id)
            sources = item.get("sources") if isinstance(item.get("sources"), dict) else {}
            hls = sources.get("hls") if isinstance(sources.get("hls"), dict) else {}
            playlist_url = str(hls.get("src") or "").strip()
            if not playlist_url.startswith("https://"):
                raise ExtractorError("private video", expected=True, video_id=video_id)
            file_id = str(item.get("id") or video_id)
            formats = self._extract_m3u8_formats(
                playlist_url,
                file_id,
                "mp4",
                m3u8_id="hls",
                headers={"Referer": embed},
            )
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            duration = meta.get("duration")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                duration_value: int | None = None
            else:
                duration_value = int(duration)
            return {
                "id": file_id,
                "title": str(item.get("title") or video_id),
                "duration": duration_value,
                "thumbnail": item.get("posterInPreview") or None,
                "formats": formats,
            }

    current = extractors_ctx.value
    rebuilt: dict[str, Any] = {}
    for name, ie in current.items():
        if name == "GenericIE":
            rebuilt["KinescopeIE"] = KinescopeIE
        rebuilt[name] = ie
    if "KinescopeIE" not in rebuilt:
        rebuilt["KinescopeIE"] = KinescopeIE
    current.clear()
    current.update(rebuilt)


_patch_yt_dlp_kinescope()


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
    "chosen_name": "",
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


def _youtube_needs_fallback(exc: BaseException) -> bool:
    """Отказ, после которого имеет смысл взять цельный файл клиента android.

    YouTube меняет причину: то 403 посреди файла, то «подтверди, что не робот»,
    то поток без обычной ссылки. Ролик при этом может быть открытым.
    """
    low = _exc_search_text(exc).lower()
    # «Подтверди возраст» — ролик закрыт по возрасту, запасной клиент его не откроет.
    if "your age" in low:
        return False
    return any(
        piece in low
        for piece in (
            "http error 403",
            "403: forbidden",
            "sign in to confirm",
            "requested format is not available",
            "only images are available",
            "sabr",
            "nsig extraction failed",
            "no supported javascript runtime",
        )
    )


def _runtime_path(name: str) -> str | None:
    """Путь к движку JavaScript. Окно программы не всегда видит PATH консоли."""
    found = shutil.which(name)
    if found:
        return found
    if name == "node":
        roots = (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        )
        for root in roots:
            candidate = Path(root) / "nodejs" / "node.exe"
            if candidate.is_file():
                return str(candidate)
    if name == "bun":
        candidate = Path.home() / ".bun" / "bin" / "bun.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _js_runtimes() -> dict[str, dict[str, str]]:
    """Движки, которыми yt-dlp считает подпись YouTube. Берём только то, что есть в системе."""
    found: dict[str, dict[str, str]] = {}
    for name in ("deno", "node", "bun"):
        path = _runtime_path(name)
        if path:
            found[name] = {"path": path}
    return found


def _apply_js_solver(opts: dict[str, Any]) -> None:
    """Подпись YouTube считается движком с компьютера и официальным скриптом yt-dlp."""
    runtimes = _js_runtimes()
    if not runtimes:
        return
    opts["js_runtimes"] = runtimes
    opts["remote_components"] = ["ejs:github"]


# Мобильная страница отдаёт цельный mp4. Обычный клиент yt-dlp ссылку даёт, а файл режет 403.
# Встроенный плеер — запасной: на части роликов он открывается, когда мобильная страница не открылась.
_YOUTUBE_PLAYER_CLIENTS = ["mweb"]
_YOUTUBE_FALLBACK_CLIENTS = ["web_embedded"]


def _youtube_retry_plan(
    cookie_file: Path | None,
    saved: list[Path],
) -> list[tuple[Path | None, list[str]]]:
    """Повтор, если мобильная страница не отдала файл.

    Сначала сохранённый вход той же страницей, затем встроенный плеер.
    """
    plans: list[tuple[Path | None, list[str]]] = []
    if cookie_file is None:
        for jar in saved[:2]:
            plans.append((jar, list(_YOUTUBE_PLAYER_CLIENTS)))
        jars: list[Path | None] = list(saved[:2]) or [None]
    else:
        jars = [cookie_file]
    for jar in jars:
        plans.append((jar, list(_YOUTUBE_FALLBACK_CLIENTS)))
    return plans


# Одна ссылка googlevideo принимает первые закрытые куски, а следующий кусок той же
# ссылки отвечает 403. Свежая ссылка на тот же диапазон проходит. Открытый конец
# Range и неровное начало — нет, поэтому куски ровно по 10 МиБ, без укорочения.
_YOUTUBE_MEDIA_CHUNK = 10 << 20
_YOUTUBE_REFRESH_PAUSE = 1.0


def _is_googlevideo_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith(".googlevideo.com") or host == "googlevideo.com"


def _needs_fresh_googlevideo_url(info: dict[str, Any]) -> bool:
    """Цельный файл с googlevideo, не нарезка из фрагментов."""
    if info.get("fragments"):
        return False
    return _is_googlevideo_host(str(info.get("url") or ""))


def _youtube_closed_ranges(total: int, chunk: int) -> list[tuple[int, int]]:
    """Закрытые диапазоны [start, end], которые подряд покрывают total байт."""
    if total <= 0 or chunk <= 0:
        return []
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total:
        end = min(start + chunk, total) - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def _youtube_refresh_cap(total: int | None, chunk: int | None = None) -> int:
    """Сколько раз можно взять новую ссылку, пока ролик не собран."""
    step = chunk or _YOUTUBE_MEDIA_CHUNK
    if not total or total < 1 or step < 1:
        return 24
    pieces = (total + step - 1) // step
    return min(80, max(24, pieces + 4))


def _youtube_media_url(info: dict[str, Any] | None, format_id: str) -> str | None:
    """Ссылка того же формата после повторного разбора страницы."""
    if not info or not format_id:
        return None
    candidates: list[dict[str, Any]] = []
    if str(info.get("format_id") or "") == format_id:
        candidates.append(info)
    for fmt in list(info.get("formats") or []) + list(info.get("requested_formats") or []):
        if str(fmt.get("format_id") or "") == format_id:
            candidates.append(fmt)
    for fmt in candidates:
        url = str(fmt.get("url") or "")
        if _is_googlevideo_host(url):
            return url
    return None


def _response_status(resp: Any) -> int:
    raw = getattr(resp, "status", None)
    if raw is None:
        raw = getattr(resp, "code", None)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _response_header(headers: Any, name: str) -> str | None:
    if not headers:
        return None
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    return str(value)


class _StaleGooglevideo(Exception):
    """Текущая ссылка этот кусок уже не отдаёт."""

    def __init__(self, exc: BaseException | None = None):
        super().__init__(str(exc) if exc else "stale media url")
        self.exc = exc


class _GooglevideoFD(HttpFD):
    """Качает googlevideo закрытыми кусками и берёт новую ссылку, если кусок режут."""

    def real_download(self, filename, info_dict):
        url = str(info_dict.get("url") or "")
        if not _needs_fresh_googlevideo_url(info_dict):
            return super().real_download(filename, info_dict)

        total = int(info_dict.get("filesize") or info_dict.get("filesize_approx") or 0)
        headers = dict(info_dict.get("http_headers") or {})
        headers.pop("Range", None)
        headers.pop("range", None)
        headers["Accept-Encoding"] = "identity"

        part = self.temp_name(filename)
        if part != filename and os.path.isfile(part):
            os.remove(part)

        cap = _youtube_refresh_cap(total or None)
        chunk = _YOUTUBE_MEDIA_CHUNK
        downloaded = 0
        refreshes = 0
        started = time.time()
        stream, part = self.sanitize_open(part, "wb")
        try:
            while total <= 0 or downloaded < total:
                start = downloaded
                end = start + chunk - 1
                if total > 0:
                    end = min(end, total - 1)
                if end < start:
                    break
                try:
                    got, seen_total = self._read_closed_range(
                        url,
                        headers,
                        start,
                        end,
                        stream,
                        info_dict,
                        total,
                        started,
                        filename,
                    )
                except _StaleGooglevideo as stale:
                    stream.seek(start)
                    stream.truncate(start)
                    downloaded = start
                    self._report_media(start, total, started, filename, info_dict)
                    if start == 0 or refreshes >= cap:
                        self._raise_media_failure(stale)
                    new_url = self._fresh_media_url(info_dict)
                    refreshes += 1
                    if _YOUTUBE_REFRESH_PAUSE:
                        time.sleep(_YOUTUBE_REFRESH_PAUSE)
                    if not new_url:
                        self._raise_media_failure(stale)
                    url = new_url
                    continue
                if seen_total and seen_total > 0:
                    total = seen_total
                downloaded += got
                if got <= 0:
                    break
        finally:
            stream.close()

        if total > 0 and downloaded < total:
            raise ContentTooShortError(downloaded, total)
        if part != filename:
            self.try_rename(part, filename)
        self._hook_progress(
            {
                "status": "finished",
                "downloaded_bytes": downloaded,
                "total_bytes": downloaded,
                "filename": filename,
                "elapsed": time.time() - started,
            },
            info_dict,
        )
        return True

    def _raise_media_failure(self, stale: _StaleGooglevideo) -> None:
        if stale.exc is not None:
            raise stale.exc
        raise DownloadError("HTTP Error 403: Forbidden")

    def _fresh_media_url(self, info_dict: dict[str, Any]) -> str | None:
        webpage = info_dict.get("webpage_url") or info_dict.get("original_url")
        format_id = str(info_dict.get("format_id") or "")
        if not webpage or not format_id:
            return None
        fresh = self.ydl.extract_info(webpage, download=False)
        return _youtube_media_url(fresh, format_id)

    def _report_media(
        self,
        downloaded: int,
        total: int,
        started: float,
        filename: str,
        info_dict: dict[str, Any],
    ) -> None:
        elapsed = time.time() - started
        speed = None
        eta = None
        if downloaded > 0 and elapsed >= 0.001:
            speed = downloaded / elapsed
            if total > downloaded and speed:
                eta = int((total - downloaded) / speed)
        status: dict[str, Any] = {
            "status": "downloading",
            "downloaded_bytes": downloaded,
            "filename": filename,
            "tmpfilename": filename,
            "elapsed": elapsed,
            "speed": speed,
            "eta": eta,
        }
        if total > 0:
            status["total_bytes"] = total
        self._hook_progress(status, info_dict)

    def _open_range(self, request: Request) -> Any:
        last: BaseException | None = None
        for _ in range(3):
            try:
                return self.ydl.urlopen(request)
            except TransportError as err:
                last = err
        if last is not None:
            raise last
        raise TransportError("media request failed")

    def _read_closed_range(
        self,
        url: str,
        headers: dict[str, Any],
        start: int,
        end: int,
        stream: Any,
        info_dict: dict[str, Any],
        total: int,
        started: float,
        filename: str,
    ) -> tuple[int, int | None]:
        request = Request(url, headers=headers)
        request.headers["Range"] = f"bytes={start}-{end}"
        resp = None
        try:
            try:
                resp = self._open_range(request)
            except HTTPError as err:
                if err.status == 416 and start > 0:
                    return 0, start
                if err.status == 403:
                    raise _StaleGooglevideo(err) from err
                raise
            except TransportError as err:
                # Обрыв посреди файла — та же судьба, что у 403: новая ссылка на этот кусок.
                if start > 0:
                    raise _StaleGooglevideo(err) from err
                raise
            status = _response_status(resp)
            if status == 200 and start > 0:
                raise _StaleGooglevideo()
            if status not in (200, 206):
                raise _StaleGooglevideo()
            content_range = _response_header(resp.headers, "Content-Range")
            cr_start, cr_end, cr_total = parse_http_range(content_range)
            if status == 206 and cr_start is not None and cr_start != start:
                raise _StaleGooglevideo()
            want = end - start + 1
            seen_total = cr_total if cr_total else None
            if cr_end is not None and cr_start == start:
                promised = cr_end - start + 1
                if 0 < promised < want:
                    want = promised
            else:
                length_raw = _response_header(resp.headers, "Content-Length")
                try:
                    length = int(length_raw) if length_raw else 0
                except ValueError:
                    length = 0
                if status == 200 and 0 < length < want:
                    want = length
                    seen_total = start + length
            wrote = 0
            try:
                while wrote < want:
                    block = resp.read(min(256 * 1024, want - wrote))
                    if not block:
                        break
                    stream.write(block)
                    wrote += len(block)
                    self._report_media(
                        start + wrote,
                        seen_total or total,
                        started,
                        filename,
                        info_dict,
                    )
            except TransportError as err:
                if start > 0 or wrote > 0:
                    raise _StaleGooglevideo(err) from err
                raise
            if wrote != want:
                raise _StaleGooglevideo()
            return wrote, seen_total
        finally:
            if resp is not None:
                resp.close()


class _YoutubeSession(yt_dlp.YoutubeDL):
    """Для файла YouTube подменяет загрузчик: оборванный кусок берётся новой ссылкой."""

    def dl(self, name, info, subtitle=False, test=False):
        if (
            not subtitle
            and not test
            and name != "-"
            and _needs_fresh_googlevideo_url(info)
        ):
            fd = _GooglevideoFD(self, self.params)
            for ph in self._progress_hooks:
                fd.add_progress_hook(ph)
            new_info = self._copy_infodict(info)
            if new_info.get("http_headers") is None:
                new_info["http_headers"] = self._calc_headers(new_info)
            return fd.download(name, new_info, subtitle)
        return super().dl(name, info, subtitle=subtitle, test=test)


def _ydl_for(url: str) -> type:
    if _is_youtube_url(url):
        return _YoutubeSession
    return yt_dlp.YoutubeDL


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

    # HackerTube показывает ролик YouTube только после клика по заглушке.
    # yt-dlp такой сайт не знает: сразу подставляем обычную ссылку на ролик.
    vid = page_videos._hackertube_id(s)
    if vid:
        return "https://www.youtube.com/watch?v=" + vid

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


def _cookie_cache_dir() -> Path:
    """Запомненный вход из браузеров. Установщик эту папку не удаляет."""
    return BASE / "cookies"


_cookie_io_lock = threading.Lock()


def _saved_browser_cookies_path(browser: str) -> Path:
    name = (browser or "").strip().lower()
    if name not in BROWSER_CHOICES:
        raise yt_dlp.utils.DownloadError(
            f"could not find {browser} cookies database"
        )
    return _cookie_cache_dir() / f"{name}.txt"


def _saved_cookies_usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _saved_browsers() -> list[str]:
    return [
        browser
        for browser in BROWSER_CHOICES
        if _saved_cookies_usable(_saved_browser_cookies_path(browser))
    ]


def _page_cookie_files() -> list[str]:
    """Уже сохранённый вход: ручной cookies.txt и копии браузеров."""
    paths: list[str] = []
    manual = _find_cookies_file()
    if manual is not None:
        paths.append(str(manual))
    for browser in _saved_browsers():
        paths.append(str(_saved_browser_cookies_path(browser)))
    return paths


def _store_cookie_jar(jar: Any, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        jar.save(str(tmp), ignore_discard=True, ignore_expires=True)
        tmp.replace(dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _resolve_browser_cookies(browser: str) -> tuple[Path, bool]:
    """Файл с входом из браузера и флаг «это уже сохранённая копия».

    Свежее чтение обновляет копию. Если браузер держит базу или не отдаёт
    cookies, а копия уже есть, берём её и браузер можно не закрывать.
    """
    saved = _saved_browser_cookies_path(browser)
    with _cookie_io_lock:
        try:
            jar = yt_dlp.cookies.extract_cookies_from_browser(browser)
        except Exception:
            if _saved_cookies_usable(saved):
                return saved, True
            raise
        if not any(True for _ in jar):
            if _saved_cookies_usable(saved):
                return saved, True
            raise yt_dlp.utils.DownloadError(
                f"could not find {browser} cookies database"
            )
        _store_cookie_jar(jar, saved)
        return saved, False


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
                "Закрой его полностью, вместе со значком в трее, один раз: "
                "Качалка запомнит вход, и дальше браузер можно не закрывать."
            )
        return (
            "Браузер держит файл cookies. "
            "Закрой его полностью, вместе со значком в трее, один раз: "
            "Качалка запомнит вход, и дальше его можно не закрывать."
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
            "Закрой его полностью, в том числе значок в трее, один раз: "
            "Качалка запомнит вход, и дальше браузер можно не закрывать. "
            "Либо положи cookies.txt рядом с Качалкой "
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
                "Выбери браузер, где ты вошла. Первый раз закрой его полностью — "
                "Качалка запомнит вход, и дальше браузер можно не закрывать. "
                "Если сохранённый вход устарел, закрой браузер ещё один раз."
            )
        if (
            "sign in to confirm" in low
            and "your age" not in low
            and (_is_youtube_url(url) or "[youtube]" in low)
        ):
            return (
                "YouTube не отдал файл: просит подтвердить, что это не робот. "
                "Ролик может быть открытым. Войди в YouTube в Яндекс.Браузере, "
                "закрой его полностью один раз и попробуй снова"
            )
        return "Видео недоступно — оно закрыто, ограничено по региону или требует входа"
    if "not available in your region" in low or "недоступно в вашем регионе" in low:
        return "Видео недоступно в твоём регионе"
    # «not available» ниже означает «удалено». У YouTube та же фраза бывает,
    # когда открытый ролик не отдал обычный файл — её надо поймать раньше.
    if "requested format is not available" in low and (
        _is_youtube_url(url) or "[youtube]" in low
    ):
        return (
            "YouTube не отдал обычный файл. Ролик может быть открытым — "
            "так сайт режет скачивание. Попробуй ещё раз чуть позже"
        )
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
            if title and not _state.get("chosen_name"):
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
            if info.get("title") and not _state.get("chosen_name"):
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


# «119 reactions · 1.4K shares | Название» — так Facebook кладёт счётчики в имя.
_COUNT_TOKEN = r"\d+(?:[ \u00a0.,]\d+)*(?:\s*(?:тыс\.?|млн\.?|[kmb]))?"
_ENGAGE_UNIT = (
    r"(?:views?|reactions?|comments?|shares?|likes?|"
    r"просмотр(?:ов|а)?|реакци(?:й|и|я)|"
    r"комментари(?:ев|й|я|и)|репост(?:ов|а)?)"
)
_ENGAGE_CHUNK = rf"{_COUNT_TOKEN}\s+{_ENGAGE_UNIT}"
_ENGAGE_PREFIX = re.compile(
    rf"^(?:{_ENGAGE_CHUNK}(?:\s*[·•|,]\s*{_ENGAGE_CHUNK})*)\s*\|\s*",
    re.IGNORECASE,
)
_FILENAME_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FILENAME_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)
_FILENAME_EXTS = (
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".m4a",
    ".mp3",
    ".aac",
    ".ogg",
    ".opus",
    ".flac",
    ".wav",
)


def _clean_site_title(title: str) -> str:
    """Убрать из названия счётчики просмотров и реакций в начале."""
    text = re.sub(r"\s+", " ", title or "").strip()
    cleaned = _ENGAGE_PREFIX.sub("", text).strip()
    cleaned = re.sub(r"\s*\|\s*Facebook$", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned or text


def _filename_stem(raw: str | None) -> str | None:
    """Имя без папки и расширения. Пустая строка — оставить название с сайта."""
    text = _clean_site_title(raw or "")
    if not text:
        return None
    text = text.replace("\\", "/").split("/")[-1].strip()
    lower = text.lower()
    for ext in _FILENAME_EXTS:
        if lower.endswith(ext) and len(text) > len(ext):
            text = text[: -len(ext)].strip()
            break
    text = _FILENAME_INVALID.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text or text in {".", ".."}:
        return None
    if _FILENAME_RESERVED.match(text):
        text += "_"
    return text[:120]


def _outtmpl(out_dir: Path, filename_stem: str | None) -> str:
    if filename_stem:
        literal = filename_stem.replace("%", "%%")
        return str(out_dir / f"{literal}.%(ext)s")
    return str(out_dir / "%(title).120s.%(ext)s")


def _ydl_opts(
    out_dir: Path,
    mode: str = MODE_VIDEO,
    quality: str = QUALITY_BEST,
    *,
    cookies_file: Path | None = None,
    cookies_browser: str | None = None,
    player_clients: list[str] | None = None,
    filename_stem: str | None = None,
    for_youtube: bool = False,
) -> dict[str, Any]:
    """Опции yt-dlp: видео (mp4 + качество) или только звук (mp3)."""
    opts: dict[str, Any] = {
        "outtmpl": _outtmpl(out_dir, filename_stem),
        "noplaylist": True,
        "progress_hooks": [_progress_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # Чуть устойчивее к обрывам (VK/CDN)
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
    }
    # Чужой User-Agent на YouTube даёт 403 даже у открытого ролика.
    # Для остальных сайтов оставляем обычный заголовок браузера.
    if not for_youtube:
        opts["http_headers"] = dict(_HTTP_HEADERS)
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

    _apply_js_solver(opts)

    if for_youtube and not player_clients:
        player_clients = list(_YOUTUBE_PLAYER_CLIENTS)
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
    filename_stem: str | None = None,
) -> None:
    mode = _normalize_mode(mode)
    quality = _normalize_quality(quality)
    url = _normalize_url(url)
    browser = _normalize_browser(cookies_browser)
    cookies_file = _find_cookies_file()
    browser_cookie_file: Path | None = None

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
            browser_cookie_file, from_cache = _resolve_browser_cookies(browser)
            if from_cache:
                hint = f"Сохранённый вход из «{_browser_title(browser)}»"
            else:
                hint = f"Вход из «{_browser_title(browser)}» сохранён"
            warning = f"{warning} {hint}".strip() if warning else hint

        with _lock:
            _state.update(
                {
                    "status": "downloading",
                    "percent": 0.0,
                    "speed": "",
                    "size": "",
                    "title": filename_stem or "",
                    "chosen_name": filename_stem or "",
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
            fresh: bool = False,
        ) -> tuple[Any, Path]:
            opts = _ydl_opts(
                out_dir,
                mode,
                quality,
                cookies_file=use_file,
                cookies_browser=use_browser,
                player_clients=player_clients,
                filename_stem=filename_stem,
                for_youtube=_is_youtube_url(url),
            )
            if fresh:
                # Оборванный первый файл нельзя докачивать уже с другим входом.
                opts["continuedl"] = False
            with _ydl_for(url)(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = _final_path(ydl, info or {}, mode)
                return info, path

        use_file = cookies_file or browser_cookie_file
        try:
            info, path = _run(use_file=use_file, use_browser=None)
        except Exception as first_exc:  # noqa: BLE001
            # Мобильная страница иногда упирается в «не робот» или 403.
            # Тогда пробуем сохранённый вход и встроенный плеер.
            if _is_youtube_url(url) and _youtube_needs_fallback(first_exc):
                saved = (
                    []
                    if use_file is not None
                    else [Path(item) for item in _page_cookie_files()]
                )
                plans = _youtube_retry_plan(use_file, saved)
                last_exc: BaseException = first_exc
                opened = False
                for jar, clients in plans:
                    try:
                        if jar is not None and use_file is None:
                            retry_note = (
                                "YouTube просит подтвердить, что это не робот. "
                                "Пробую сохранённый вход…"
                            )
                        else:
                            retry_note = "YouTube не отдал файл, пробую ещё раз…"
                        with _lock:
                            _state["warning"] = (
                                (warning + " " if warning else "") + retry_note
                            ).strip()
                        info, path = _run(
                            use_file=jar,
                            use_browser=None,
                            player_clients=clients,
                            fresh=True,
                        )
                        notes: list[str] = []
                        if jar is not None and use_file is None:
                            notes.append("Сохранённый вход из браузера")
                        if notes:
                            warning = f"{warning} {' '.join(notes)}".strip()
                            with _lock:
                                _state["warning"] = warning
                        opened = True
                        break
                    except Exception as retry_exc:  # noqa: BLE001
                        last_exc = retry_exc
                        if not _youtube_needs_fallback(retry_exc):
                            raise
                        continue
                if not opened:
                    raise last_exc
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
                        saved, cached = _resolve_browser_cookies(b)
                        with _lock:
                            _state["warning"] = (
                                (warning + " " if warning else "")
                                + f"Пробую браузер «{_browser_title(b)}»…"
                            ).strip()
                        info, path = _run(use_file=saved, use_browser=None)
                        best_exc = None  # type: ignore[assignment]
                        note = (
                            f"Сохранённый вход из «{_browser_title(b)}»"
                            if cached
                            else f"Вход из «{_browser_title(b)}» сохранён"
                        )
                        with _lock:
                            _state["warning"] = (
                                (warning + " " if warning else "") + note
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
            elif (
                page_videos._kinescope_id(url)
                and _needs_auth_retry(first_exc)
                and use_file is None
            ):
                last_exc: BaseException = first_exc
                opened = False
                for cookie_path in _page_cookie_files():
                    try:
                        with _lock:
                            _state["warning"] = (
                                (warning + " " if warning else "")
                                + "Ролик закрытый, пробую сохранённый вход…"
                            ).strip()
                        info, path = _run(use_file=Path(cookie_path), use_browser=None)
                        note = "Сохранённый вход из браузера"
                        warning = f"{warning} {note}".strip() if warning else note
                        with _lock:
                            _state["warning"] = warning
                        opened = True
                        break
                    except Exception as retry_exc:  # noqa: BLE001
                        last_exc = retry_exc
                        continue
                if not opened:
                    raise last_exc
            else:
                raise

        default_title = "Аудио" if mode == MODE_AUDIO else "Видео"
        title = (info or {}).get("title") or default_title
        height = int((info or {}).get("height") or 0)
        if (
            mode == MODE_VIDEO
            and _is_youtube_url(url)
            and height
            and height <= 360
        ):
            note = (
                f"Файл скачан в {height}p: более высокое качество "
                "YouTube сейчас не отдаёт."
            )
            warning = f"{warning} {note}".strip() if warning else note
            with _lock:
                _state["warning"] = warning

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
        _state["chosen_name"] = ""
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
    # none | chrome | edge | firefox | opera | brave | chromium | yandex
    cookies_browser: str | None = None
    filename: str | None = None


class PreviewNameRequest(BaseModel):
    url: str
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
        title = _clean_site_title(str(item.get("title") or "")) or "Видео"
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


class OpenReleaseRequest(BaseModel):
    url: str = ""


def _version_parts(text: str) -> tuple[int, ...] | None:
    """«v1.3.10» → (1, 3, 10). Пустая или нечисловая строка — не версия."""
    raw = (text or "").strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    if not raw:
        return None
    parts: list[int] = []
    for piece in raw.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            return None
        parts.append(int(digits))
    return tuple(parts)


def _version_is_newer(latest: str, current: str) -> bool:
    new = _version_parts(latest)
    old = _version_parts(current)
    if not new or not old:
        return False
    width = max(len(new), len(old))
    new = new + (0,) * (width - len(new))
    old = old + (0,) * (width - len(old))
    return new > old


def _allowed_release_url(url: str) -> bool:
    """Только страница релизов этого репозитория. Чужой адрес браузер не открывает."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "github.com":
        return False
    path = parsed.path or ""
    return path == _RELEASE_PREFIX or path.startswith(_RELEASE_PREFIX + "/")


def _setup_download_url(release: dict[str, Any]) -> str:
    """Файл установщика из списка вложений. Архив расширения сюда не берём."""
    tag = str(release.get("tag_name") or "").lstrip("vV")
    found: list[tuple[int, str]] = []
    assets = release.get("assets") or []
    if not isinstance(assets, list):
        return ""
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        low = name.lower()
        if not low.startswith("kachalka-setup") or not low.endswith(".exe"):
            continue
        if not _allowed_release_url(url):
            continue
        score = 2 if tag and tag in name else 1
        found.append((score, url))
    if not found:
        return ""
    found.sort(key=lambda item: item[0], reverse=True)
    return found[0][1]


def _describe_release(release: dict[str, Any], current: str | None = None) -> dict[str, Any]:
    here = current or APP_VERSION
    tag = str(release.get("tag_name") or "")
    if _version_parts(tag) is None:
        return _update_unreachable(here)
    page = str(release.get("html_url") or "")
    if page and not _allowed_release_url(page):
        page = ""
    newer = _version_is_newer(tag, here)
    download = _setup_download_url(release) if newer else ""
    if not download and newer and page:
        download = page
    shown = tag[1:] if tag[:1] in ("v", "V") else tag
    if newer:
        message = f"Вышла версия {shown}. Сейчас стоит {here}."
    else:
        message = "Это последняя версия."
    return {
        "ok": True,
        "current": here,
        "latest": shown,
        "update": newer,
        "message": message,
        "download_url": download,
        "page_url": page,
    }


def _update_unreachable(current: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "current": current or APP_VERSION,
        "latest": "",
        "update": False,
        "message": "Не удалось проверить обновления. Проверь интернет и попробуй ещё раз.",
        "download_url": "",
        "page_url": "",
    }


def fetch_latest_release(timeout: float = 8.0) -> dict[str, Any]:
    """Свежий выпуск с GitHub. Черновики и предварительные версии сюда не попадают."""
    request = urllib.request.Request(
        _UPDATE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Kachalka/{APP_VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(1_000_000)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not a release")
    return data


def check_for_update() -> dict[str, Any]:
    try:
        release = fetch_latest_release()
    except (OSError, ValueError, json.JSONDecodeError):
        return _update_unreachable()
    return _describe_release(release)


def open_release_page(url: str) -> dict[str, Any]:
    if not _allowed_release_url(url):
        return {"ok": False, "error": "Эту ссылку открывать нельзя."}
    try:
        webbrowser.open(url)
    except OSError:
        return {"ok": False, "error": "Не удалось открыть ссылку."}
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    cookies = _find_cookies_file()
    return {
        "version": APP_VERSION,
        "default_folder": str(DEFAULT_OUT),
        "default_folder_label": "Скачанное",
        "cookies_file": str(cookies) if cookies else None,
        "cookies_file_name": cookies.name if cookies else None,
        "browsers": list(BROWSER_CHOICES),
        "saved_browsers": _saved_browsers(),
    }


@app.get("/api/updates")
def updates() -> dict[str, Any]:
    """Сравнить установленную версию со свежим выпуском на GitHub."""
    return check_for_update()


@app.post("/api/open-release")
def open_release(req: OpenReleaseRequest) -> dict[str, Any]:
    """Открыть страницу выпуска или установщик в браузере, не в окне Качалки."""
    return open_release_page(req.url or "")


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


def _quiet_ydl_opts(
    *,
    browser: str | None,
    use_file: bool,
    cookie_path: Path | None = None,
    for_youtube: bool = False,
) -> dict[str, Any]:
    """Метаданные ролика без скачивания и без прогресса в окне."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 1,
    }
    if not for_youtube:
        opts["http_headers"] = dict(_HTTP_HEADERS)
    else:
        opts["extractor_args"] = {
            "youtube": {"player_client": list(_YOUTUBE_PLAYER_CLIENTS)}
        }
    _apply_js_solver(opts)
    if (FFMPEG_BIN / "ffmpeg.exe").exists():
        opts["ffmpeg_location"] = str(FFMPEG_BIN)
    if cookie_path is not None and cookie_path.is_file():
        opts["cookiefile"] = str(cookie_path)
    elif use_file:
        cookies = _find_cookies_file()
        if cookies is not None:
            opts["cookiefile"] = str(cookies)
        elif browser:
            saved, _cached = _resolve_browser_cookies(browser)
            opts["cookiefile"] = str(saved)
    return opts


def _extract_title(
    url: str,
    *,
    browser: str | None,
    use_file: bool,
    cookie_path: Path | None = None,
) -> str:
    with yt_dlp.YoutubeDL(
        _quiet_ydl_opts(
            browser=browser,
            use_file=use_file,
            cookie_path=cookie_path,
            for_youtube=_is_youtube_url(url),
        )
    ) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    if info.get("_type") == "playlist":
        entries = [item for item in (info.get("entries") or []) if item]
        if entries:
            info = entries[0]
    title = str(info.get("title") or "").strip()
    if not title:
        raise yt_dlp.utils.DownloadError("no title")
    return title


def _lookup_title(url: str, browser: str | None) -> str:
    try:
        return _extract_title(url, browser=None, use_file=False)
    except Exception as exc:
        if not (_needs_auth_retry(exc) or _youtube_needs_fallback(exc)):
            raise
        if _find_cookies_file() is not None or browser:
            return _extract_title(url, browser=browser, use_file=True)
        last_exc: BaseException = exc
        for item in _page_cookie_files()[:2]:
            try:
                return _extract_title(
                    url,
                    browser=None,
                    use_file=False,
                    cookie_path=Path(item),
                )
            except Exception as retry_exc:  # noqa: BLE001
                last_exc = retry_exc
                continue
        raise last_exc


@app.post("/api/preview-name")
def preview_name(req: PreviewNameRequest) -> dict[str, Any]:
    """Узнать имя файла до скачивания, без записи ролика на диск."""
    url = _normalize_url(req.url or "")
    if not url.startswith("http"):
        return {
            "ok": False,
            "error": "Это не похоже на ссылку. Вставь адрес вида https://…",
        }
    try:
        title = _lookup_title(url, _normalize_browser(req.cookies_browser))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": _human_error(exc, url=url)}
    name = _filename_stem(title)
    if not name:
        return {
            "ok": False,
            "error": "Сайт не сказал, как называется ролик. Впиши имя сам.",
        }
    return {"ok": True, "name": name}


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
        if _saved_cookies_usable(_saved_browser_cookies_path(cookies_browser)):
            extra = f"Сохранённый вход из «{_browser_title(cookies_browser)}»"
        else:
            extra = (
                f"Возьму вход из «{_browser_title(cookies_browser)}» и запомню его"
            )
        warning = f"{warning} {extra}".strip() if warning else extra
    raw_name = (req.filename or "").strip()
    stem = _filename_stem(raw_name) if raw_name else None
    if raw_name and not stem:
        return {
            "ok": False,
            "error": "В имени файла не осталось нормальных символов. Поправь его.",
        }

    # Статус до запуска потока: иначе первый опрос ещё видит idle
    # и окно прячет прогресс, будто кнопка ничего не сделала.
    with _lock:
        _state.update(
            {
                "status": "downloading",
                "percent": 0.0,
                "speed": "",
                "size": "",
                "title": stem or "",
                "chosen_name": stem or "",
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
        args=(url, req.folder, mode, quality, cookies_browser, stem),
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
        "filename": stem,
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
                "chosen_name": "",
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


page_videos.set_page_cookie_files(_page_cookie_files)


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8031, reload=False)
