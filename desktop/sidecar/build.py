#!/usr/bin/env python3
"""Build the Splitter sidecar (PyInstaller one-dir) and pack it into one archive.

    python desktop/sidecar/build.py [--protect] [--tracks PATH_OR_WHEEL]

Steps: install Splitter and the vendored velocidrone-ws into the current
interpreter; optionally install the private velocidrone-tracks client and,
with --protect, compile it with Cython so the frozen build carries a native
extension rather than readable Python; run PyInstaller (one-dir: an executable
plus an _internal/ folder — one-file builds are a notorious antivirus false
positive, see docs/code-signing.md); then pack the folder as
desktop/sidecar/dist/splitter-sidecar.tar.gz, which the Tauri shell embeds
(desktop/src-tauri/build.rs) and extracts once per version at launch.

Archive layout (the shell relies on it): every entry is under a top-level
``splitter-sidecar/`` directory, and the executable is
``splitter-sidecar/splitter-sidecar[.exe]``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
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

    stamp = os.environ.get("SPLITTER_BUILD", "").strip()
    run(PY, str(ROOT / "scripts" / "stamp.py"), "--write", *(["--stamp", stamp] if stamp else []))
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
    folder = dist / "splitter-sidecar"
    exe = folder / ("splitter-sidecar.exe" if os.name == "nt" else "splitter-sidecar")
    if not exe.is_file():
        raise SystemExit(f"expected {exe}")
    archive = pack(folder, dist / "splitter-sidecar.tar.gz")
    size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    print(f"sidecar built: {folder} ({size // 1_000_000} MB unpacked) → "
          f"{archive} ({archive.stat().st_size // 1_000_000} MB)", flush=True)


def pack(folder: Path, archive: Path) -> Path:
    """Tar+gzip ``folder`` as ``splitter-sidecar/…`` (sorted, no owner names, mtimes
    zeroed) so the same build gives the same bytes; modes are kept so the Unix
    executable and shared objects come out runnable."""
    def norm(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info

    archive.unlink(missing_ok=True)
    with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
        for path in sorted(folder.rglob("*")):
            if path.is_dir():
                continue
            tar.add(path, arcname=f"splitter-sidecar/{path.relative_to(folder).as_posix()}",
                    recursive=False, filter=norm)
    return archive


if __name__ == "__main__":
    main()
