"""Millisecond formatting shared by templates, the live feed and the CLI."""

from __future__ import annotations


def format_ms(ms: int | None) -> str:
    """Render milliseconds as ``M:SS.mmm`` (or ``SS.mmm`` under a minute)."""
    if ms is None or ms < 0:
        return "--"
    minutes, rem = divmod(ms, 60_000)
    seconds = rem / 1000
    if minutes:
        return f"{minutes}:{seconds:06.3f}"
    return f"{seconds:.3f}"


def format_delta(ms: int | None) -> str:
    """Render a split as ``-0.123`` / ``+0.123`` (``--`` when unknown)."""
    if ms is None:
        return "--"
    sign = "-" if ms < 0 else "+"
    return f"{sign}{abs(ms) / 1000:.3f}"


def format_speed(mps: float | None) -> str:
    """Metres/second → ``km/h`` with no decimals."""
    if mps is None:
        return "--"
    return f"{mps * 3.6:.0f}"
