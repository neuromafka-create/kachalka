# Файл: kachalka.py
# «Качалка» — скачиватель видео. Кирпичик: yt-dlp.
# Вставь ссылку — видео сохранится в папку «Скачанное» рядом с этим файлом.
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

OUT = Path(__file__).parent / "Скачанное"
FFMPEG_BIN = Path(__file__).parent / "ffmpeg" / "bin"  # портативный ffmpeg (install.bat)
BASE = Path(__file__).resolve().parent
COOKIES_NAMES = ("cookies.txt", "vk_cookies.txt", "youtube_cookies.txt")
# С июля 2026 основной домен — vk.ru (.com ещё работает как зеркало)
VK_PREFERRED_HOST = "vk.ru"
_VK_VIDEO_ID_RE = re.compile(
    r"(?:video|clip)(-?\d+_\d+)|z=video(-?\d+_\d+)",
    re.IGNORECASE,
)


def _configure_ssl_certs() -> None:
    """CA-бандл для дочернего yt-dlp (иначе на Windows VK часто падает по SSL)."""
    try:
        import certifi
    except ImportError:
        return
    ca = certifi.where()
    if Path(ca).is_file():
        os.environ.setdefault("SSL_CERT_FILE", ca)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
        os.environ.setdefault("CURL_CA_BUNDLE", ca)


def _is_vk_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "vk.com" or host.endswith(
        (".vk.com", "vk.ru", ".vk.ru", "vkvideo.ru", ".vkvideo.ru")
    )


def _normalize_url(url: str) -> str:
    s = (url or "").strip().strip("\"'<>")
    if not s:
        return s
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
    if s.startswith("//"):
        s = "https:" + s
    elif re.match(r"^(?:[\w-]+\.)?(?:vk|vkvideo)\.(?:com|ru)/", s, re.I):
        s = "https://" + s
    try:
        parsed = urlparse(s)
        host = (parsed.hostname or "").lower()
    except ValueError:
        parsed = None
        host = ""
    if host in ("vk.com", "m.vk.com", "new.vk.com", "www.vk.com"):
        s = parsed._replace(netloc=host.replace("vk.com", VK_PREFERRED_HOST)).geturl()  # type: ignore[union-attr]
    if _is_vk_url(s):
        found = _VK_VIDEO_ID_RE.search(s)
        vid = (found.group(1) or found.group(2)) if found else None
        if not vid:
            qs = parse_qs(urlparse(s).query)
            z = unquote((qs.get("z") or [""])[0])
            zm = re.search(r"video(-?\d+_\d+)", z, re.I)
            if zm:
                vid = zm.group(1)
        if vid:
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
    for d in (BASE, OUT):
        for name in COOKIES_NAMES:
            p = d / name
            if p.is_file() and p.stat().st_size > 0:
                return p
    return None


def _pick_embedded(url: str) -> str:
    """Если вставили адрес статьи — взять встроенный ролик."""
    try:
        import page_videos
    except ImportError:
        return url
    found = page_videos.inspect_page(url)
    videos = list(found.get("videos") or [])
    if found.get("mode") != "embeds" or not videos:
        if found.get("scanned"):
            print("Встроенного плеера на странице не нашёл — пробую скачать саму ссылку.\n")
        return url
    if len(videos) == 1:
        print(f"На странице нашёл встроенное видео: {videos[0]['title']}")
        print(videos[0]["url"])
        return str(videos[0]["url"])
    print("На странице несколько роликов:")
    for i, item in enumerate(videos, 1):
        print(f"  {i}. {item['title']}")
        print(f"     {item['url']}")
    raw = input("Номер ролика (Enter — первый): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(videos):
        return str(videos[int(raw) - 1]["url"])
    print("Беру первый.")
    return str(videos[0]["url"])


def main() -> None:
    _configure_ssl_certs()
    print("=" * 52)
    print("  ⬇  КАЧАЛКА — скачиватель видео (кирпичик yt-dlp)")
    print("=" * 52)
    url = _normalize_url(
        input("\nВставь ссылку на видео или на страницу, где оно встроено:\n> ")
    )
    if not url.startswith("http"):
        print("\n[!] Это не похоже на ссылку. Запусти ещё раз и вставь адрес вида https://…")
        return
    url = _pick_embedded(url)
    OUT.mkdir(exist_ok=True)
    print(f"\nСкачиваю в папку: {OUT}")
    print(f"Ссылка: {url}\n")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        # progressive mp4 (VK url720/1080) + DASH/HLS fallback
        "-f", "b[ext=mp4]/bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(OUT / "%(title).120s.%(ext)s"),
        "--no-playlist",
        "--retries", "10",
        "--fragment-retries", "10",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ]
    if (FFMPEG_BIN / "ffmpeg.exe").exists():
        cmd += ["--ffmpeg-location", str(FFMPEG_BIN)]  # для склейки лучшего качества
    cookies = _find_cookies_file()
    if cookies is not None:
        cmd += ["--cookies", str(cookies)]
        print(f"Cookies: {cookies.name}\n")
    elif _is_vk_url(url):
        print(
            "Подсказка ВК: если ролик закрытый — положи cookies.txt рядом "
            "с kachalka.py (экспорт из браузера) или используй start.bat с UI.\n"
        )
    result = subprocess.run(cmd + [url])
    if result.returncode == 0:
        print("\n[OK] Готово! Видео лежит в папке «Скачанное».")
    else:
        print("\n[!] Не получилось скачать. Скопируй ВЕСЬ текст выше и отправь его")
        print("    нейросети со словами: «объясни простыми словами и почини» —")
        print("    промпт для этого лежит в файле ПРОМПТЫ_ДЛЯ_ДОПИЛА.txt")


if __name__ == "__main__":
    try:
        main()
    finally:
        input("\nНажми Enter, чтобы закрыть окно…")
