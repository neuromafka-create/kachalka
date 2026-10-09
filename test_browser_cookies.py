# Яндекс.Браузер в списке cookies и разные тексты ошибок Chrome/Edge и закрытого файла.
from __future__ import annotations

import tempfile
import unittest
from http.cookiejar import Cookie
from pathlib import Path
from unittest.mock import patch

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
        self.assertIn("запомнит", locked)
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
        self.assertIn("запомнит вход", html)
        self.assertIn("saved_browsers", html)
        self.assertIn("savedBrowsers", html)


def _session_cookie() -> Cookie:
    """Сессионная cookie: discard=True, без срока. В файл её надо сохранить явно."""
    return Cookie(
        0,
        "sid",
        "test-value",
        None,
        False,
        "vk.ru",
        True,
        False,
        "/",
        True,
        True,
        None,
        True,
        None,
        None,
        {},
    )


def _jar_with_sid() -> cookies.YoutubeDLCookieJar:
    jar = cookies.YoutubeDLCookieJar()
    jar.set_cookie(_session_cookie())
    return jar


class SavedBrowserCookiesTests(unittest.TestCase):
    def test_saves_session_cookie_and_reuses_when_browser_is_locked(self) -> None:
        calls = {"n": 0}

        def fake(browser, *args, **kwargs):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                self.assertEqual(browser, "yandex")
                return _jar_with_sid()
            raise DownloadError(f"could not copy {browser} cookie database")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(app, "_cookie_cache_dir", return_value=root),
                patch.object(cookies, "extract_cookies_from_browser", fake),
            ):
                path, cached = app._resolve_browser_cookies("yandex")
                self.assertFalse(cached)
                self.assertEqual(path, root / "yandex.txt")
                text = path.read_text(encoding="utf-8")
                self.assertIn("sid", text)
                self.assertIn("test-value", text)
                loaded = cookies.YoutubeDLCookieJar()
                loaded.load(str(path))
                self.assertIn("sid", [item.name for item in loaded])

                again, from_cache = app._resolve_browser_cookies("yandex")
                self.assertTrue(from_cache)
                self.assertEqual(again, path)
                self.assertIn("sid", again.read_text(encoding="utf-8"))

                with self.assertRaises(DownloadError) as caught:
                    app._resolve_browser_cookies("firefox")
                self.assertIn("firefox", str(caught.exception).lower())
                self.assertFalse((root / "firefox.txt").exists())

    def test_empty_jar_does_not_erase_saved_login(self) -> None:
        calls = {"n": 0}

        def fake(browser, *args, **kwargs):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                return _jar_with_sid()
            return cookies.YoutubeDLCookieJar()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(app, "_cookie_cache_dir", return_value=root),
                patch.object(cookies, "extract_cookies_from_browser", fake),
            ):
                path, cached = app._resolve_browser_cookies("opera")
                self.assertFalse(cached)
                kept = path.read_text(encoding="utf-8")
                again, from_cache = app._resolve_browser_cookies("opera")
                self.assertTrue(from_cache)
                self.assertEqual(again.read_text(encoding="utf-8"), kept)
                self.assertIn("sid", kept)

    def test_app_bound_without_a_saved_file_creates_nothing(self) -> None:
        def fake(browser, *args, **kwargs):  # noqa: ANN001
            raise DownloadError(
                f"{browser} cookies are app-bound (v20) and were not shared"
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(app, "_cookie_cache_dir", return_value=root),
                patch.object(cookies, "extract_cookies_from_browser", fake),
            ):
                with self.assertRaises(DownloadError) as caught:
                    app._resolve_browser_cookies("chrome")
                self.assertIn("app-bound", str(caught.exception))
                self.assertFalse((root / "chrome.txt").exists())
                self.assertEqual(list(root.iterdir()), [])

    def test_empty_saved_file_is_not_reused(self) -> None:
        def fake(browser, *args, **kwargs):  # noqa: ANN001
            raise DownloadError(f"could not copy {browser} cookie database")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "brave.txt").write_text("", encoding="utf-8")
            with (
                patch.object(app, "_cookie_cache_dir", return_value=root),
                patch.object(cookies, "extract_cookies_from_browser", fake),
            ):
                with self.assertRaises(DownloadError):
                    app._resolve_browser_cookies("brave")

    def test_browser_name_cannot_escape_the_cache_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(app, "_cookie_cache_dir", return_value=root):
                with self.assertRaises(DownloadError):
                    app._resolve_browser_cookies(r"..\cookies")
            self.assertEqual(list(root.iterdir()), [])

    def test_manual_cookies_file_beats_the_browser(self) -> None:
        def fake(browser, *args, **kwargs):  # noqa: ANN001
            raise AssertionError("browser cookies must not be read")

        with tempfile.TemporaryDirectory() as tmp:
            manual = Path(tmp) / "cookies.txt"
            manual.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            with (
                patch.object(app, "_find_cookies_file", return_value=manual),
                patch.object(cookies, "extract_cookies_from_browser", fake),
            ):
                opts = app._quiet_ydl_opts(browser="yandex", use_file=True)
            self.assertEqual(opts["cookiefile"], str(manual))
            self.assertNotIn("cookiesfrombrowser", opts)

    def test_preview_uses_saved_login_when_the_browser_is_locked(self) -> None:
        calls = {"n": 0}

        def fake(browser, *args, **kwargs):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                return _jar_with_sid()
            raise DownloadError(f"could not copy {browser} cookie database")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(app, "_cookie_cache_dir", return_value=root),
                patch.object(app, "_find_cookies_file", return_value=None),
                patch.object(cookies, "extract_cookies_from_browser", fake),
            ):
                first = app._quiet_ydl_opts(browser="yandex", use_file=True)
                second = app._quiet_ydl_opts(browser="yandex", use_file=True)
            self.assertEqual(first["cookiefile"], str(root / "yandex.txt"))
            self.assertNotIn("cookiesfrombrowser", first)
            self.assertNotIn("cookiesfrombrowser", second)
            self.assertEqual(second["cookiefile"], str(root / "yandex.txt"))
            self.assertTrue((root / "yandex.txt").is_file())
            self.assertEqual(calls["n"], 2)

    def test_config_lists_saved_browsers(self) -> None:
        def fake(browser, *args, **kwargs):  # noqa: ANN001
            if browser == "yandex":
                return _jar_with_sid()
            raise DownloadError(f"could not copy {browser} cookie database")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(app, "_cookie_cache_dir", return_value=root),
                patch.object(cookies, "extract_cookies_from_browser", fake),
            ):
                app._resolve_browser_cookies("yandex")
                saved = app._saved_browsers()
                cfg = app.get_config()
            self.assertEqual(saved, ["yandex"])
            self.assertEqual(cfg["saved_browsers"], ["yandex"])


if __name__ == "__main__":
    unittest.main()
