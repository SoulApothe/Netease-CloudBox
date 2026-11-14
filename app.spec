# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成单文件、免安装依赖的 CloudBox.exe。

分发包只包含 exe 本体（+ cookie.txt），不打包本机的 settings.json、
cloudbox.db、music 目录；这些都会在对方电脑首次启动时自动生成。
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

hiddenimports = [
    "flask", "jinja2", "werkzeug", "requests", "cryptography",
    "config", "music_api", "tags", "downloader", "server", "desktop",
    "accounts", "discover", "importer", "localmusic", "sqlite3",
]
binaries = []
# 只打包只读资源（页面模板）；用户数据一律运行时生成
datas = [("templates", "templates")]

for package in ("webview", "mutagen", "qrcode", "clr_loader", "pythonnet",
                "bottle", "proxy_tools", "cryptography"):
    try:
        pkg_datas, pkg_bins, pkg_hidden = collect_all(package)
        datas += pkg_datas
        binaries += pkg_bins
        hiddenimports += pkg_hidden
    except Exception:
        hiddenimports += collect_submodules(package)

hiddenimports = list(dict.fromkeys(hiddenimports))

ICON = "assets/cloudbox.ico" if os.path.isfile("assets/cloudbox.ico") else None
VERSION_FILE = "version_info.txt" if os.path.isfile("version_info.txt") else None

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "aiohttp", "aiofiles", "tkinter", "matplotlib", "numpy", "PIL",
        "pytest", "IPython", "notebook", "pandas", "scipy",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CloudBox",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 无黑色控制台窗口，像正规桌面软件
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION_FILE,
)
