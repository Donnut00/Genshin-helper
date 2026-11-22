# -*- mode: python ; coding: utf-8 -*-
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "src", "data", "category_aliases.json"), os.path.join("src", "data")),
    (os.path.join(ROOT, "src", "locales"), os.path.join("src", "locales")),
    (os.path.join(ROOT, "src", "zoom_thumb_template.png"), "src"),
    (os.path.join(ROOT, "assets", "icons"), os.path.join("assets", "icons")),
]

hiddenimports = [
    "PIL.IcoImagePlugin",
    "keyboard",
    "src.hoyolab_sync",
    "src.hotkey_conflicts",
    "src.pin_guide",
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    "winrt.windows.media.ocr",
    "winrt.windows.graphics.imaging",
    "winrt.windows.storage.streams",
]

excludes = [
    "bettercam",
    "mss",
    "websockets",
    "pynput",
    "requests",
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "pytest",
]

a_main = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz_main = PYZ(a_main.pure)

exe_main = EXE(
    pyz_main,
    a_main.scripts,
    [],
    exclude_binaries=True,
    name="GenshinHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "assets", "icons", "app.ico"),
)

a_host = Analysis(
    [os.path.join(ROOT, "src", "native_host.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes + ["cv2", "numpy", "PIL"],
    noarchive=False,
    optimize=0,
)
pyz_host = PYZ(a_host.pure)

exe_host = EXE(
    pyz_host,
    a_host.scripts,
    [],
    exclude_binaries=True,
    name="GenshinHelperHost",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "assets", "icons", "app.ico"),
)

coll = COLLECT(
    exe_main,
    a_main.binaries,
    a_main.datas,
    exe_host,
    a_host.binaries,
    a_host.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GenshinHelper",
)
