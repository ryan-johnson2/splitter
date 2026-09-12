# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Splitter sidecar: one self-contained executable that
# serves the whole web app. Built by desktop/sidecar/build.py; embedded into the
# Tauri shell by desktop/src-tauri/build.rs.
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
    + ["aiosqlite", "multipart", "python_multipart", "pydantic_settings", "jinja2"]
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
    a.binaries,
    a.datas,
    [],
    name="splitter-sidecar",
    console=True,  # the shell reads SPLITTER_READY from stdout; no window is created on Windows by the shell
    disable_windowed_traceback=False,
    upx=False,
    strip=False,
)
