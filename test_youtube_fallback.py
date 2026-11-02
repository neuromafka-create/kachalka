# Запасной клиент YouTube, когда CDN отвечает 403 на открытый ролик.
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yt_dlp
from yt_dlp.networking.exceptions import HTTPError

import app


class YoutubeFallbackTests(unittest.TestCase):
    def test_youtube_hosts(self) -> None:
        self.assertTrue(app._is_youtube_url("https://www.youtube.com/watch?v=-5gV7DH7oQ4"))
        self.assertTrue(app._is_youtube_url("https://youtu.be/-5gV7DH7oQ4"))
        self.assertTrue(app._is_youtube_url("https://m.youtube.com/watch?v=abc"))
        self.assertFalse(app._is_youtube_url("https://vk.ru/video-1_2"))

    def test_cdn_block_is_403_only(self) -> None:
        self.assertTrue(app._youtube_cdn_blocked(RuntimeError("HTTP Error 403: Forbidden")))
        self.assertFalse(app._youtube_cdn_blocked(RuntimeError("Video unavailable")))

    def test_other_youtube_refusals_use_the_same_fallback(self) -> None:
        self.assertTrue(
            app._youtube_needs_fallback(
                RuntimeError("Sign in to confirm you're not a bot")
            )
        )
        self.assertTrue(
            app._youtube_needs_fallback(
                RuntimeError("Requested format is not available")
            )
        )
        self.assertFalse(app._youtube_needs_fallback(RuntimeError("Video unavailable")))
        self.assertFalse(
            app._youtube_needs_fallback(
                RuntimeError("Sign in to confirm your age")
            )
        )

    def test_bot_check_is_not_called_a_closed_video(self) -> None:
        text = app._human_error(
            RuntimeError("[youtube] Sign in to confirm you're not a bot"),
            url="https://youtu.be/8ITWNqPaylo",
        )
        self.assertIn("не робот", text)
        self.assertIn("Яндекс", text)
        self.assertNotIn("закрыто", text)

    def test_missing_file_is_not_called_deleted(self) -> None:
        text = app._human_error(
            RuntimeError("[youtube] 8ITWNqPaylo: Requested format is not available"),
            url="https://youtu.be/8ITWNqPaylo",
        )
        self.assertIn("обычный файл", text)
        self.assertNotIn("удалено", text)
        other = app._human_error(
            RuntimeError("Requested format is not available"),
            url="https://example.com/watch",
        )
        self.assertIn("удалено", other)

    def test_js_runtime_uses_node_when_it_is_installed(self) -> None:
        original = app.shutil.which
        original_home = app.Path.home

        def which(name: str) -> str | None:
            if name == "node":
                return r"C:\node\node.exe"
            return None

        app.shutil.which = which
        app.Path.home = staticmethod(lambda: Path(r"C:\no\such\home"))
        try:
            opts = app._ydl_opts(app.BASE)
        finally:
            app.shutil.which = original
            app.Path.home = original_home
        self.assertEqual(opts["js_runtimes"], {"node": {"path": r"C:\node\node.exe"}})
        self.assertEqual(opts["remote_components"], ["ejs:github"])

    def test_youtube_does_not_pretend_to_be_another_browser(self) -> None:
        youtube = app._ydl_opts(app.BASE, for_youtube=True)
        other = app._ydl_opts(app.BASE)
        self.assertNotIn("http_headers", youtube)
        self.assertEqual(
            youtube["extractor_args"]["youtube"]["player_client"],
            ["mweb"],
        )
        self.assertFalse(youtube["continuedl"])
        self.assertNotIn("extractor_args", other)
        self.assertIn("Chrome/131", other["http_headers"]["User-Agent"])

    def test_403_message_does_not_call_public_video_closed(self) -> None:
        text = app._human_error(
            RuntimeError("ERROR: unable to download video data: HTTP Error 403: Forbidden"),
            url="https://www.youtube.com/watch?v=-5gV7DH7oQ4",
        )
        self.assertIn("403", text)
        self.assertNotIn("доступ запрещён", text)

    def test_saved_login_is_tried_before_the_embedded_player(self) -> None:
        jar = Path(r"C:\cookies\yandex.txt")
        self.assertEqual(
            app._youtube_retry_plan(None, [jar]),
            [(jar, ["mweb"]), (jar, ["web_embedded"])],
        )

    def test_cookies_already_in_use_try_the_embedded_player(self) -> None:
        jar = Path(r"C:\cookies\cookies.txt")
        self.assertEqual(
            app._youtube_retry_plan(jar, [Path(r"C:\cookies\yandex.txt")]),
            [(jar, ["web_embedded"])],
        )

    def test_without_cookies_the_embedded_player_is_still_tried(self) -> None:
        self.assertEqual(
            app._youtube_retry_plan(None, []),
            [(None, ["web_embedded"])],
        )

    def test_android_client_is_a_fresh_download(self) -> None:
        opts = app._ydl_opts(
            app.BASE,
            player_clients=["android"],
        )
        self.assertEqual(opts["extractor_args"]["youtube"]["player_client"], ["android"])
        self.assertFalse(opts["continuedl"])

    def test_closed_ranges_cover_the_file_without_gaps(self) -> None:
        ranges = app._youtube_closed_ranges(25, 10)
        self.assertEqual(ranges, [(0, 9), (10, 19), (20, 24)])
        self.assertEqual(ranges[-1][1], 24)
        cursor = 0
        for start, end in ranges:
            self.assertEqual(start, cursor)
            self.assertLessEqual(end - start + 1, 10)
            cursor = end + 1
        self.assertEqual(cursor, 25)

        total = 145982675
        chunk = 10 << 20
        ranges = app._youtube_closed_ranges(total, chunk)
        self.assertEqual(ranges[0], (0, chunk - 1))
        self.assertEqual(ranges[-1][1], total - 1)
        self.assertEqual(ranges[-1][0] % chunk, 0)
        cursor = 0
        for start, end in ranges:
            self.assertEqual(start, cursor)
            self.assertLessEqual(end - start + 1, chunk)
            cursor = end + 1
        self.assertEqual(cursor, total)

    def test_refresh_cap_allows_a_new_link_for_each_piece(self) -> None:
        self.assertEqual(app._youtube_refresh_cap(None, 10), 24)
        self.assertGreaterEqual(app._youtube_refresh_cap(145982675, 10 << 20), 14)
        self.assertLessEqual(app._youtube_refresh_cap(145982675, 10 << 20), 80)

    def test_youtube_download_uses_the_piece_reader(self) -> None:
        self.assertIs(app._ydl_for("https://youtu.be/mB_PlIIO2SA"), app._YoutubeSession)
        self.assertIs(app._ydl_for("https://vk.ru/video-1_2"), yt_dlp.YoutubeDL)
        self.assertTrue(
            app._needs_fresh_googlevideo_url(
                {"url": "https://rr.googlevideo.com/videoplayback?id=1"}
            )
        )
        self.assertFalse(
            app._needs_fresh_googlevideo_url(
                {
                    "url": "https://rr.googlevideo.com/videoplayback?id=1",
                    "fragments": [{"url": "https://rr.googlevideo.com/a"}],
                }
            )
        )
        self.assertFalse(
            app._needs_fresh_googlevideo_url({"url": "https://vk.ru/video.mp4"})
        )

    def test_403_after_bytes_retries_the_same_range_on_a_new_link(self) -> None:
        body = self._download_with_script(
            {
                ("1", 0): (206, b"A" * 10, "bytes 0-9/25"),
                ("1", 10): 403,
                ("2", 10): (206, b"B" * 10, "bytes 10-19/25"),
                ("2", 20): (206, b"C" * 5, "bytes 20-24/25"),
            }
        )
        self.assertEqual(body, b"A" * 10 + b"B" * 10 + b"C" * 5)
        ranges = [(url_id, start) for url_id, start in self.calls]
        self.assertEqual(ranges, [("1", 0), ("1", 10), ("2", 10), ("2", 20)])

    def test_403_at_the_first_byte_does_not_ask_for_a_new_link(self) -> None:
        with self.assertRaises(Exception) as caught:
            self._download_with_script({("1", 0): 403})
        self.assertTrue(app._youtube_cdn_blocked(caught.exception))
        self.assertEqual(self.extracts, [])

    def test_ignored_range_is_not_appended(self) -> None:
        body = self._download_with_script(
            {
                ("1", 0): (206, b"A" * 10, "bytes 0-9/20"),
                ("1", 10): (200, b"X" * 20, None),
                ("2", 10): (206, b"B" * 10, "bytes 10-19/20"),
            }
        )
        self.assertEqual(body, b"A" * 10 + b"B" * 10)
        self.assertNotIn(b"X", body)

    def _download_with_script(self, script: dict) -> bytes:
        original_chunk = app._YOUTUBE_MEDIA_CHUNK
        original_pause = app._YOUTUBE_REFRESH_PAUSE
        app._YOUTUBE_MEDIA_CHUNK = 10
        app._YOUTUBE_REFRESH_PAUSE = 0
        self.calls = []
        self.extracts = []

        class _Denied:
            status = 403
            reason = "Forbidden"

            def close(self) -> None:
                return None

        class _Resp:
            def __init__(self, status: int, body: bytes, content_range: str | None):
                self.status = status
                self.headers = {
                    "Content-Length": str(len(body)),
                }
                if content_range:
                    self.headers["Content-Range"] = content_range
                self._body = body
                self._pos = 0

            def read(self, size: int) -> bytes:
                part = self._body[self._pos : self._pos + size]
                self._pos += len(part)
                return part

            def close(self) -> None:
                return None

        class _Colors:
            out = False

        outer = self

        class _FakeYdl:
            params = {"quiet": True, "noprogress": True}
            _allow_colors = _Colors()

            def __getattr__(self, name: str):
                return lambda *args, **kwargs: None

            def evaluate_outtmpl(self, *args, **kwargs) -> str:
                return ""

            def _format_text(self, stream, allow, text, *args, **kwargs):
                return text

            def extract_info(self, url, download=False):
                outer.extracts.append((url, download))
                return {
                    "format_id": "18",
                    "url": "https://rr.googlevideo.com/videoplayback?id=2",
                }

            def urlopen(self, request):
                header = str(request.headers.get("Range") or "")
                start = int(header.split("=", 1)[1].split("-", 1)[0])
                url_id = "2" if "id=2" in request.url else "1"
                outer.calls.append((url_id, start))
                outcome = script[(url_id, start)]
                if outcome == 403:
                    raise HTTPError(_Denied())
                status, body, content_range = outcome
                return _Resp(status, body, content_range)

        ydl = _FakeYdl()
        fd = app._GooglevideoFD(ydl, {"quiet": True, "noprogress": True, "continuedl": False})
        info = {
            "url": "https://rr.googlevideo.com/videoplayback?id=1",
            "format_id": "18",
            "webpage_url": "https://www.youtube.com/watch?v=mB_PlIIO2SA",
            "filesize": 10,
            "http_headers": {},
        }
        if ("2", 20) in script:
            info["filesize"] = 25
        elif ("2", 10) in script:
            info["filesize"] = 20
        try:
            with tempfile.TemporaryDirectory() as folder:
                dest = Path(folder) / "clip.mp4"
                fd.real_download(str(dest), info)
                return dest.read_bytes()
        finally:
            app._YOUTUBE_MEDIA_CHUNK = original_chunk
            app._YOUTUBE_REFRESH_PAUSE = original_pause
