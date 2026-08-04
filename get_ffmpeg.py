# Файл: get_ffmpeg.py
# Скачивает портативный ffmpeg в папку ./ffmpeg/bin — без прав администратора.
# Зеркала (как в Курсографе): сначала GitHub (CDN, обычно быстрее), затем gyan.dev.
# С ffmpeg Качалка скачивает видео в ЛУЧШЕМ качестве (склейка видео+звука);
# без него yt-dlp берёт готовый одиночный файл послабее — тоже работает.
import io
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

BIN_DIR = Path(__file__).parent / "ffmpeg" / "bin"

# настоящий браузерный User-Agent — иначе gyan.dev режет скорость «не-браузерным» клиентам
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

# ВАЖНО: у BtbN тег роллинг-сборки называется "latest" → путь /releases/download/latest/…
# и берём РЕЛИЗНУЮ ветку (n8.1), не master-снапшот.
URLS = [
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]


def _download(url: str) -> bytes:
    print(f"    Источник: {url}")
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf = bytearray()
        start = time.time()
        mb = 1024 * 1024
        while True:
            part = r.read(1 << 16)
            if not part:
                break
            buf += part
            if total:
                pct = int(len(buf) * 100 / total)
                speed = len(buf) / max(time.time() - start, 0.1) / mb
                sys.stdout.write(f"\r    {pct:3d}%  {len(buf)/mb:5.1f}/{total/mb:.1f} МБ  {speed:4.1f} МБ/с ")
                sys.stdout.flush()
        print()
        return bytes(buf)


def main() -> int:
    if (BIN_DIR / "ffmpeg.exe").exists():
        print("[OK] ffmpeg уже на месте — пропускаю.")
        return 0
    for i, url in enumerate(URLS, 1):
        try:
            print(f"Скачиваю портативный ffmpeg (зеркало {i} из {len(URLS)})…")
            data = _download(url)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                BIN_DIR.mkdir(parents=True, exist_ok=True)
                for info in z.infolist():
                    name = Path(info.filename).name.lower()
                    if name in ("ffmpeg.exe", "ffprobe.exe"):
                        (BIN_DIR / name).write_bytes(z.read(info))
            if (BIN_DIR / "ffmpeg.exe").exists():
                print(f"[OK] ffmpeg установлен: {BIN_DIR}")
                return 0
            print("    [!] в архиве не нашлось ffmpeg.exe — пробую другое зеркало")
        except Exception as e:
            print(f"    [!] зеркало не сработало ({e}) — пробую следующее")
    print("[!] Не удалось скачать ffmpeg. Не страшно: Качалка работает и без него,")
    print("    просто качество видео будет пониже. Запусти install.bat позже ещё раз.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
