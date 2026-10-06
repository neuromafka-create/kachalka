# Яндекс.Браузер в списке cookies и разные тексты ошибок Chrome/Edge и закрытого файла.
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yt_dlp.cookies as cookies
from yt_dlp.utils import DownloadError

import app


class _SilentLog:
    def debug(self, *args, **kwargs) -> None:
        return None

    def info(self, *args, **kwargs) -> None:
        return None

    def warning(self, *args, **kwargs) -> None:
        return None

    def error(self, *args, **kwargs) -> None:
        return None


class BrowserCookieTests(unittest.TestCase):
    def test_yandex_is_a_browser_choice(self) -> None:
        self.assertEqual(app.BROWSER_CHOICES[0], "yandex")
        self.assertEqual(app._normalize_browser("Yandex"), "yandex")
        self.assertIsNone(app._normalize_browser("ie"))
        self.assertEqual(app._browser_title("yandex"), "Яндекс.Браузер")

    def test_yandex_profile_path_is_registered(self) -> None:
        self.assertIn("yandex", cookies.SUPPORTED_BROWSERS)
        self.assertIn("yandex", cookies.CHROMIUM_BASED_BROWSERS)
        cfg = cookies._get_chromium_based_browser_settings("yandex")
        self.assertEqual(cfg["browser_dir"], app._yandex_user_data_dir())
        self.assertTrue(str(cfg["browser_dir"]).endswith(
            str(Path("Yandex") / "YandexBrowser" / "User Data")
        ))
        self.assertTrue(cfg["supports_profiles"])
        chrome = cookies._get_chromium_based_browser_settings("chrome")
        self.assertIn("Chrome", chrome["browser_dir"])

    def test_v20_cookie_is_skipped_without_dpapi(self) -> None:
        before = getattr(cookies, "_kachalka_skipped_v20", 0)
        with tempfile.TemporaryDirectory() as tmp:
            decryptor = cookies.WindowsChromeCookieDecryptor(tmp, _SilentLog())
            self.assertIsNone(decryptor.decrypt(b"v20" + b"x" * 32))
        self.assertEqual(cookies._kachalka_skipped_v20, before + 1)

    def test_empty_v20_jar_explains_app_bound(self) -> None:
        orig = cookies._extract_chrome_cookies

        def fake(browser_name, profile, keyring, logger):  # noqa: ANN001
            cookies._kachalka_skipped_v20 = 2
            return cookies.YoutubeDLCookieJar()

        cookies._extract_chrome_cookies = fake
        try:
            with self.assertRaises(DownloadError) as caught:
                cookies.extract_cookies_from_browser("chrome", logger=_SilentLog())
        finally:
            cookies._extract_chrome_cookies = orig
        self.assertIn("app-bound", str(caught.exception))

    def test_locked_database_names_the_browser(self) -> None:
        orig = cookies._extract_chrome_cookies

        def fake(browser_name, profile, keyring, logger):  # noqa: ANN001
            raise DownloadError(
                "Could not copy Chrome cookie database. See https://example.invalid"
            )

        cookies._extract_chrome_cookies = fake
        try:
            with self.assertRaises(DownloadError) as caught:
                cookies.extract_cookies_from_browser("yandex", logger=_SilentLog())
        finally:
            cookies._extract_chrome_cookies = orig
        self.assertIn("yandex", str(caught.exception).lower())

    def test_error_text_splits_lock_and_app_bound(self) -> None:
        locked = app._human_error(
            DownloadError("could not copy yandex cookie database")
        )
        self.assertIn("Яндекс.Браузер", locked)
        self.assertIn("трее", locked)
        self.assertNotIn("DPAPI", locked)

        bound = app._human_error(
            DownloadError("chrome cookies are app-bound (v20) and were not shared")
        )
        self.assertIn("Яндекс.Браузер", bound)
        self.assertNotIn("Windows DPAPI", bound)
        self.assertNotIn("Закрой браузер и попробуй", bound)

        yandex_bound = app._human_error(
            DownloadError("yandex cookies are app-bound (v20) and were not shared")
        )
        self.assertIn("cookies.txt", yandex_bound)
        self.assertNotIn("выбери", yandex_bound.lower())

        try:
            raise DownloadError("Could not copy yandex cookie database")
        except DownloadError as cause:
            try:
                raise DownloadError("ERROR: failed to load cookies") from cause
            except DownloadError as outer:
                text = app._human_error(outer)
        self.assertIn("трее", text)

    def test_vk_retry_starts_with_yandex(self) -> None:
        self.assertEqual(app.VK_COOKIE_RETRY[0], "yandex")
        self.assertNotIn("chrome", app.VK_COOKIE_RETRY)
        self.assertNotIn("edge", app.VK_COOKIE_RETRY)
        self.assertGreater(
            app._cookie_failure_rank(
                DownloadError("could not copy yandex cookie database")
            ),
            app._cookie_failure_rank(DownloadError("login required")),
        )

    def test_ui_lists_yandex(self) -> None:
        html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('<option value="yandex">Войти через Яндекс.Браузер</option>', html)
        self.assertIn('"yandex"', html)


if __name__ == "__main__":
    unittest.main()
