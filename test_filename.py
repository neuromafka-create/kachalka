# Имя файла до скачивания: своё название и без счётчиков Facebook.
from __future__ import annotations

import unittest

import app


class SyncThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None) -> None:
        self._target = target
        self._args = args or ()

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args)


class FilenameTests(unittest.TestCase):
    def test_facebook_counts_are_not_the_name(self) -> None:
        self.assertEqual(
            app._filename_stem(
                "119 reactions · 1.4K shares | Asif Nawab Butt on Reels"
            ),
            "Asif Nawab Butt on Reels",
        )
        self.assertEqual(
            app._filename_stem("1,2 тыс. просмотров · 89 реакций | Урок по магазину"),
            "Урок по магазину",
        )
        self.assertEqual(
            app._filename_stem("12K views, 30 reactions | Hello world"),
            "Hello world",
        )

    def test_plain_name_stays_editable(self) -> None:
        self.assertEqual(app._filename_stem("Обычный урок"), "Обычный урок")
        self.assertEqual(app._filename_stem("Урок: первый.mp4"), "Урок первый")
        self.assertIsNone(app._filename_stem("   "))
        self.assertIsNone(app._filename_stem("???"))
        self.assertEqual(app._filename_stem("con"), "con_")
        self.assertEqual(app._filename_stem(r"C:\Видео\Урок.mp4"), "Урок")

    def test_download_template_uses_the_chosen_name(self) -> None:
        custom = app._ydl_opts(app.BASE, filename_stem="100% готов")
        self.assertTrue(custom["outtmpl"].endswith("100%% готов.%(ext)s"))
        plain = app._ydl_opts(app.BASE)
        self.assertTrue(plain["outtmpl"].endswith("%(title).120s.%(ext)s"))

    def test_start_download_sends_cleaned_name(self) -> None:
        captured: dict[str, str | None] = {}

        def fake(
            url: str,
            folder: str | None = None,
            mode: str = "video",
            quality: str = "best",
            cookies_browser: str | None = None,
            filename_stem: str | None = None,
        ) -> None:
            captured["stem"] = filename_stem
            with app._lock:
                app._state["status"] = "idle"

        old_download = app._do_download
        old_thread = app.threading.Thread
        old_state = dict(app._state)
        app._do_download = fake
        app.threading.Thread = SyncThread
        with app._lock:
            app._state["status"] = "idle"
        try:
            result = app.start_download(
                app.DownloadRequest(
                    url="https://example.com/video",
                    filename="119 reactions · 1.4K shares | Урок.mp4",
                )
            )
        finally:
            app._do_download = old_download
            app.threading.Thread = old_thread
            app._state.clear()
            app._state.update(old_state)
        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], "Урок")
        self.assertEqual(captured["stem"], "Урок")

    def test_empty_name_keeps_site_title(self) -> None:
        captured: dict[str, str | None] = {}

        def fake(
            url: str,
            folder: str | None = None,
            mode: str = "video",
            quality: str = "best",
            cookies_browser: str | None = None,
            filename_stem: str | None = None,
        ) -> None:
            captured["stem"] = filename_stem
            with app._lock:
                app._state["status"] = "idle"

        old_download = app._do_download
        old_thread = app.threading.Thread
        old_state = dict(app._state)
        app._do_download = fake
        app.threading.Thread = SyncThread
        with app._lock:
            app._state["status"] = "idle"
        try:
            result = app.start_download(
                app.DownloadRequest(url="https://example.com/video", filename="   ")
            )
        finally:
            app._do_download = old_download
            app.threading.Thread = old_thread
            app._state.clear()
            app._state.update(old_state)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["filename"])
        self.assertIsNone(captured["stem"])


if __name__ == "__main__":
    unittest.main()
