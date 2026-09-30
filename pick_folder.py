# Файл: pick_folder.py
# Отдельный процесс для диалога «куда сохранять».
# Запуск: pythonw pick_folder.py <начальная_папка> <файл_результата>
# В файл пишется выбранный путь (UTF-8) или пустая строка, если отмена.
from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

# IFileDialog::Show — пользователь закрыл окно без выбора.
_HRESULT_CANCELLED = 0x800704C7


def _write(out: Path, text: str) -> None:
    out.write_text(text or "", encoding="utf-8")


def _cancelled(hr: int) -> bool:
    return (int(hr) & 0xFFFFFFFF) == _HRESULT_CANCELLED


def _failed(hr: int) -> bool:
    # Старший бит: и для знакового HRESULT, и если ctypes отдал его без знака.
    return (int(hr) & 0x80000000) != 0


class _DialogFinished(Exception):
    """Окно уже показали. Второй диалог открывать нельзя."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _pick_modern(initial: str) -> str:
    """Окно выбора папки текущей Windows (IFileOpenDialog).

    Путь или '' (отмена). Исключение — диалог даже не открылся.
    Старый SHBrowseForFolder сюда не ставить: на 64-битном Python
    указатель папки обрезается, выбор пропадает, и следом всплывает
    второй диалог.
    """
    if sys.platform != "win32":
        raise RuntimeError("not windows")

    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    def guid(text: str) -> GUID:
        value = GUID()
        ctypes.memmove(ctypes.byref(value), uuid.UUID(text).bytes_le, 16)
        return value

    def method(ptr, index, restype, argtypes):
        table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return proto(table[index])

    def release(ptr) -> None:
        if ptr is not None and getattr(ptr, "value", None):
            method(ptr, 2, ctypes.c_ulong, [])(ptr)

    COINIT_APARTMENTTHREADED = 0x2
    CLSCTX_INPROC_SERVER = 0x1
    FOS_PICKFOLDERS = 0x20
    FOS_FORCEFILESYSTEM = 0x40
    FOS_PATHMUSTEXIST = 0x800
    SIGDN_FILESYSPATH = 0x80058000

    co_init = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_ulong)(
        ("CoInitializeEx", ole32)
    )
    co_create = ctypes.WINFUNCTYPE(
        ctypes.HRESULT,
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )(("CoCreateInstance", ole32))
    co_task_free = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)(("CoTaskMemFree", ole32))
    co_uninit = ctypes.WINFUNCTYPE(None)(("CoUninitialize", ole32))
    from_path = ctypes.WINFUNCTYPE(
        ctypes.HRESULT,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )(("SHCreateItemFromParsingName", shell32))

    dialog = ctypes.c_void_p()
    folder_item = ctypes.c_void_p()
    result_item = ctypes.c_void_p()
    own_com = False
    try:
        init_hr = int(co_init(None, COINIT_APARTMENTTHREADED))
        if _failed(init_hr):
            raise OSError(f"CoInitializeEx: {init_hr & 0xFFFFFFFF:#010x}")
        own_com = True

        hr = int(
            co_create(
                ctypes.byref(guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")),
                None,
                CLSCTX_INPROC_SERVER,
                ctypes.byref(guid("d57c7288-d4ad-4768-be02-9d969532d960")),
                ctypes.byref(dialog),
            )
        )
        if _failed(hr) or not dialog.value:
            raise OSError(f"CoCreateInstance: {hr & 0xFFFFFFFF:#010x}")

        options = wintypes.DWORD()
        hr = int(method(dialog, 10, ctypes.HRESULT, [ctypes.POINTER(wintypes.DWORD)])(
            dialog, ctypes.byref(options)
        ))
        if _failed(hr):
            raise OSError(f"GetOptions: {hr & 0xFFFFFFFF:#010x}")
        flags = options.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
        hr = int(method(dialog, 9, ctypes.HRESULT, [wintypes.DWORD])(dialog, flags))
        if _failed(hr):
            raise OSError(f"SetOptions: {hr & 0xFFFFFFFF:#010x}")

        hr = int(method(dialog, 17, ctypes.HRESULT, [wintypes.LPCWSTR])(
            dialog, "Куда сохранять видео"
        ))
        if _failed(hr):
            raise OSError(f"SetTitle: {hr & 0xFFFFFFFF:#010x}")

        if initial and Path(initial).is_dir():
            hr = int(
                from_path(
                    initial,
                    None,
                    ctypes.byref(guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")),
                    ctypes.byref(folder_item),
                )
            )
            if not _failed(hr) and folder_item.value:
                method(dialog, 12, ctypes.HRESULT, [ctypes.c_void_p])(
                    dialog, folder_item
                )

        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        owner = user32.FindWindowW(None, "Качалка") or 0
        hr = int(method(dialog, 3, ctypes.HRESULT, [wintypes.HWND])(dialog, owner))
        if _cancelled(hr):
            raise _DialogFinished("")
        if _failed(hr):
            raise OSError(f"Show: {hr & 0xFFFFFFFF:#010x}")

        hr = int(method(dialog, 20, ctypes.HRESULT, [ctypes.POINTER(ctypes.c_void_p)])(
            dialog, ctypes.byref(result_item)
        ))
        if _failed(hr) or not result_item.value:
            raise _DialogFinished("")

        name = ctypes.c_wchar_p()
        hr = int(
            method(
                result_item,
                5,
                ctypes.HRESULT,
                [ctypes.c_uint, ctypes.POINTER(ctypes.c_wchar_p)],
            )(result_item, SIGDN_FILESYSPATH, ctypes.byref(name))
        )
        if _failed(hr) or not name.value:
            raise _DialogFinished("")
        path = name.value
        co_task_free(ctypes.cast(name, ctypes.c_void_p))
        raise _DialogFinished(path.strip())
    finally:
        for ptr in (result_item, folder_item, dialog):
            try:
                release(ptr)
            except Exception:
                pass
        if own_com:
            try:
                co_uninit()
            except Exception:
                pass


def probe_modern_dialog(initial: str | None = None) -> None:
    """Создать диалог и отпустить его, не показывая окно. Для проверки COM."""
    if sys.platform != "win32":
        raise RuntimeError("not windows")

    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.windll.ole32

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    def guid(text: str) -> GUID:
        value = GUID()
        ctypes.memmove(ctypes.byref(value), uuid.UUID(text).bytes_le, 16)
        return value

    co_init = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_ulong)(
        ("CoInitializeEx", ole32)
    )
    co_create = ctypes.WINFUNCTYPE(
        ctypes.HRESULT,
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )(("CoCreateInstance", ole32))
    co_uninit = ctypes.WINFUNCTYPE(None)(("CoUninitialize", ole32))

    hr = int(co_init(None, 0x2))
    if _failed(hr):
        raise OSError(f"CoInitializeEx: {hr & 0xFFFFFFFF:#010x}")
    dialog = ctypes.c_void_p()
    try:
        hr = int(
            co_create(
                ctypes.byref(guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")),
                None,
                1,
                ctypes.byref(guid("d57c7288-d4ad-4768-be02-9d969532d960")),
                ctypes.byref(dialog),
            )
        )
        if _failed(hr) or not dialog.value:
            raise OSError(f"CoCreateInstance: {hr & 0xFFFFFFFF:#010x}")
        table = ctypes.cast(dialog, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        get_options = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)
        )(table[10])
        set_options = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.DWORD)(
            table[9]
        )
        set_title = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.LPCWSTR)(
            table[17]
        )
        options = wintypes.DWORD()
        hr = int(get_options(dialog, ctypes.byref(options)))
        if _failed(hr):
            raise OSError(f"GetOptions: {hr & 0xFFFFFFFF:#010x}")
        hr = int(set_options(dialog, options.value | 0x20 | 0x40 | 0x800))
        if _failed(hr):
            raise OSError(f"SetOptions: {hr & 0xFFFFFFFF:#010x}")
        hr = int(set_title(dialog, "Куда сохранять видео"))
        if _failed(hr):
            raise OSError(f"SetTitle: {hr & 0xFFFFFFFF:#010x}")
    finally:
        if dialog.value:
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                ctypes.cast(dialog, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0][2]
            )
            release(dialog)
        co_uninit()


def _pick_tk(initial: str) -> str:
    """Запасной диалог. Путь или '' (отмена). Исключение — не удалось показать."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    # Пустое окно Tcl не показываем: это не выбор папки, а служебный корень.
    root.withdraw()
    try:
        root.attributes("-alpha", 0)
    except tk.TclError:
        pass
    try:
        root.wm_attributes("-topmost", 1)
    except tk.TclError:
        pass
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
    """Последний запасной путь. Путь или '' (отмена). Исключение — не удалось показать."""
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

    # Сначала одно окно текущей системы. Дальше — только если оно не открылось.
    methods = (_pick_modern, _pick_tk, _pick_powershell)
    last_error = ""
    for method_fn in methods:
        try:
            chosen = method_fn(initial)
            _write(out, chosen)
            return 0
        except _DialogFinished as done:
            _write(out, done.path)
            return 0
        except Exception as exc:
            last_error = str(exc)
            continue

    _write(out, "")
    if last_error:
        try:
            sys.stderr.write(last_error + "\n")
        except Exception:
            pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
