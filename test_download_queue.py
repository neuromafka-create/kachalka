# Очередь нескольких роликов из расширения браузера.
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


class DownloadQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._do_download = app._do_download
        self._thread = app.threading.Thread
        self._pending = list(app._pending)
        self._state = dict(app._state)
        app._pending.clear()
        app._state.update(
            {
                "status": "idle",
                "queued": 0,
                "queue_note": "",
                "error": "",
                "title": "",
            }
        )
        self.order: list[str] = []

        def fake_download(
            url: str,
            folder: str | None = None,
            mode: str = "video",
            quality: str = "best",
            cookies_browser: str | None = None,
        ) -> None:
            self.order.append(url)
            with app._lock:
                app._state["status"] = "done"
                app._state["queued"] = len(app._pending)
            app._pump_queue()

        app._do_download = fake_download
        app.threading.Thread = SyncThread

    def tearDown(self) -> None:
        app._do_download = self._do_download
        app.threading.Thread = self._thread
        app._pending[:] = self._pending
        app._state.clear()
        app._state.update(self._state)

    def test_several_urls_run_one_after_another(self) -> None:
        result = app._enqueue_downloads(
            [
                "https://example.com/a.mp4",
                "https://example.com/a.mp4",
                "не ссылка",
                "https://example.com/b.mp4",
            ],
            None,
            "video",
            "best",
            None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(self.order, [
            "https://example.com/a.mp4",
            "https://example.com/b.mp4",
        ])
        self.assertEqual(app._state["status"], "done")
        self.assertEqual(app._state["queued"], 0)
        self.assertEqual(app._pending, [])

    def test_busy_download_keeps_the_rest_waiting(self) -> None:
        app._state["status"] = "downloading"
        result = app._enqueue_downloads(
            ["https://example.com/c.mp4", "https://example.com/d.mp4"],
            None,
            "video",
            "best",
            None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.order, [])
        self.assertEqual(len(app._pending), 2)
        app._state["status"] = "done"
        app._pump_queue()
        self.assertEqual(self.order, [
            "https://example.com/c.mp4",
            "https://example.com/d.mp4",
        ])

    def test_empty_selection_is_rejected(self) -> None:
        result = app._enqueue_downloads(["", "просто текст"], None, "video", "best", None)
        self.assertFalse(result["ok"])
        self.assertEqual(app._pending, [])
