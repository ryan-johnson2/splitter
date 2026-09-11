"""The installed package version, for the footer, settings page and /healthz."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("splitter")
except PackageNotFoundError:  # running from a source checkout without an install
    __version__ = "dev"
