# Файл: pick_folder.py
# Отдельный процесс для диалога «куда сохранять».
# Запуск: pythonw pick_folder.py <начальная_папка> <файл_результата>
# В файл пишется выбранный путь (UTF-8) или пустая строка, если отмена.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _write(out: Path, text: str) -> None:
    out.write_text(text or "", encoding="utf-8")


def _pick_win32(initial: str) -> str:
    """Нативный диалог Windows. Путь или '' (отмена). Исключение — не удалось показать."""
    if sys.platform != "win32":
        raise RuntimeError("not windows")

    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", ctypes.c_wchar_p),
            ("lpszTitle", ctypes.c_wchar_p),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_NEWDIALOGSTYLE = 0x0040
    BIF_EDITBOX = 0x0010

    display = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFOW()
    bi.hwndOwner = None
    bi.pidlRoot = None
    bi.pszDisplayName = ctypes.cast(display, ctypes.c_wchar_p)
    bi.lpszTitle = "Куда сохранять видео"
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX
    bi.lpfn = None
    bi.lParam = 0
    bi.iImage = 0

    ole32.CoInitialize(None)
    try:
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
        path_buf = ctypes.create_unicode_buffer(1024)
        ok = shell32.SHGetPathFromIDListW(pidl, path_buf)
        ole32.CoTaskMemFree(pidl)
        if not ok:
            return ""
        return (path_buf.value or "").strip()
    finally:
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


def _pick_tk(initial: str) -> str:
    """tkinter. Путь или '' (отмена). Исключение — не удалось показать."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.wm_attributes("-topmost", 1)
    except tk.TclError:
        pass
    root.update()
    try:
        chosen = filedialog.askdirectory(
            parent=root,
            title="Куда сохранять видео",
            initialdir=initial,
        )
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    return (chosen or "").strip()


def _pick_powershell(initial: str) -> str:
    """WinForms. Путь или '' (отмена). Исключение — не удалось показать."""
    safe = initial.replace("'", "''")
    ps = f"""
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Куда сохранять видео'
$d.ShowNewFolderButton = $true
$d.SelectedPath = '{safe}'
$r = $d.ShowDialog()
if ($r -eq [System.Windows.Forms.DialogResult]::OK) {{
  [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
  [Console]::Write($d.SelectedPath)
}}
"""
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-Command",
            ps,
        ],
        capture_output=True,
        timeout=600,
    )
    raw = proc.stdout or b""
    for enc in ("utf-8", "utf-16-le", "cp1251"):
        try:
            text = raw.decode(enc).strip("\r\n\x00 ").strip()
        except UnicodeDecodeError:
            continue
        if text:
            return text
    return ""


def main() -> int:
    initial = sys.argv[1] if len(sys.argv) > 1 else ""
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if out is None:
        print("usage: pick_folder.py <initial_dir> <result_file>", file=sys.stderr)
        return 2

    if not initial or not Path(initial).is_dir():
        initial = str(Path.home())

    methods = (_pick_win32, _pick_tk, _pick_powershell)
    last_error = ""
    for method in methods:
        try:
            # Успешно показали диалог (даже если отмена) — не пробуем дальше
            chosen = method(initial)
            _write(out, chosen)
            return 0
        except Exception as exc:
            last_error = str(exc)
            continue

    _write(out, "")
    # Не удалось ни один способ — пустой файл + код 1
    if last_error:
        try:
            sys.stderr.write(last_error + "\n")
        except Exception:
            pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
