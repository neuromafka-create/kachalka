# Проверка, что «Обзор» открывает один современный диалог Windows,
# а не старое окно SHBrowseForFolder и не пустое окно Tcl.
from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pick_folder


class CancelCodeTests(unittest.TestCase):
    def test_cancelled_hresult(self) -> None:
        self.assertTrue(pick_folder._cancelled(0x800704C7))
        self.assertTrue(pick_folder._cancelled(-2147023673))
        self.assertFalse(pick_folder._cancelled(0))

    def test_modern_is_first(self) -> None:
        source = Path("pick_folder.py").read_text(encoding="utf-8")
        self.assertNotIn("def _pick_win32", source)
        self.assertIn("(_pick_modern, _pick_tk, _pick_powershell)", source)


@unittest.skipUnless(sys.platform == "win32", "диалог папки только на Windows")
class ModernDialogTests(unittest.TestCase):
    def test_dialog_object_opens_without_showing(self) -> None:
        pick_folder.probe_modern_dialog(str(Path.home()))

    def test_shown_dialog_is_the_shell_picker_and_returns_folder(self) -> None:
        result = Path(tempfile.gettempdir()) / "kachalka_pick_folder_test.txt"
        result.unlink(missing_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "pick_folder.py", str(Path.home()), str(result)],
        )
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.SendMessageTimeoutW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_void_p
        enum_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumChildWindows.argtypes = [ctypes.c_void_p, enum_type, ctypes.c_void_p]
        timed = ctypes.c_size_t()

        def send(hwnd, msg, wparam, lparam=0):
            return user32.SendMessageTimeoutW(
                hwnd, msg, wparam, lparam, 0x0002, 400, ctypes.byref(timed)
            )

        try:
            hwnd = None
            deadline = time.time() + 12
            while time.time() < deadline and proc.poll() is None:
                if user32.FindWindowW(None, "Обзор папок") or user32.FindWindowW(None, "tk"):
                    self.fail("перед системным окном открылось лишнее")
                hwnd = user32.FindWindowW(None, "Куда сохранять видео")
                if hwnd:
                    break
                time.sleep(0.1)

            self.assertIsNotNone(hwnd, "системный диалог не появился")
            names: list[str] = []

            def on_child(child, _lparam):
                buf = ctypes.create_unicode_buffer(80)
                send(child, 0x000D, 80, ctypes.addressof(buf))
                if buf.value:
                    names.append(buf.value)
                return True

            callback = enum_type(on_child)
            ready = time.time() + 3
            while time.time() < ready and "Адресная строка" not in names:
                names.clear()
                user32.EnumChildWindows(hwnd, callback, 0)
                time.sleep(0.05)
            # Адресная строка и кнопка «Выбор папки» есть у проводника,
            # у старого дерева «Обзор папок» их нет.
            self.assertIn("Адресная строка", names)
            self.assertIn("Выбор папки", names)
            send(hwnd, 0x0111, 1, 0)
            code = proc.wait(timeout=8)
            self.assertEqual(code, 0)
            self.assertEqual(
                Path(result.read_text(encoding="utf-8")),
                Path.home(),
            )
            self.assertFalse(user32.FindWindowW(None, "Обзор папок"))
            self.assertFalse(user32.FindWindowW(None, "tk"))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
