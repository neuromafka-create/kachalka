# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: pyinstaller --noconfirm kachalka.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
root = Path(SPECPATH)

datas = [
    (str(root / "static"), "static"),
    (str(root / "pick_folder.py"), "."),
]
# ffmpeg, если уже скачан install.bat
ffmpeg_bin = root / "ffmpeg" / "bin"
if ffmpeg_bin.is_dir():
    datas.append((str(ffmpeg_bin), "ffmpeg/bin"))

icon_path = root / "assets" / "icon.ico"
if icon_path.is_file():
    datas.append((str(icon_path), "assets"))
    # Дублируем в корень onedir — для ярлыков и ручной смены иконки
    datas.append((str(icon_path), "."))

# yt-dlp тянет кучу extractors — собираем целиком
tmp_ret = collect_all("yt_dlp")
datas += tmp_ret[0]
binaries = list(tmp_ret[1])
hiddenimports = list(tmp_ret[2])

for pkg in ("uvicorn", "anyio", "starlette", "fastapi", "webview", "certifi"):
    try:
        r = collect_all(pkg)
        datas += r[0]
        binaries += r[1]
        hiddenimports += r[2]
    except Exception:
        pass

hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "app",
    "page_videos",
    "pick_folder",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "multipart",
]

a = Analysis(
    [str(root / "desktop_main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Kachalka",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # без чёрной консоли — обычное окно
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Kachalka",
)
