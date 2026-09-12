#!/usr/bin/env python3
"""Build the Splitter sidecar executable (PyInstaller one-file).

    python desktop/sidecar/build.py [--protect] [--tracks PATH_OR_WHEEL]

Steps: install Splitter and the vendored velocidrone-ws into the current
interpreter; optionally install the private velocidrone-tracks client and,
with --protect, compile it with Cython so the frozen binary carries a native
extension rather than readable Python; then run PyInstaller. Output:
desktop/sidecar/dist/splitter-sidecar[.exe].
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protect import protect_package  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PY = sys.executable


def run(*cmd: str, **kw: object) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(list(cmd), check=True, **kw)  # type: ignore[arg-type]


def pip(*args: str) -> None:
    run(PY, "-m", "pip", "install", "--disable-pip-version-check", "-q", *args)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", default=os.environ.get("SPLITTER_TRACKS_SRC", ""),
                    help="path to the private velocidrone-tracks source dir or wheel (optional)")
    ap.add_argument("--protect", action="store_true", help="Cython-compile velocidrone-tracks")
    ap.add_argument("--skip-install", action="store_true")
    args = ap.parse_args()

    if not args.skip_install:
        pip("pyinstaller>=6.10")
        pip(str(ROOT / "libs" / "velocidrone-ws"), str(ROOT))
        if args.tracks:
            pip(args.tracks)
    has_tracks = importlib.util.find_spec("velocidrone_tracks") is not None
    print(f"velocidrone-tracks installed: {has_tracks}", flush=True)
    if args.protect:
        if not has_tracks:
            raise SystemExit("--protect needs velocidrone-tracks installed (pass --tracks)")
        protect_package("velocidrone_tracks")

    dist = HERE / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    shutil.rmtree(HERE / "build", ignore_errors=True)
    run(PY, "-m", "PyInstaller", "--clean", "--noconfirm", "--distpath", str(dist),
        "--workpath", str(HERE / "build"), str(HERE / "splitter-sidecar.spec"))
    out = dist / ("splitter-sidecar.exe" if os.name == "nt" else "splitter-sidecar")
    if not out.is_file():
        raise SystemExit(f"expected {out}")
    print(f"sidecar built: {out} ({out.stat().st_size // 1_000_000} MB)", flush=True)


if __name__ == "__main__":
    main()
