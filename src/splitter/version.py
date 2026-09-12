"""The version a running build reports (footer, settings page, /healthz, logs).

Build pipelines write ``_stamp.py`` (see ``scripts/stamp.py``): a release is
its tag (``0.1.0``), anything else is ``<base>-dev-<hash>``. Without a stamp,
the installed package version is used, or ``dev`` from a bare checkout.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _resolve() -> str:
    try:
        from splitter._stamp import STAMP

        return str(STAMP)
    except ImportError:
        pass
    try:
        return version("splitter")
    except PackageNotFoundError:  # running from a source checkout without an install
        return "dev"


__version__ = _resolve()
