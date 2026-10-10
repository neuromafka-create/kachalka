# Файл: page_videos.py
# Ищет ролики, встроенные в обычную страницу: iframe, <video>, og:video, JSON-LD.
# Прямую ссылку на YouTube/VK не трогает — её и так понимает yt-dlp.
from __future__ import annotations

import base64
import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

MAX_HTML_BYTES = 2_000_000
MAX_VIDEOS = 12
FETCH_TIMEOUT = 12.0

_MEDIA_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".ogv", ".m3u8", ".mpd")
_SECRET_KEYS = ("hash", "access_key", "access_hash", "p", "token", "password", "sig", "h")
_GENERIC_TITLES = {
    "",
    "video",
    "video player",
    "youtube",
    "youtube video player",
    "vk",
    "вк",
    "rutube",
    "vimeo",
    "player",
    "embedded video",
}
_SKIP_HOST_SUFFIXES = (
    "doubleclick.net",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
    "mc.yandex.ru",
    "mc.yandex.com",
    "hotjar.com",
    "scorecardresearch.com",
    "facebook.net",
    "gravatar.com",
)
# Сайт сам является видеосервисом: ленту и рекомендации не разбираем,
# их отдаём yt-dlp. Обычная статья сюда не попадает.
_PLATFORM_SUFFIXES = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "vk.com",
    "vk.ru",
    "vkvideo.ru",
    "rutube.ru",
    "vimeo.com",
    "dailymotion.com",
    "dai.ly",
    "ok.ru",
    "tiktok.com",
    "twitch.tv",
    "instagram.com",
    "twitter.com",
    "x.com",
)
_MEDIA_NAME_SKIP = (
    "thumbnail",
    "thumb",
    "/poster",
    "sprite",
    "placeholder",
    "/avatar",
    "/logo",
    "/icon",
    "favicon",
)
# Только адреса плеера, а не обычные ссылки «смотреть на YouTube» в тексте статьи.
_EMBED_RES = (
    re.compile(
        r"https?://(?:www\.|m\.)?(?:youtube\.com|youtube-nocookie\.com)/embed/[\w-]{6,}",
        re.I,
    ),
    re.compile(
        r"https?://(?:[\w.-]+\.)?vk\.(?:com|ru)/video_ext\.php\?[^\"'\s<>]{0,500}",
        re.I,
    ),
    re.compile(
        r"https?://(?:[\w.-]+\.)?vkvideo\.ru/video_ext\.php\?[^\"'\s<>]{0,500}",
        re.I,
    ),
    re.compile(
        r"https?://(?:www\.)?rutube\.ru/(?:play/embed|embed)/[\w-]+/?"
        r"(?:\?[^\s\"'<>)]{0,300})?",
        re.I,
    ),
    re.compile(
        r"https?://player\.vimeo\.com/video/\d+(?:\?[^\"'\s<>]{0,200})?",
        re.I,
    ),
    re.compile(r"https?://(?:www\.|m\.)?ok\.ru/videoembed/\d+", re.I),
    re.compile(r"https?://(?:www\.)?dailymotion\.com/embed/video/[\w]+", re.I),
    re.compile(r"https?://(?:www\.)?dzen\.ru/embed/[\w.-]+", re.I),
    re.compile(r"https?://player\.twitch\.tv/\?[^\"'\s<>]{0,400}", re.I),
    # Плеер GetCourse: школы часто сидят на своём домене, а ролик — на sign-player.
    re.compile(
        r"https?://[^\"'\s<>]+/sign-player/\?(?:[^\"'\s<>]*&)?json=[^\"'\s<>]+",
        re.I,
    ),
    re.compile(
        r"https?://[^\"'\s<>]+?\.(?:mp4|webm|mkv|mov|m4v|ogv|m3u8|mpd)"
        r"(?:\?[^\"'\s<>]{0,400})?",
        re.I,
    ),
    # Плеер Kinescope: школы вставляют его и на свою страницу, и внутрь урока.
    re.compile(
        r"https?://(?:www\.)?kinescope\.io/embed/"
        r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9A-Za-z]{10,})/?",
        re.I,
    ),
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

Fetch = Callable[[str], tuple[str, str, str]]


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _host_is(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _skipped_host(host: str) -> bool:
    return any(_host_is(host, s) for s in _SKIP_HOST_SUFFIXES)


def _platform_page(url: str) -> bool:
    host = _host(url)
    return any(_host_is(host, s) for s in _PLATFORM_SUFFIXES)


def _media_ext(path: str) -> str:
    low = (path or "").lower().split("?", 1)[0]
    for ext in _MEDIA_EXTS:
        if low.endswith(ext):
            return ext
    return ""


def _secret_suffix(query: str) -> str:
    qs = parse_qs(query or "")
    parts = [f"{k}={qs[k][0]}" for k in _SECRET_KEYS if qs.get(k) and qs[k][0]]
    return ("|" + "&".join(parts)) if parts else ""


def _has_secret(url: str) -> bool:
    return bool(_secret_suffix(urlparse(url).query))


# Запись Pruffme: в HTML лежит path, а сам файл — на video.pruffme.com.
_PRUFFME_PATH = re.compile(
    r"user/[0-9a-f]{16,64}/video/[0-9a-f]{16,64}"
    r"(?:/\d{3,4})?/video\.(?:mp4|webm|mkv|mov|m4v)$",
    re.I,
)
_PRUFFME_EMBEDDED = re.compile(
    r"embedded_media_content\s*=\s*function\s*\(\)\s*\{\s*/\*(.*?)\*/\s*\}",
    re.S,
)


def _pruffme_file_url(media: dict[str, Any]) -> str:
    """Прямой файл записи. Плейлист m3u8 не берём: mp4 уже целый ролик."""
    direct = str(media.get("url") or "").strip()
    if direct.startswith("https://"):
        parsed = urlparse(direct)
        path = unquote(parsed.path or "").lstrip("/")
        host = _host(direct)
        if (
            _host_is(host, "pruffme.com")
            and _PRUFFME_PATH.fullmatch(path)
            and not parsed.query
        ):
            return "https://video.pruffme.com/" + path
    path = str(media.get("path") or "").strip().replace("\\", "/").lstrip("/")
    if _PRUFFME_PATH.fullmatch(path):
        return "https://video.pruffme.com/" + path
    return ""


def _add_pruffme(html: str, page_url: str, collector: "_Collector") -> None:
    for raw in _PRUFFME_EMBEDDED.findall(html or ""):
        try:
            media = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(media, dict):
            continue
        url = _pruffme_file_url(media)
        if not url:
            continue
        collector.add(url, str(media.get("name") or ""), "file", page_url)


_KINESCOPE_ID = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9A-Za-z]{10,})$",
    re.I,
)


def _kinescope_id(url: str) -> str:
    """Короткий адрес ролика или embed. Скрипт плеера и постер сюда не входят."""
    host = _host(url)
    if host not in ("kinescope.io", "www.kinescope.io"):
        return ""
    parts = [part for part in (urlparse(url).path or "").split("/") if part]
    if len(parts) == 2 and parts[0].lower() == "embed":
        candidate = parts[1]
    elif len(parts) == 1:
        candidate = parts[0]
    else:
        return ""
    if not _KINESCOPE_ID.fullmatch(candidate):
        return ""
    if re.fullmatch(r"[0-9a-f-]{36}", candidate, re.I):
        return candidate.lower()
    return candidate


def _getcourse_player(url: str) -> bool:
    """Подписанный плеер GetCourse. Сам json= обязателен, иначе это не ролик."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    path = (parsed.path or "").rstrip("/")
    if not path.endswith("/sign-player"):
        return False
    return "json=" in (parsed.query or "").lower()


def _getcourse_hash(url: str) -> str:
    """video_hash из json=, чтобы один ролик с двумя подписями не двоился."""
    if not _getcourse_player(url):
        return ""
    # parse_qs превращает «+» из base64 в пробел и ломает video_hash.
    found = re.search(r"(?:^|&)json=([^&]*)", urlparse(url).query)
    raw = unquote(found.group(1)).strip() if found else ""
    if not raw:
        return ""
    padded = raw + "=" * ((4 - len(raw) % 4) % 4)
    try:
        payload = json.loads(base64.b64decode(padded, validate=False))
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    video_hash = str(payload.get("video_hash") or "")
    if re.fullmatch(r"[0-9a-fA-F]{16,64}", video_hash):
        return video_hash.lower()
    return ""


def is_video_url(url: str) -> bool:
    """Ссылка уже указывает на ролик или файл, а не на статью."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = _host(url)
    if not host or _skipped_host(host):
        return False
    path = parsed.path or ""
    ext = _media_ext(path)
    if ext:
        low = unquote(path).lower()
        if any(bad in low for bad in _MEDIA_NAME_SKIP):
            return False
        return True
    if host == "youtu.be" or host.endswith(".youtu.be"):
        return bool(re.fullmatch(r"/[\w-]{6,}/?", path))
    if host.endswith(("youtube.com", "youtube-nocookie.com")):
        if re.search(r"/(?:embed|shorts|live|v)/[\w-]{6,}", path):
            return True
        if path.rstrip("/") == "/watch":
            return bool(parse_qs(parsed.query).get("v"))
        if path.startswith("/playlist"):
            return bool(parse_qs(parsed.query).get("list"))
        return False
    if host.endswith(("vk.com", "vk.ru", "vkvideo.ru")):
        if re.search(r"(?:video|clip)-?\d+_\d+", path) or "video_ext.php" in path:
            return True
        return bool(re.search(r"(?:video|clip)-?\d+_\d+", parsed.query))
    if host.endswith("rutube.ru"):
        return bool(re.search(r"/(?:video|play/embed|embed|shorts)/[\w-]+", path))
    if host == "player.vimeo.com":
        return bool(re.search(r"/video/\d+", path))
    if host.endswith("vimeo.com"):
        return bool(re.fullmatch(r"/(?:video/)?\d+/?", path))
    if host.endswith("dailymotion.com"):
        return bool(re.search(r"/(?:video|embed/video)/[\w]+", path))
    if host == "dai.ly":
        return bool(re.fullmatch(r"/[\w-]+/?", path))
    if host.endswith("ok.ru"):
        return bool(re.search(r"/(?:videoembed|video|live)/\d+", path))
    if host.endswith("dzen.ru"):
        return bool(re.search(r"/(?:embed|video/watch|shorts)/[\w.-]+", path))
    if host.endswith("tiktok.com"):
        return "/video/" in path
    if host.endswith("twitch.tv"):
        return host.startswith("player.") or "/videos/" in path
    if _getcourse_player(url):
        return True
    if _kinescope_id(url):
        return True
    return False


def canonical_key(url: str) -> str:
    """Один и тот же ролик в embed и в og:video не показываем дважды."""
    parsed = urlparse(url)
    host = _host(url)
    path = parsed.path or ""
    secret = _secret_suffix(parsed.query)
    if host == "youtu.be" or host.endswith(".youtu.be"):
        vid = path.strip("/").split("/")[0]
        if re.fullmatch(r"[\w-]{6,}", vid):
            return "yt:" + vid + secret
    if "youtube" in host:
        qs = parse_qs(parsed.query)
        vid = (qs.get("v") or [""])[0]
        if not vid:
            found = re.search(r"/(?:embed|shorts|live|v)/([\w-]{6,})", path)
            if found:
                vid = found.group(1)
        if vid:
            return "yt:" + vid + secret
    if host.endswith(("vk.com", "vk.ru", "vkvideo.ru")):
        found = re.search(r"(?:video|clip)(-?\d+_\d+)", url)
        if found:
            return "vk:" + found.group(1) + secret
        qs = parse_qs(parsed.query)
        if qs.get("oid") and qs.get("id"):
            return f"vk:{qs['oid'][0]}_{qs['id'][0]}{secret}"
    if host.endswith("rutube.ru"):
        found = re.search(r"/(?:play/embed|video|embed|shorts)/([\w-]+)", path)
        if found:
            return "rt:" + found.group(1) + secret
    if host.endswith("vimeo.com"):
        found = re.search(r"/video/(\d+)", path) or re.search(r"/(\d+)/?$", path)
        if found:
            return "vm:" + found.group(1) + secret
    if host.endswith("ok.ru"):
        found = re.search(r"/(?:videoembed|video|live)/(\d+)", path)
        if found:
            return "ok:" + found.group(1) + secret
    video_hash = _getcourse_hash(url)
    if video_hash:
        return "gc:" + video_hash
    kinescope_id = _kinescope_id(url)
    if kinescope_id:
        return "ks:" + kinescope_id
    if _media_ext(path):
        return "file:" + host + path
    return url.split("#", 1)[0]


def download_url(url: str) -> str:
    """Адрес, который отдаём в yt-dlp. Секретные параметры не выкидываем."""
    clean = url.split("#", 1)[0]
    if _has_secret(clean):
        return clean
    key = canonical_key(clean)
    if key.startswith("yt:"):
        return "https://www.youtube.com/watch?v=" + key[3:].split("|", 1)[0]
    if key.startswith("vk:"):
        return "https://vk.ru/video" + key[3:].split("|", 1)[0]
    if key.startswith("rt:"):
        return "https://rutube.ru/video/" + key[3:].split("|", 1)[0] + "/"
    if key.startswith("vm:"):
        return "https://vimeo.com/" + key[3:].split("|", 1)[0]
    if key.startswith("ok:"):
        return "https://ok.ru/video/" + key[3:].split("|", 1)[0]
    return clean


def service_name(url: str) -> str:
    host = _host(url)
    if "youtu" in host:
        return "YouTube"
    if host.endswith(("vk.com", "vk.ru", "vkvideo.ru")):
        return "ВК"
    if host.endswith("rutube.ru"):
        return "Rutube"
    if host.endswith("vimeo.com"):
        return "Vimeo"
    if host.endswith("ok.ru"):
        return "OK"
    if host.endswith("dailymotion.com") or host == "dai.ly":
        return "Dailymotion"
    if host.endswith("dzen.ru"):
        return "Дзен"
    if host.endswith("twitch.tv"):
        return "Twitch"
    if host.endswith("tiktok.com"):
        return "TikTok"
    if _getcourse_player(url):
        return "GetCourse"
    if _kinescope_id(url):
        return "Kinescope"
    if _media_ext(urlparse(url).path):
        return "Файл на странице"
    return host or "Видео"


def _clean_title(text: str) -> str:
    title = re.sub(r"\s+", " ", text or "").strip()
    return title[:140]


def _file_title(url: str) -> str:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    stem = name.rsplit(".", 1)[0]
    stem = re.sub(r"[-_]+", " ", stem).strip()
    if len(stem) >= 3:
        return stem[:140]
    return "Файл на странице"


def _display_title(url: str, raw: str, og_title: str, *, alone: bool, kind: str) -> str:
    title = _clean_title(raw)
    if title.casefold() not in _GENERIC_TITLES:
        return title
    page_title = _clean_title(og_title)
    if alone and page_title.casefold() not in _GENERIC_TITLES:
        return page_title
    if kind in ("video", "file") and _media_ext(urlparse(url).path):
        return _file_title(url)
    return service_name(url)


class _Collector:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []
        self._seen: set[str] = set()

    def add(self, raw_url: str, title: str, kind: str, page_url: str) -> None:
        if len(self.items) >= MAX_VIDEOS:
            return
        candidate = (raw_url or "").strip().strip("\"'<>")
        if not candidate or candidate.startswith(("data:", "blob:", "javascript:", "about:")):
            return
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        absolute = urljoin(page_url, candidate)
        absolute = absolute.split("#", 1)[0]
        if not is_video_url(absolute):
            return
        key = canonical_key(absolute)
        # Один id уже есть (embed с token и тот же embed без query) — не дублируем.
        bare = key.split("|", 1)[0]
        if key in self._seen or bare in self._seen:
            return
        self._seen.add(key)
        self._seen.add(bare)
        self.items.append(
            {
                "url": download_url(absolute),
                "title": _clean_title(title),
                "kind": kind,
            }
        )


class _PageParser(HTMLParser):
    def __init__(self, page_url: str, collector: _Collector) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.collector = collector
        self.og_title = ""
        self._jsonld: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._on_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._on_tag(tag, attrs)

    def _on_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {(k or "").lower(): (v or "") for k, v in attrs}
        name = tag.lower()
        if name == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            content = a.get("content") or ""
            if prop in (
                "og:video",
                "og:video:url",
                "og:video:secure_url",
                "og:video:iframe",
                "twitter:player",
                "twitter:player:stream",
            ):
                self.collector.add(content, "", "meta", self.page_url)
            if prop in ("og:title", "twitter:title") and content and not self.og_title:
                self.og_title = content
            return
        if name == "script":
            if "ld+json" in (a.get("type") or "").lower():
                self._jsonld = []
            return
        if name.endswith("iframe") or name in ("frame", "embed"):
            label = a.get("title") or ""
            for key in ("src", "data-src", "data-lazy-src", "data-iframe-src"):
                if a.get(key):
                    self.collector.add(a[key], label, "iframe", self.page_url)
            return
        if name == "object" and a.get("data"):
            self.collector.add(a.get("data") or "", a.get("title") or "", "object", self.page_url)
            return
        if name == "video":
            label = a.get("title") or ""
            for key in ("src", "data-src", "data-video", "data-url"):
                if a.get(key):
                    self.collector.add(a[key], label, "video", self.page_url)
            return
        if name == "source":
            if (a.get("type") or "").lower().startswith("audio/"):
                return
            if a.get("src"):
                self.collector.add(a["src"], "", "video", self.page_url)
            return
        youtube_id = a.get("data-youtube-id") or ""
        if re.fullmatch(r"[\w-]{6,}", youtube_id):
            self.collector.add(
                f"https://www.youtube.com/embed/{youtube_id}",
                a.get("title") or "",
                "iframe",
                self.page_url,
            )
        # GetCourse кладёт адрес плеера и в iframe, и в data-iframe-src блока.
        lazy_player = a.get("data-iframe-src") or ""
        if lazy_player:
            self.collector.add(lazy_player, a.get("title") or "", "iframe", self.page_url)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._jsonld is not None:
            self._read_jsonld("".join(self._jsonld))
            self._jsonld = None

    def handle_data(self, data: str) -> None:
        if self._jsonld is not None and len(self._jsonld) < 40:
            self._jsonld.append(data[:500_000])

    def _read_jsonld(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        self._walk_jsonld(payload, 0, set())

    def _walk_jsonld(self, node: Any, depth: int, seen: set[int]) -> None:
        if depth > 12:
            return
        if isinstance(node, list):
            for item in node:
                self._walk_jsonld(item, depth + 1, seen)
            return
        if not isinstance(node, dict):
            return
        marker = id(node)
        if marker in seen:
            return
        seen.add(marker)
        types = node.get("@type") or ""
        if isinstance(types, str):
            types = [types]
        type_names = " ".join(str(item).lower() for item in types)
        if "videoobject" in type_names or type_names.strip() == "clip":
            label = node.get("name") if isinstance(node.get("name"), str) else ""
            for key in ("embedUrl", "embedURL", "contentUrl", "contentURL"):
                value = node.get(key)
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if isinstance(item, str):
                        self.collector.add(item, label, "jsonld", self.page_url)
        for value in node.values():
            if isinstance(value, (dict, list)):
                self._walk_jsonld(value, depth + 1, seen)


def _flatten(html: str) -> str:
    flat = (
        html.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u002F", "/")
        .replace("\\u003d", "=")
        .replace("\\u003D", "=")
        .replace("\\u003a", ":")
        .replace("\\u003A", ":")
    )
    return unescape(flat)


def _scan_text(html: str, page_url: str, collector: _Collector) -> None:
    flat = _flatten(html)
    found = 0
    for pattern in _EMBED_RES:
        for match in pattern.finditer(flat):
            # file.mp4.jpg не должен становиться роликом file.mp4
            end = match.end()
            if end < len(flat) and (flat[end].isalnum() or flat[end] in "._-/%"):
                continue
            collector.add(match.group(0).rstrip(").,;'\""), "", "text", page_url)
            found += 1
            if found >= 40 or len(collector.items) >= MAX_VIDEOS:
                return


def find_embedded_videos(html: str, page_url: str) -> list[dict[str, str]]:
    collector = _Collector()
    parser = _PageParser(page_url, collector)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Кривой HTML не должен прятать то, что уже нашли и что видно регулярками.
        pass
    _scan_text(html, page_url, collector)
    _add_pruffme(html, page_url, collector)
    alone = len(collector.items) == 1
    videos: list[dict[str, str]] = []
    for item in collector.items:
        videos.append(
            {
                "url": item["url"],
                "title": _display_title(
                    item["url"],
                    item["title"],
                    parser.og_title,
                    alone=alone,
                    kind=item["kind"],
                ),
                "kind": item["kind"],
            }
        )
    return videos


def _decode(raw: bytes, content_type: str) -> str:
    charset = ""
    found = re.search(r"charset=([\w.-]+)", content_type or "", re.I)
    if found:
        charset = found.group(1)
    if not charset:
        head = raw[:4096]
        meta = re.search(br"charset=[\"']?([\w.-]+)", head, re.I)
        if meta:
            charset = meta.group(1).decode("ascii", errors="ignore")
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _fetch_html(url: str) -> tuple[str, str, str]:
    req = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        },
    )
    try:
        with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            final = resp.geturl()
            ctype = resp.headers.get("Content-Type", "") or ""
            raw = resp.read(MAX_HTML_BYTES)
    except HTTPError as exc:
        final = exc.geturl() or url
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        try:
            raw = exc.read(MAX_HTML_BYTES)
        except Exception:
            raw = b""
    except (URLError, TimeoutError, OSError) as exc:
        raise OSError(str(exc)) from exc
    if urlparse(final).scheme not in ("http", "https"):
        raise OSError("redirect left http")
    return final, _decode(raw, ctype), ctype


def _looks_like_html(text: str, content_type: str) -> bool:
    low = (content_type or "").lower()
    if "html" in low or "xml" in low:
        return True
    if low.startswith("text/"):
        return True
    if not low:
        sample = text.lstrip()[:200].lower()
        return sample.startswith("<!doctype html") or sample.startswith("<html") or "<iframe" in sample or "<video" in sample
    return False


def inspect_page(url: str, *, fetch: Fetch | None = None) -> dict[str, Any]:
    """Разобрать ссылку пользователя.

    mode=direct — качать как есть (это уже ролик, сайт-платформа или страница не открылась).
    mode=embeds — на обычной странице нашлись встроенные ролики.
    scanned=true — HTML страницы реально смотрели.
    """
    if is_video_url(url) or _platform_page(url):
        return {"mode": "direct", "videos": [], "scanned": False}
    loader = fetch or _fetch_html
    try:
        final_url, html, content_type = loader(url)
    except (OSError, ValueError):
        return {"mode": "direct", "videos": [], "scanned": False}
    if not _looks_like_html(html, content_type):
        if is_video_url(final_url):
            title = _display_title(final_url, "", "", alone=True, kind="file")
            return {
                "mode": "embeds",
                "videos": [{"url": download_url(final_url), "title": title, "kind": "redirect"}],
                "scanned": False,
            }
        return {"mode": "direct", "videos": [], "scanned": False}
    videos = find_embedded_videos(html, final_url or url)
    if not videos:
        return {"mode": "direct", "videos": [], "scanned": True}
    return {"mode": "embeds", "videos": videos, "scanned": True}
