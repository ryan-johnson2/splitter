#!/usr/bin/env python3
"""Cython-compile an installed pure-Python package in place and drop its sources.

    python desktop/sidecar/protect.py velocidrone_tracks

Used by the sidecar build (--protect) and by the Docker image build so that
shipped artifacts carry native extension modules rather than readable Python
for the private track client. ``__init__.py`` stays pure Python: PyInstaller's
frozen importer cannot load a package whose __init__ is an extension module,
and it only re-exports names anyway.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


def protect_package(name: str) -> None:
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit(f"{name} is not installed; cannot protect it")
    pkg_dir = Path(next(iter(spec.submodule_search_locations)))
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
         "cython>=3.0", "setuptools"],
        check=True,
    )
    modules = sorted(p for p in pkg_dir.glob("*.py") if p.name != "__init__.py")
    print(f"compiling {len(modules)} modules of {name} in {pkg_dir}", flush=True)
    # build_ext --inplace resolves "<pkg>.<mod>" relative to the cwd, so run from
    # the package's parent (site-packages) with package-relative paths.
    parent = pkg_dir.parent
    rel = [str(m.relative_to(parent)) for m in modules]
    setup = parent / "_protect_setup.py"
    setup.write_text(
        "from setuptools import setup\nfrom Cython.Build import cythonize\n"
        f"setup(name='{name}_ext', ext_modules=cythonize({rel!r}, "
        "compiler_directives={'language_level': '3', 'emit_code_comments': False}, quiet=True), "
        "script_args=['build_ext', '--inplace'])\n"
    )
    try:
        subprocess.run([sys.executable, str(setup)], check=True, cwd=str(parent))
    finally:
        setup.unlink(missing_ok=True)
        shutil.rmtree(parent / "build", ignore_errors=True)
    for m in modules:
        built = list(pkg_dir.glob(m.stem + ".*.so")) + list(pkg_dir.glob(m.stem + ".*.pyd"))
        if not built:
            raise SystemExit(f"no extension built for {m.name}")
        m.unlink()
        for c in pkg_dir.glob(m.stem + ".c"):
            c.unlink()
    for junk in ("build", "__pycache__"):
        shutil.rmtree(pkg_dir / junk, ignore_errors=True)
    print(f"{name} protected: {sorted(p.name for p in pkg_dir.iterdir())}", flush=True)


if __name__ == "__main__":
    for pkg in sys.argv[1:] or ["velocidrone_tracks"]:
        protect_package(pkg)
