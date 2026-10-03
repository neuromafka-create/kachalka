# Файл: test_page_videos.py
# Проверка поиска роликов, встроенных в HTML. Сеть не нужна, кроме одного
# локального сервера внутри теста inspect.
from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import page_videos as pv

PAGE = "https://news.example/article/1"
YT = "https://www.youtube.com/watch?v=abcdefghijk"


def _urls(html: str, page: str = PAGE) -> list[str]:
    return [item["url"] for item in pv.find_embedded_videos(html, page)]


class FindEmbeddedTests(unittest.TestCase):
    def test_youtube_iframe(self) -> None:
        html = """
        <html><head>
          <meta property="og:title" content="Лекция про продажу"/>
        </head><body>
          <iframe
            src="https://www.youtube.com/embed/abcdefghijk"
            title="YouTube video player"></iframe>
        </body></html>
        """
        found = pv.find_embedded_videos(html, PAGE)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["url"], YT)
        self.assertEqual(found[0]["title"], "Лекция про продажу")
        self.assertEqual(found[0]["kind"], "iframe")

    def test_protocol_relative_and_amp(self) -> None:
        html = '<iframe src="//www.youtube.com/embed/abcdefghijk?start=10&amp;rel=0"></iframe>'
        self.assertEqual(_urls(html), [YT])

    def test_same_video_from_embed_and_og_is_one(self) -> None:
        html = """
        <meta property="og:video" content="https://www.youtube.com/watch?v=abcdefghijk"/>
        <iframe src="https://www.youtube-nocookie.com/embed/abcdefghijk"></iframe>
        """
        self.assertEqual(_urls(html), [YT])

    def test_two_different_players(self) -> None:
        html = """
        <iframe src="https://www.youtube.com/embed/abcdefghijk" title="Первый"></iframe>
        <iframe src="https://vk.com/video_ext.php?oid=-10&id=20&hash=abc" title="Второй"></iframe>
        """
        found = pv.find_embedded_videos(html, PAGE)
        self.assertEqual(
            [item["url"] for item in found],
            [YT, "https://vk.com/video_ext.php?oid=-10&id=20&hash=abc"],
        )
        self.assertEqual([item["title"] for item in found], ["Первый", "Второй"])

    def test_vk_ext_without_hash_becomes_vk_ru(self) -> None:
        html = '<iframe src="https://vk.com/video_ext.php?oid=-10&amp;id=20"></iframe>'
        self.assertEqual(_urls(html), ["https://vk.ru/video-10_20"])

    def test_relative_video_file(self) -> None:
        html = '<video><source src="/media/lecture.mp4" type="video/mp4"/></video>'
        found = pv.find_embedded_videos(html, PAGE)
        self.assertEqual(found[0]["url"], "https://news.example/media/lecture.mp4")
        self.assertEqual(found[0]["title"], "lecture")

    def test_poster_image_is_not_a_video(self) -> None:
        html = '<img src="/media/poster.jpg"><video poster="/thumbs/preview.jpg"></video>'
        self.assertEqual(_urls(html), [])

    def test_plain_link_is_not_an_embed(self) -> None:
        html = '<p>Смотри <a href="https://youtu.be/abcdefghijk">здесь</a></p>'
        self.assertEqual(_urls(html), [])

    def test_json_ld_and_escaped_script(self) -> None:
        html = """
        <script type="application/ld+json">
        {"@type": "VideoObject", "name": "Разбор",
         "embedUrl": "https://rutube.ru/play/embed/deadbeef/"}
        </script>
        <script>var p = "https:\\/\\/www.youtube.com\\/embed\\/zzzzzzzzzzz";</script>
        """
        found = pv.find_embedded_videos(html, PAGE)
        self.assertEqual(
            [item["url"] for item in found],
            [
                "https://rutube.ru/video/deadbeef/",
                "https://www.youtube.com/watch?v=zzzzzzzzzzz",
            ],
        )
        self.assertEqual(found[0]["title"], "Разбор")

    def test_data_youtube_id(self) -> None:
        html = '<div data-youtube-id="abcdefghijk"></div>'
        self.assertEqual(_urls(html), [YT])

    def test_lazy_iframe(self) -> None:
        html = '<iframe src="about:blank" data-src="https://player.vimeo.com/video/991122"></iframe>'
        self.assertEqual(_urls(html), ["https://vimeo.com/991122"])

    def test_rutube_token_inside_script(self) -> None:
        html = '<script>player("https://rutube.ru/play/embed/deadbeef/?p=secret")</script>'
        self.assertEqual(_urls(html), ["https://rutube.ru/play/embed/deadbeef/?p=secret"])

    def test_mp4_jpg_is_not_a_video(self) -> None:
        html = '<img src="https://cdn.example/photo.mp4.jpg">'
        self.assertEqual(_urls(html), [])

    def test_rutube_private_token_kept(self) -> None:
        html = '<iframe src="https://rutube.ru/play/embed/deadbeef/?p=secret"></iframe>'
        self.assertEqual(_urls(html), ["https://rutube.ru/play/embed/deadbeef/?p=secret"])

    def test_ad_iframe_ignored(self) -> None:
        html = '<iframe src="https://googleads.g.doubleclick.net/pagead/html/r2026"></iframe>'
        self.assertEqual(_urls(html), [])

    def test_audio_source_ignored(self) -> None:
        html = '<audio><source src="/a.mp3" type="audio/mpeg"/></audio>'
        self.assertEqual(_urls(html), [])

    def test_getcourse_player_keeps_signature_and_collapses_copies(self) -> None:
        import base64

        payload = base64.b64encode(
            b'{"video_hash":"e0d48990424815b4cca708573e6510bf","user_id":-1}'
        ).decode("ascii")
        first = (
            "https://vh-api-1-de.gceuproxy.com/sign-player/"
            f"?json={payload}&s=abc123"
        )
        second = first.replace("s=abc123", "s=def456")
        html = (
            f'<iframe src="{first}"></iframe>'
            f'<div data-iframe-src="{second}"></div>'
        )
        found = pv.find_embedded_videos(html, "https://vasilinfo.ru/vkshopszap")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["url"], first)
        self.assertEqual(found[0]["title"], "GetCourse")
        self.assertIn("s=abc123", found[0]["url"])

    def test_getcourse_host_without_player_is_not_a_video(self) -> None:
        html = '<a href="https://vh-api-1-de.gceuproxy.com/health">cdn</a>'
        self.assertEqual(_urls(html), [])


class GetCourseExtractorTests(unittest.TestCase):
    def test_ytdlp_accepts_gceuproxy_player(self) -> None:
        import app  # noqa: F401  — патч плеера при импорте
        from yt_dlp.extractor.lazy_extractors import GetCourseRuPlayerIE

        player = (
            "https://vh-api-1-de.gceuproxy.com/sign-player/"
            "?json=eyJ2aWRlb19oYXNoIjoiZTBkNDg5OTA0MjQ4MTViNGNjYTcwODU3M2U2NTEwYmYifQ&s=abc"
        )
        classic = "http://player02.getcourse.ru/sign-player/?json=eyJ2aWRlb19oYXNoIjoiYWJjIn0&s=1"
        self.assertTrue(GetCourseRuPlayerIE.suitable(player))
        self.assertTrue(GetCourseRuPlayerIE.suitable(classic))
        self.assertFalse(GetCourseRuPlayerIE.suitable("https://vasilinfo.ru/vkshopszap"))


class DirectUrlTests(unittest.TestCase):
    def test_known_video_links(self) -> None:
        samples = [
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://youtu.be/abcdefghijk",
            "https://vk.ru/video-10_20",
            "https://rutube.ru/video/deadbeef/",
            "https://news.example/clip.mp4",
            "https://vimeo.com/991122",
        ]
        for url in samples:
            self.assertTrue(pv.is_video_url(url), url)

    def test_article_and_home_are_not_videos(self) -> None:
        for url in (
            "https://news.example/article/1",
            "https://www.youtube.com/",
            "https://vk.ru/feed",
            "https://news.example/poster.jpg",
        ):
            self.assertFalse(pv.is_video_url(url), url)

    def test_inspect_skips_fetch_for_direct_link(self) -> None:
        def boom(_url: str) -> tuple[str, str, str]:
            raise AssertionError("страницу ролика качать не нужно")

        found = pv.inspect_page("https://youtu.be/abcdefghijk", fetch=boom)
        self.assertEqual(found["mode"], "direct")
        self.assertFalse(found["scanned"])

    def test_inspect_reports_embeds(self) -> None:
        html = '<iframe src="https://www.youtube.com/embed/abcdefghijk" title="Урок"></iframe>'

        def fake(_url: str) -> tuple[str, str, str]:
            return PAGE, html, "text/html; charset=utf-8"

        found = pv.inspect_page(PAGE, fetch=fake)
        self.assertEqual(found["mode"], "embeds")
        self.assertTrue(found["scanned"])
        self.assertEqual(found["videos"][0]["url"], YT)
        self.assertEqual(found["videos"][0]["title"], "Урок")

    def test_inspect_empty_page_is_scanned(self) -> None:
        def fake(_url: str) -> tuple[str, str, str]:
            return PAGE, "<html><p>текста нет</p></html>", "text/html"

        found = pv.inspect_page(PAGE, fetch=fake)
        self.assertEqual(found["mode"], "direct")
        self.assertTrue(found["scanned"])
        self.assertEqual(found["videos"], [])

    def test_inspect_fetch_error_falls_through(self) -> None:
        def fake(_url: str) -> tuple[str, str, str]:
            raise OSError("timeout")

        found = pv.inspect_page(PAGE, fetch=fake)
        self.assertEqual(found["mode"], "direct")
        self.assertFalse(found["scanned"])


class InspectRouteTests(unittest.TestCase):
    def test_route_drops_duplicate_after_normalize(self) -> None:
        import app

        original = app.page_videos.inspect_page

        def fake(_url: str, fetch: object = None) -> dict:
            return {
                "mode": "embeds",
                "scanned": True,
                "videos": [
                    {
                        "url": "https://www.youtube.com/watch?v=abcdefghijk",
                        "title": "Урок",
                        "kind": "iframe",
                    },
                    {
                        "url": "https://www.youtube.com/watch?v=abcdefghijk",
                        "title": "Дубль",
                        "kind": "meta",
                    },
                ],
            }

        app.page_videos.inspect_page = fake
        try:
            result = app.inspect_link(app.InspectRequest(url="https://news.example/a"))
        finally:
            app.page_videos.inspect_page = original
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "embeds")
        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(result["videos"][0]["title"], "Урок")

    def test_route_empty_embed_list_falls_back(self) -> None:
        import app

        original = app.page_videos.inspect_page
        app.page_videos.inspect_page = lambda _url, fetch=None: {
            "mode": "embeds",
            "videos": [],
            "scanned": True,
        }
        try:
            result = app.inspect_link(app.InspectRequest(url="https://news.example/a"))
        finally:
            app.page_videos.inspect_page = original
        self.assertEqual(result["mode"], "direct")
        self.assertEqual(result["videos"], [])

    def test_route_rejects_text(self) -> None:
        import app

        result = app.inspect_link(app.InspectRequest(url="просто текст"))
        self.assertFalse(result["ok"])


class LiveFetchTests(unittest.TestCase):
    def test_local_page(self) -> None:
        html = (
            "<!doctype html><meta property='og:title' content='Локальный урок'>"
            "<iframe src='https://www.youtube.com/embed/abcdefghijk'></iframe>"
        ).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = html if self.path.startswith("/article") else b"no"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            found = pv.inspect_page(f"http://127.0.0.1:{port}/article")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(found["mode"], "embeds")
        self.assertEqual(found["videos"][0]["url"], YT)
        self.assertEqual(found["videos"][0]["title"], "Локальный урок")


if __name__ == "__main__":
    unittest.main()
