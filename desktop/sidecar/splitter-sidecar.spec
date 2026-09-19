# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Splitter sidecar: a one-DIR build (an executable plus
# an _internal/ folder) that serves the whole web app. Built by
# desktop/sidecar/build.py, which packs the folder into one tar.gz that
# desktop/src-tauri/build.rs embeds into the Tauri shell.
#
# One-dir, not one-file, on purpose: a one-file exe unpacks itself into a temp
# folder on every launch, which is exactly what packers and droppers do, and
# unsigned PyInstaller one-file builds are a well-known Defender / SmartScreen
# false positive. A one-dir build is extracted once by the shell into a stable
# folder and runs from there. See docs/code-signing.md.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parents[1]  # repo root
SRC = ROOT / "src" / "splitter"

datas = [
    (str(SRC / "web" / "templates"), "splitter/web/templates"),
    (str(SRC / "web" / "static"), "splitter/web/static"),
    (str(SRC / "data"), "splitter/data"),
]
datas += copy_metadata("splitter")

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + collect_submodules("sqlalchemy.dialects.sqlite")
    + ["aiosqlite", "greenlet", "multipart", "python_multipart", "pydantic_settings", "jinja2"]
)
# The optional private track client. When it is Cython-compiled its imports are
# invisible to the analysis (no bytecode to scan), so name its dependencies too.
for opt in ("velocidrone_tracks", "velocidrone_api"):
    try:
        __import__(opt)
    except ImportError:
        continue
    hiddenimports += collect_submodules(opt)
    hiddenimports += collect_submodules("httpx") + collect_submodules("httpcore")
    hiddenimports += collect_submodules("Crypto.Cipher") + collect_submodules("Crypto.Util")
    hiddenimports += ["anyio", "sniffio", "h11", "idna", "certifi"]
    datas += collect_data_files("certifi")

a = Analysis(
    [str(SRC / "desktop_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="splitter-sidecar",
    console=True,  # the shell reads SPLITTER_READY from stdout; no window is created on Windows by the shell
    disable_windowed_traceback=False,
    upx=False,
    strip=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="splitter-sidecar",  # → dist/splitter-sidecar/{splitter-sidecar[.exe], _internal/}
    upx=False,
    strip=False,
)
