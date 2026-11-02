# Файл: test_updates.py
# Сравнение версии со страницей выпусков. Сеть не нужна.
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import app

ROOT = Path(__file__).resolve().parent
SETUP = (
    "https://github.com/neuromafka-create/kachalka/releases/download/"
    "v1.4.0/Kachalka-Setup-1.4.0.exe"
)
PAGE = "https://github.com/neuromafka-create/kachalka/releases/tag/v1.4.0"


def _release(tag: str = "v1.4.0", setup: str = SETUP, page: str = PAGE) -> dict:
    return {
        "tag_name": tag,
        "html_url": page,
        "assets": [
            {
                "name": "Kachalka-Extension-1.4.0.zip",
                "browser_download_url": page.replace("/tag/", "/download/") + "/Kachalka-Extension-1.4.0.zip",
            },
            {
                "name": "Kachalka-Setup-1.4.0.exe",
                "browser_download_url": setup,
            },
        ],
    }


class VersionTests(unittest.TestCase):
    def test_newer_patch_and_minor(self) -> None:
        self.assertTrue(app._version_is_newer("v1.3.10", "1.3.9"))
        self.assertTrue(app._version_is_newer("1.4", "1.3.9"))
        self.assertFalse(app._version_is_newer("v1.3.9", "1.3.9"))
        self.assertFalse(app._version_is_newer("1.3.8", "1.3.9"))
        self.assertFalse(app._version_is_newer("не версия", "1.3.9"))

    def test_app_version_matches_installer_and_readme(self) -> None:
        iss = (ROOT / "installer.iss").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f'#define MyAppVersion "{app.APP_VERSION}"', iss)
        self.assertIn(f"VersionInfoVersion={app.APP_VERSION}.0", iss)
        self.assertIn(f"**Версия {app.APP_VERSION}**", readme)
        self.assertEqual(app.get_config()["version"], app.APP_VERSION)

    def test_release_offers_the_setup_when_newer(self) -> None:
        found = app._describe_release(_release(), current="1.3.9")
        self.assertTrue(found["ok"])
        self.assertTrue(found["update"])
        self.assertEqual(found["latest"], "1.4.0")
        self.assertEqual(found["download_url"], SETUP)
        self.assertIn("Вышла версия 1.4.0", found["message"])

    def test_same_version_has_no_download(self) -> None:
        found = app._describe_release(_release(tag="v1.3.9"), current="1.3.9")
        self.assertFalse(found["update"])
        self.assertEqual(found["download_url"], "")
        self.assertEqual(found["message"], "Это последняя версия.")

    def test_foreign_setup_link_is_dropped(self) -> None:
        found = app._describe_release(
            _release(setup="https://example.com/Kachalka-Setup-1.4.0.exe"),
            current="1.3.9",
        )
        self.assertTrue(found["update"])
        self.assertEqual(found["download_url"], PAGE)

    def test_check_failure_stays_on_the_current_version(self) -> None:
        with patch.object(app, "fetch_latest_release", side_effect=OSError("offline")):
            found = app.check_for_update()
        self.assertFalse(found["ok"])
        self.assertFalse(found["update"])
        self.assertEqual(found["current"], app.APP_VERSION)
        self.assertEqual(found["download_url"], "")
        self.assertIn("Не удалось проверить", found["message"])

    def test_check_reads_a_release_without_the_network(self) -> None:
        with patch.object(app, "fetch_latest_release", return_value=_release()):
            found = app.check_for_update()
        self.assertEqual(found["download_url"], SETUP)

    def test_open_release_rejects_other_sites(self) -> None:
        with patch("app.webbrowser.open") as opened:
            bad = app.open_release_page("https://example.com/setup.exe")
            also = app.open_release_page("http://github.com/neuromafka-create/kachalka/releases")
        self.assertFalse(bad["ok"])
        self.assertFalse(also["ok"])
        opened.assert_not_called()

    def test_open_release_opens_the_github_file(self) -> None:
        with patch("app.webbrowser.open", return_value=True) as opened:
            good = app.open_release_page(SETUP)
        self.assertTrue(good["ok"])
        opened.assert_called_once_with(SETUP)

    def test_window_has_version_and_about_dialog(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="app-version"', html)
        self.assertIn('id="btn-about"', html)
        self.assertIn("Проверить обновления", html)
        self.assertIn("Скачать новую версию", html)
        self.assertIn("/api/updates", html)
        self.assertIn("/api/open-release", html)


if __name__ == "__main__":
    unittest.main()
