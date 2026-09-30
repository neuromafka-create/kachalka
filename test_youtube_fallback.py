# Запасной клиент YouTube, когда CDN отвечает 403 на открытый ролик.
from __future__ import annotations

import unittest

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

    def test_403_message_does_not_call_public_video_closed(self) -> None:
        text = app._human_error(
            RuntimeError("ERROR: unable to download video data: HTTP Error 403: Forbidden"),
            url="https://www.youtube.com/watch?v=-5gV7DH7oQ4",
        )
        self.assertIn("403", text)
        self.assertNotIn("доступ запрещён", text)

    def test_android_client_is_a_fresh_download(self) -> None:
        opts = app._ydl_opts(
            app.BASE,
            player_clients=["android"],
        )
        self.assertEqual(opts["extractor_args"]["youtube"]["player_client"], ["android"])
        self.assertFalse(opts["continuedl"])
