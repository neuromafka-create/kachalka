# Файл: kachalka.py
# «Качалка» — скачиватель видео. Кирпичик: yt-dlp.
# Вставь ссылку — видео сохранится в папку «Скачанное» рядом с этим файлом.
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).parent / "Скачанное"
FFMPEG_BIN = Path(__file__).parent / "ffmpeg" / "bin"  # портативный ffmpeg (install.bat)


def main() -> None:
    print("=" * 52)
    print("  ⬇  КАЧАЛКА — скачиватель видео (кирпичик yt-dlp)")
    print("=" * 52)
    url = input("\nВставь ссылку на видео и нажми Enter:\n> ").strip()
    if not url.startswith("http"):
        print("\n[!] Это не похоже на ссылку. Запусти ещё раз и вставь адрес вида https://…")
        return
    OUT.mkdir(exist_ok=True)
    print(f"\nСкачиваю в папку: {OUT}\n")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",  # лучший mp4, иначе лучшее что есть
        "--merge-output-format", "mp4",
        "-o", str(OUT / "%(title).120s.%(ext)s"),
        "--no-playlist",
    ]
    if (FFMPEG_BIN / "ffmpeg.exe").exists():
        cmd += ["--ffmpeg-location", str(FFMPEG_BIN)]  # для склейки лучшего качества
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
